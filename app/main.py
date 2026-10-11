from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import importlib
import importlib.util
import json
import logging
import math
import os
import secrets
import sqlite3
import statistics
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .consensus import ConsensusClient, ConsensusError, api_key as consensus_api_key
from .simulation import PROFILES, SimulatedSource
from .triage_support import ACTIONS, fallback_triage as _fallback_triage, format_constants, numeric

logger = logging.getLogger(__name__)

try:
    bleak_mod = importlib.import_module("bleak")
    BleakClient = getattr(bleak_mod, "BleakClient", None)
    BleakScanner = getattr(bleak_mod, "BleakScanner", None)
except Exception:
    BleakClient = None
    BleakScanner = None


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "health_mvp.db"

HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

DISCLAIMER = "Outil de bien-être, pas un dispositif médical."
DEFAULT_SOURCE = os.getenv("SOURCE", "sim").lower()
TARGET_BLE_NAME = os.getenv("BLE_NAME", "garmin").lower()
DEFAULT_BLE_ADDRESS = os.getenv("BLE_ADDR", "")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
EMERGENCY_PHONE = os.getenv("EMERGENCY_PHONE", "51288215").strip()
try:
    OLLAMA_TIMEOUT_SECONDS = max(5, min(300, float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "90"))))
except ValueError:
    OLLAMA_TIMEOUT_SECONDS = 90.0


class SourceConfig(BaseModel):
    mode: Literal["sim", "ble"]
    device_address: Optional[str] = None


class SimConfig(BaseModel):
    profile: Literal["mixed", "calm", "activity", "stress", "recovery", "sleep", "fever", "respiratory", "hypotension"] = "mixed"
    speed: float = Field(default=1.0, ge=0.25, le=8.0)


class TriageRequest(BaseModel):
    language: str = "fr"
    motif: str = ""
    symptoms: str = ""
    temp: str = "N/A"
    pain: str = "0/10"
    red_flags: dict[str, bool] = Field(default_factory=dict)
    use_monitoring: bool = True


class EmergencyNotificationRequest(BaseModel):
    analyse_id: UUID


class ConsensusChatRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)

    @field_validator("question")
    @classmethod
    def meaningful_question(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("La question doit contenir au moins trois caractères.")
        return value


class HealthConnectRegisterRequest(BaseModel):
    device_id: str = Field(min_length=3, max_length=128)
    patient_id: Optional[str] = Field(default=None, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=128)


class HealthConnectImportRequest(BaseModel):
    device_id: str = Field(min_length=3, max_length=128)
    patient_id: Optional[str] = Field(default=None, max_length=128)
    source: str = Field(default="garmin_connect", max_length=64)
    measurements: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("measurements")
    @classmethod
    def validate_measurements(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("measurements doit être une liste.")
        return value


TRIAGE_SYSTEM_PROMPT = """Tu es Triage-Copilot, assistant de pré-triage médical.
Règle absolue : si au moins une red flag est positive, classe en P1.
Utilise les constantes, tendances et le contexte d'activité fournis dans l'instantané.
Ne remplace jamais une constante par une valeur inventée. Une constante absente reste N/A.
Les données marquées simulées servent uniquement à une démonstration et ne décrivent pas un patient réel.
Une FC élevée pendant l'activité n'a pas le même sens qu'une FC élevée au repos.
Le stress, la RMSSD et Body Battery sont contextuels et ne suffisent pas à établir un diagnostic.
Dans les règles de ce prototype, une température de 36.1 à 38.0 °C n'est pas un critère d'alerte thermique.
Ne qualifie pas une température de 36.7 °C de fièvre ou de température élevée.
N'annonce pas de diagnostic certain, de traitement, de délai garanti ou l'arrivée d'un soignant.
Réponds en JSON uniquement avec les champs suivants :
{
  \"statut_tri\": \"P1|P2|P3|P4\",
  \"recommandation_action\": \"Une courte orientation à valider par l'IAO\",
  \"justification_tri\": [\"Deux ou trois raisons courtes fondées sur les données fournies\"]
}
Si le patient n'est pas urgent, propose une orientation vers un centre de santé ou une pharmacie de garde, sans rejeter le patient.
Le format est strict, sans commentaires, sans markdown, sans texte avant/après JSON.
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json_blob(text: str) -> dict[str, Any]:
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass
    try:
        return json.loads(text)
    except Exception:
        return {}


def local_ollama_generate(prompt: str, model: str = OLLAMA_MODEL) -> str:
    request_body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 300},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=request_body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SECONDS) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return str(data.get("response") or "")


def build_triage_prompt(payload: dict[str, Any], language: str) -> str:
    red_flags = payload.get("red_flags", {})
    red_flag_lines = [
        f"- douleur poitrine: {red_flags.get('chest', False)}",
        f"- difficulté respiratoire: {red_flags.get('breath', False)}",
        f"- faiblesse unilatérale ou perte connaissance: {red_flags.get('weakness', False)}",
        f"- saignement abondant: {red_flags.get('bleeding', False)}",
    ]
    lang = "fr" if language.lower().startswith("fr") else "ar"
    if lang == "ar":
        preface = "Réponds en arabe tunisien (Darja) avec un JSON strict."
    else:
        preface = "Réponds en français avec un JSON strict."
    checked = _fallback_triage(payload)
    return f"""
{preface}
Motif principal: {payload.get('motif', '')}
Symptômes / contexte: {payload.get('symptoms', '')}
Température: {payload.get('temp', 'N/A')}
Douleur EVA: {payload.get('pain', '0/10')}
Réponses red flags:
{chr(10).join(red_flag_lines)}

Instantané des mesures au moment de la demande (source, fraîcheur, activité, tendances et alertes) :
{json.dumps(payload.get('monitoring', {}), ensure_ascii=False)}
Constantes utilisables, avec priorité à la température saisie :
{json.dumps(format_constants(payload), ensure_ascii=False)}
Origine de la température : {payload.get('temperature_source', 'non renseignee')}.
Une donnée périmée ou manquante ne doit pas être présentée comme une mesure actuelle.
Critères vérifiés par les règles du prototype :
{json.dumps(checked['justification_tri'], ensure_ascii=False)}
La priorité ne peut pas être moins urgente que {checked['statut_tri']}.
N'ajoute pas de fièvre, de désaturation ou d'instabilité qui ne figure pas dans les données.

Instruis-toi de ce qui suit :
- Si au moins une réponse red flag est vraie, classe en P1.
- Sinon, adapte la décision selon la douleur, la fièvre, la durée et le retentissement.
- Produis uniquement le JSON demandé.
"""


async def run_triage_with_ollama(payload: dict[str, Any], language: str) -> dict[str, Any]:
    fallback = _fallback_triage(payload)
    # A verified P1 must reach the UI immediately, even if the model is slow
    # or offline. Further interpretation cannot lower this priority.
    if fallback["statut_tri"] == "P1":
        fallback["analyse_prioritaire_immediate"] = True
        return fallback
    final_prompt = TRIAGE_SYSTEM_PROMPT + "\n" + build_triage_prompt(payload, language)
    try:
        # The local HTTP client is blocking: run it outside the event loop so
        # simulation, status and browser updates continue during inference.
        response = await asyncio.wait_for(asyncio.to_thread(local_ollama_generate, final_prompt), timeout=OLLAMA_TIMEOUT_SECONDS + 5)
        parsed = _extract_json_blob(response)
        if not isinstance(parsed, dict) or parsed.get("statut_tri") not in ACTIONS:
            return fallback
        result = dict(fallback)
        result["moteur_analyse"] = "ollama"
        proposed = parsed["statut_tri"]
        priority = f"P{min(int(proposed[1]), int(fallback['statut_tri'][1]))}"
        result["statut_tri"] = priority
        result["garde_fou_applique"] = priority != proposed
        recommendation = parsed.get("recommandation_action")
        result["recommandation_action"] = ACTIONS[priority]
        if isinstance(recommendation, str) and recommendation.strip():
            result["orientation_proposee_ia"] = recommendation.strip()
        explanation = parsed.get("justification_tri")
        if isinstance(explanation, list):
            result["raisonnement_ia"] = [text for text in explanation if isinstance(text, str)]
        elif isinstance(explanation, str):
            result["raisonnement_ia"] = [explanation]
        # Input symptoms and recorded constants stay authoritative even when
        # the model invents, omits or changes its JSON fields.
        return result
    except Exception as exc:
        logger.info("Ollama indisponible, utilisation des règles locales: %s", type(exc).__name__)
        return fallback


class SQLiteSink:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT NOT NULL,
                hr REAL,
                spo2 REAL,
                stress REAL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS emergency_alerts (
                id TEXT PRIMARY KEY,
                analyse_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()
        self.health_connect = HealthConnectConnectorStore(self.conn)

    def write(self, sample: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO measurements (ts, source, hr, spo2, stress, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                sample.get("ts", now_iso()),
                sample.get("source", "unknown"),
                sample.get("hr"),
                sample.get("spo2"),
                sample.get("stress"),
                json.dumps(sample, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def emergency_alert(self, analyse_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT payload_json FROM emergency_alerts WHERE analyse_id = ?", (analyse_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def notify_emergency(self, result: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        context = result.get("contexte_mesures", {})
        alert = {
            "id": str(uuid4()),
            "analyse_id": result["analyse_id"],
            "created_at": now_iso(),
            "priority": "P1",
            "motif": result["motif_principal"],
            "constantes": result["constantes_injectees"],
            "reasons": result["justification_tri"],
            "action": result["recommandation_action"],
            "simulated": bool(context.get("simulated") or context.get("source") == "sim"),
            "channel": "application",
        }
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO emergency_alerts (id, analyse_id, created_at, payload_json) VALUES (?, ?, ?, ?)",
            (alert["id"], alert["analyse_id"], alert["created_at"], json.dumps(alert, ensure_ascii=False)),
        )
        self.conn.commit()
        return self.emergency_alert(alert["analyse_id"]), cursor.rowcount == 1

    def emergency_history(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT payload_json FROM emergency_alerts ORDER BY rowid DESC LIMIT 20").fetchall()
        return [json.loads(row[0]) for row in rows]

    def register_device(self, device_id: str, patient_id: Optional[str], display_name: Optional[str]) -> str:
        return self.health_connect.register_device(device_id, patient_id, display_name)

    def validate_token(self, device_id: str, token: Optional[str]) -> bool:
        return self.health_connect.validate_token(device_id, token)

    def import_measurements(self, device_id: str, patient_id: Optional[str], source: str, measurements: list[dict[str, Any]]) -> tuple[int, int, dict[str, Any]]:
        return self.health_connect.import_measurements(device_id, patient_id, source, measurements)

    def health_connect_status(self) -> dict[str, Any]:
        return self.health_connect.health_connect_status()

    def close(self) -> None:
        self.conn.close()


class HealthConnectConnectorStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS health_connect_devices (
                device_id TEXT PRIMARY KEY,
                patient_id TEXT,
                display_name TEXT,
                token_salt TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_sync TEXT,
                last_heart_rate REAL,
                last_resting_hr REAL,
                last_sleep TEXT,
                last_steps INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS health_connect_measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                patient_id TEXT,
                record_type TEXT NOT NULL,
                source_app TEXT,
                value REAL,
                unit TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(device_id, patient_id, record_type, start_time, end_time, source_app, value, unit)
                ON CONFLICT IGNORE
            )
            """
        )
        self.conn.commit()

    def register_device(self, device_id: str, patient_id: Optional[str], display_name: Optional[str]) -> str:
        device_id = (device_id or "").strip()
        patient_id = (patient_id or "").strip() or None
        display_name = (display_name or "").strip() or None
        if not device_id:
            raise ValueError("device_id requis")
        salt = secrets.token_urlsafe(16)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(f"{salt}{token}".encode("utf-8")).hexdigest()
        updated_at = now_iso()
        self.conn.execute(
            """
            INSERT INTO health_connect_devices (device_id, patient_id, display_name, token_salt, token_hash, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                patient_id = excluded.patient_id,
                display_name = excluded.display_name,
                token_salt = excluded.token_salt,
                token_hash = excluded.token_hash,
                enabled = 1,
                updated_at = excluded.updated_at
            """,
            (device_id, patient_id, display_name, salt, token_hash, updated_at),
        )
        self.conn.commit()
        return token

    def validate_token(self, device_id: str, token: Optional[str]) -> bool:
        if not device_id or not token:
            return False
        row = self.conn.execute(
            "SELECT token_salt, token_hash, enabled, patient_id FROM health_connect_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if row is None:
            return False
        salt, expected_hash, enabled, _ = row
        if not enabled:
            return False
        candidate = hashlib.sha256(f"{salt}{token}".encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate, expected_hash)

    def import_measurements(self, device_id: str, patient_id: Optional[str], source: str, measurements: list[dict[str, Any]]) -> tuple[int, int, dict[str, Any]]:
        device_id = (device_id or "").strip()
        patient_id = (patient_id or "").strip() or None
        if not device_id:
            raise ValueError("device_id requis")
        inserted = 0
        duplicates = 0
        last_sync = now_iso()
        last_heart_rate = None
        last_resting_hr = None
        last_sleep = None
        last_steps = None

        for item in measurements:
            if not isinstance(item, dict):
                continue
            record_type = str(item.get("record_type") or "").strip()
            if not record_type:
                continue
            value = item.get("value")
            if value is None:
                continue
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                continue
            unit = str(item.get("unit") or "").strip() or "unknown"
            start_time = str(item.get("start_time") or "").strip()
            end_time = str(item.get("end_time") or "").strip() or start_time
            if not start_time:
                continue
            try:
                datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            except ValueError:
                continue
            payload = {
                "device_id": device_id,
                "patient_id": patient_id,
                "record_type": record_type,
                "source": source,
                "source_app": item.get("source_app") or "garmin_connect",
                "value": numeric_value,
                "unit": unit,
                "start_time": start_time,
                "end_time": end_time,
                "metadata": item.get("metadata") or {},
            }
            row = self.conn.execute(
                """
                INSERT OR IGNORE INTO health_connect_measurements
                (device_id, patient_id, record_type, source_app, value, unit, start_time, end_time, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    patient_id,
                    record_type,
                    payload["source_app"],
                    numeric_value,
                    unit,
                    start_time,
                    end_time,
                    json.dumps(payload, ensure_ascii=False),
                    now_iso(),
                ),
            )
            if row.rowcount == 1:
                inserted += 1
                if record_type == "HeartRateRecord":
                    last_heart_rate = numeric_value
                elif record_type == "RestingHeartRateRecord":
                    last_resting_hr = numeric_value
                elif record_type == "SleepSessionRecord":
                    last_sleep = end_time or start_time
                elif record_type == "StepsRecord":
                    last_steps = int(numeric_value)
            else:
                duplicates += 1

        self.conn.execute(
            """
            UPDATE health_connect_devices
            SET patient_id = COALESCE(?, health_connect_devices.patient_id),
                last_sync = ?,
                last_heart_rate = COALESCE(?, health_connect_devices.last_heart_rate),
                last_resting_hr = COALESCE(?, health_connect_devices.last_resting_hr),
                last_sleep = COALESCE(?, health_connect_devices.last_sleep),
                last_steps = COALESCE(?, health_connect_devices.last_steps),
                updated_at = ?
            WHERE device_id = ?
            """,
            (
                patient_id,
                last_sync,
                last_heart_rate,
                last_resting_hr,
                last_sleep,
                last_steps,
                now_iso(),
                device_id,
            ),
        )
        self.conn.commit()

        status = {
            "device_id": device_id,
            "patient_id": patient_id,
            "source": source,
            "last_sync": last_sync,
            "last_heart_rate": last_heart_rate,
            "last_resting_hr": last_resting_hr,
            "last_sleep": last_sleep,
            "last_steps": last_steps,
        }
        return inserted, duplicates, status

    def health_connect_status(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT device_id, patient_id, display_name, enabled, last_sync, last_heart_rate, last_resting_hr, last_sleep, last_steps FROM health_connect_devices ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {
                "garmin_connected": False,
                "source": "garmin_health_connect",
                "last_sync": None,
                "last_heart_rate": None,
                "last_resting_hr": None,
                "last_sleep": None,
                "last_steps": None,
                "is_enabled": False,
                "patient_id": None,
            }
        device_id, patient_id, display_name, enabled, last_sync, last_heart_rate, last_resting_hr, last_sleep, last_steps = row
        return {
            "garmin_connected": bool(last_sync and int(enabled or 0) == 1),
            "source": "garmin_health_connect",
            "last_sync": last_sync,
            "last_heart_rate": last_heart_rate,
            "last_resting_hr": last_resting_hr,
            "last_sleep": last_sleep,
            "last_steps": last_steps,
            "is_enabled": bool(enabled),
            "patient_id": patient_id,
            "device_id": device_id,
            "display_name": display_name,
        }


class MetricsEngine:
    def __init__(self) -> None:
        self.last_hour: deque[dict[str, Any]] = deque(maxlen=3600)
        self.rr_recent: deque[float] = deque(maxlen=120)
        self.baseline_rest_hr = 62.0
        self.high_stress_start_ts: Optional[float] = None

    def _window(self, seconds: int, now_ts: float) -> list[dict[str, Any]]:
        return [s for s in self.last_hour if now_ts - s["epoch"] <= seconds]

    @staticmethod
    def _mean(vals: list[float]) -> Optional[float]:
        clean = [v for v in vals if v is not None]
        return statistics.fmean(clean) if clean else None

    @staticmethod
    def _rmssd(rr_ms: list[float]) -> Optional[float]:
        if len(rr_ms) < 3:
            return None
        diffs = [rr_ms[i] - rr_ms[i - 1] for i in range(1, len(rr_ms))]
        sq = [d * d for d in diffs]
        return math.sqrt(statistics.fmean(sq)) if sq else None

    def process(self, sample: dict[str, Any]) -> dict[str, Any]:
        epoch = time.time()
        sample["epoch"] = epoch

        hr = float(sample.get("hr") or 0)
        rr = sample.get("rr_ms") or []
        if rr:
            for x in rr:
                self.rr_recent.append(float(x))

        # Cumulative steps never return to zero after a walk. Use current
        # cadence/step increments so recovery is recognised as rest again.
        moving = float(sample.get("cadence") or sample.get("steps_delta") or 0) > 20 or sample.get("phase") == "active"

        if hr > 35 and hr < 90 and not moving:
            self.baseline_rest_hr = 0.98 * self.baseline_rest_hr + 0.02 * hr

        self.last_hour.append(sample)

        w1 = self._window(60, epoch)
        w5 = self._window(300, epoch)
        w60 = self._window(3600, epoch)

        hr_1m = self._mean([float(s.get("hr") or 0) for s in w1])
        hr_5m = self._mean([float(s.get("hr") or 0) for s in w5])
        hr_1h = self._mean([float(s.get("hr") or 0) for s in w60])

        rmssd = self._rmssd(list(self.rr_recent))
        stress_source = sample.get("stress")

        z_hr = max(0.0, (hr - self.baseline_rest_hr) / 40.0)
        z_hrv = 0.0 if rmssd is None else max(0.0, (35.0 - rmssd) / 35.0)
        stress_score = min(100, int((0.65 * z_hr + 0.35 * z_hrv) * 100))

        if stress_source is not None:
            stress_score = int(0.6 * stress_score + 0.4 * float(stress_source))

        sleep_quality = sample.get("sleep_score")
        if sleep_quality is None:
            sleep_quality = max(30, min(95, int(100 - stress_score * 0.5)))

        recovery_score = int(max(0, min(100, 0.5 * sleep_quality + 0.5 * (100 - stress_score))))

        alerts: list[str] = []
        if hr > self.baseline_rest_hr + 25 and not moving:
            alerts.append("Fréquence cardiaque élevée au repos.")

        spo2 = sample.get("spo2")
        if spo2 is not None and float(spo2) < 92:
            alerts.append("SpO2 basse détectée.")

        if stress_score >= 75:
            if self.high_stress_start_ts is None:
                self.high_stress_start_ts = epoch
            high_for = int((epoch - self.high_stress_start_ts) / 60)
            if high_for >= 10:
                alerts.append(f"Stress élevé prolongé depuis {high_for} min.")
        else:
            self.high_stress_start_ts = None

        if alerts:
            summary = " ; ".join(alerts) + " Vérifier les mesures et le contexte avant l’orientation."
        else:
            summary = "Signaux stables. Continue ton rythme actuel."

        return {
            "ts": sample.get("ts", now_iso()),
            "source": sample.get("source", "unknown"),
            "raw": {
                "phase": sample.get("phase"),
                "sim_profile": sample.get("sim_profile"),
                "simulated_seconds": sample.get("simulated_seconds"),
                "hr": sample.get("hr"),
                "rr_ms": sample.get("rr_ms", []),
                "spo2": sample.get("spo2"),
                "respiration": sample.get("respiration"),
                "temperature": sample.get("temperature"),
                "systolic": sample.get("systolic"),
                "diastolic": sample.get("diastolic"),
                "stress": sample.get("stress"),
                "body_battery": sample.get("body_battery"),
                "sleep_score": sample.get("sleep_score"),
                "steps": sample.get("steps"),
                "steps_delta": sample.get("steps_delta"),
                "cadence": sample.get("cadence"),
                "intensity": sample.get("intensity"),
            },
            "windows": {
                "hr_1m": hr_1m,
                "hr_5m": hr_5m,
                "hr_1h": hr_1h,
                "baseline_rest_hr": self.baseline_rest_hr,
            },
            "derived": {
                "rmssd": rmssd,
                "stress_score": stress_score,
                "recovery_score": recovery_score,
                "sleep_quality": sleep_quality,
            },
            "alerts": alerts,
            "summary": summary,
            "disclaimer": DISCLAIMER,
        }


class WatchBleHrSource:
    def __init__(self, device_address: Optional[str] = None):
        self.device_address = device_address
        self.client: Optional[Any] = None
        self._queue: Optional[asyncio.Queue] = None

    async def _discover_address(self) -> str:
        if BleakScanner is None:
            raise RuntimeError("bleak n'est pas installé")

        devices = await BleakScanner.discover(timeout=10.0)
        if not devices:
            raise RuntimeError("Aucun périphérique BLE détecté.")

        def _name(d: Any) -> str:
            return (getattr(d, "name", None) or "").lower()

        def _uuids(d: Any) -> list[str]:
            meta = getattr(d, "metadata", {}) or {}
            uuids = meta.get("uuids", []) or []
            return [str(u).lower() for u in uuids]

        target_named = [d for d in devices if TARGET_BLE_NAME in _name(d)]
        if target_named:
            return target_named[0].address

        hr_service = [d for d in devices if HEART_RATE_SERVICE_UUID.lower() in _uuids(d)]
        if hr_service:
            return hr_service[0].address

        raise RuntimeError(
            f"Montre '{TARGET_BLE_NAME}' non détectée automatiquement. Utilise le scan et colle l'adresse BLE manuellement."
        )

    async def _resolve_device(self) -> Any:
        """Retourne un périphérique BLE réellement visible maintenant."""
        if BleakScanner is None:
            raise RuntimeError("bleak n'est pas installé")

        devices = await BleakScanner.discover(timeout=8.0)
        if not devices:
            raise RuntimeError("Aucun périphérique BLE détecté.")

        # 1) Adresse manuelle si elle est visible maintenant.
        if self.device_address:
            for device in devices:
                if (device.address or "").lower() == self.device_address.lower():
                    return device

        # 2) Nom cible.
        target_name = TARGET_BLE_NAME
        for device in devices:
            if target_name and target_name in (device.name or "").lower():
                return device

        # 3) Service cardio standard.
        for device in devices:
            uuids = [str(u).lower() for u in (getattr(device, "metadata", {}) or {}).get("uuids", [])]
            if HEART_RATE_SERVICE_UUID.lower() in uuids:
                return device

        # 4) Si aucun nom/service ne matche, on prend le périphérique le plus fort.
        best = None
        best_score = -9999
        for device in devices:
            rssi = getattr(device, "rssi", None)
            score = int(rssi or -9999)
            if score > best_score:
                best = device
                best_score = score

        if best is not None:
            return best

        raise RuntimeError(
            f"Adresse BLE introuvable ou périphérique non visible. Adresse demandée={self.device_address or 'aucune'}, cible={TARGET_BLE_NAME}."
        )

    async def start(self, queue: asyncio.Queue) -> None:
        if BleakClient is None:
            raise RuntimeError("bleak n'est pas installé")

        self._queue = queue
        device = await self._resolve_device()
        address = device.address or await self._discover_address()

        self.client = BleakClient(address)
        await self.client.connect(timeout=12.0)

        def _callback(_: Any, data: bytearray) -> None:
            hr, rr_ms = parse_heart_rate_measurement(data)
            sample = {
                "ts": now_iso(),
                "source": "ble",
                "hr": hr,
                "rr_ms": rr_ms,
                "spo2": None,
                "respiration": None,
                "temperature": None,
                "stress": None,
                "body_battery": None,
                "sleep_score": None,
                "steps": None,
                "intensity": None,
            }
            if self._queue is not None:
                asyncio.create_task(self._queue.put(sample))

        await self.client.start_notify(HEART_RATE_MEASUREMENT_UUID, _callback)

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.stop_notify(HEART_RATE_MEASUREMENT_UUID)
            except Exception:
                pass
            await self.client.disconnect()
            self.client = None


app = FastAPI(title="Garmin Venu Health MVP", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return {
        "active_source": app.state.active_source,
        "default_source": DEFAULT_SOURCE,
        "target_ble_name": TARGET_BLE_NAME,
        "bleak_available": BleakClient is not None,
        "clients": len(app.state.clients),
        "processor_running": not app.state.processor_task.done(),
        "stream_error": app.state.stream_error,
        "websocket_available": bool(importlib.util.find_spec("websockets") or importlib.util.find_spec("wsproto")),
    }


@app.post("/api/integrations/health-connect/register")
async def register_health_connect(request: HealthConnectRegisterRequest) -> dict[str, Any]:
    token = app.state.sink.register_device(request.device_id, request.patient_id, request.display_name)
    return {
        "ok": True,
        "device_id": request.device_id,
        "patient_id": request.patient_id,
        "display_name": request.display_name,
        "token": token,
        "source": "garmin_health_connect",
        "is_enabled": True,
        "last_sync": None,
    }


@app.get("/api/integrations/health-connect/status")
async def health_connect_status() -> dict[str, Any]:
    return app.state.sink.health_connect_status()


@app.post("/api/integrations/health-connect/import")
async def import_health_connect(request: Request, payload: HealthConnectImportRequest) -> dict[str, Any]:
    device_id = (payload.device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id requis")
    token = (request.headers.get("x-connector-token") or request.headers.get("authorization") or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not app.state.sink.validate_token(device_id, token):
        raise HTTPException(status_code=401, detail="Token Health Connect invalide ou non enregistré.")
    inserted, duplicates, status = app.state.sink.import_measurements(
        device_id=device_id,
        patient_id=payload.patient_id,
        source=payload.source,
        measurements=payload.measurements,
    )
    app.state.health_connect_snapshot = status
    return {
        "ok": True,
        "device_id": device_id,
        "patient_id": status.get("patient_id"),
        "inserted": inserted,
        "duplicate_count": duplicates,
        "last_sync": status.get("last_sync"),
        "last_heart_rate": status.get("last_heart_rate"),
        "last_resting_hr": status.get("last_resting_hr"),
        "last_sleep": status.get("last_sleep"),
        "last_steps": status.get("last_steps"),
    }


def monitoring_snapshot() -> dict[str, Any]:
    latest = copy.deepcopy(app.state.latest_payload)
    if latest is None:
        return {"available": False, "fresh": False, "source": app.state.active_source, "raw": {}, "age_seconds": None}
    age = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(latest["ts"])).total_seconds())
    latest["age_seconds"] = round(age, 1)
    latest["fresh"] = age <= 10 and latest["source"] == app.state.active_source
    latest["available"] = True
    latest["simulated"] = latest["source"] == "sim"
    return latest


@app.get("/api/metrics/latest")
async def metrics_latest() -> dict[str, Any]:
    # HTTP works with minimal Uvicorn installations that have no WS adapter.
    return monitoring_snapshot()


@app.get("/api/sim/config")
async def get_sim_config() -> dict[str, Any]:
    return dict(app.state.sim_config)


@app.post("/api/sim/config")
async def update_sim_config(cfg: SimConfig) -> dict[str, Any]:
    async with app.state.source_lock:
        app.state.sim_config.update(cfg.model_dump())
    return {"ok": True, "sim_config": dict(app.state.sim_config)}


@app.get("/api/devices")
async def devices() -> dict[str, Any]:
    if BleakScanner is None:
        return {"devices": [], "error": "bleak non disponible"}

    try:
        found = await asyncio.wait_for(BleakScanner.discover(timeout=8.0), timeout=12.0)
    except asyncio.TimeoutError:
        return {"devices": [], "error": "Timeout scan BLE (adaptateur Bluetooth occupé ?)"}
    except Exception as ex:
        return {"devices": [], "error": f"Erreur scan BLE: {ex}"}

    out: list[dict[str, Any]] = []
    preferred_address: Optional[str] = None
    for d in found:
        meta = getattr(d, "metadata", {}) or {}
        local_name = meta.get("local_name")
        manufacturer_data = meta.get("manufacturer_data", {}) or {}
        name = d.name or local_name or "Unknown"
        addr = d.address
        rssi = getattr(d, "rssi", None)
        uuids = [str(u).lower() for u in (meta.get("uuids", []) or [])]

        is_target_name = TARGET_BLE_NAME in name.lower()
        has_hr_service = HEART_RATE_SERVICE_UUID.lower() in uuids
        score = (2 if is_target_name else 0) + (1 if has_hr_service else 0)

        if name == "Unknown" and has_hr_service:
            name = "Heart Rate device"
        elif name == "Unknown" and manufacturer_data:
            name = f"BLE device ({len(manufacturer_data)} mfg data)"

        if preferred_address is None and score > 0:
            preferred_address = addr

        out.append(
            {
                "name": name,
                "local_name": local_name,
                "address": addr,
                "rssi": rssi,
                "is_target_name": is_target_name,
                "has_hr_service": has_hr_service,
                "score": score,
                "uuids": uuids[:8],
                "manufacturer_data_keys": list(manufacturer_data.keys())[:6],
            }
        )

    out.sort(key=lambda x: (x["score"], x["rssi"] if x["rssi"] is not None else -999), reverse=True)

    auto_pick = out[0]["address"] if out else None
    auto_pick_name = out[0]["name"] if out else None

    return {
        "devices": out,
        "preferred_address": preferred_address,
        "auto_pick": auto_pick,
        "auto_pick_name": auto_pick_name,
        "count": len(out),
        "target_ble_name": TARGET_BLE_NAME,
    }


def _ollama_status() -> dict[str, Any]:
    try:
        req = urllib.request.Request(f"{OLLAMA_BASE_URL}/api/tags", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = data.get("models", []) if isinstance(data, dict) else []
        model_present = any(
            model.get("name", "") == OLLAMA_MODEL
            or (":" not in OLLAMA_MODEL and model.get("name", "").startswith(f"{OLLAMA_MODEL}:"))
            for model in models
        )
        return {"available": model_present, "server_available": True, "base_url": OLLAMA_BASE_URL, "model": OLLAMA_MODEL, "models": models}
    except Exception:
        return {"available": False, "base_url": OLLAMA_BASE_URL, "model": OLLAMA_MODEL, "models": []}


@app.get("/api/triage/status")
async def triage_status() -> dict[str, Any]:
    result = await asyncio.to_thread(_ollama_status)
    result["analysis_timeout_seconds"] = OLLAMA_TIMEOUT_SECONDS + 5
    return result


@app.get("/api/emergency/config")
async def emergency_config() -> dict[str, str]:
    return {"phone": EMERGENCY_PHONE, "notification_channel": "application"}


@app.get("/api/consensus/status")
async def consensus_status() -> dict[str, Any]:
    # No remote request (and no paid search) is made merely to check setup.
    return {"configured": bool(consensus_api_key()), "provider": "consensus"}


@app.post("/api/consensus/chat")
async def consensus_chat(request: ConsensusChatRequest) -> dict[str, Any]:
    try:
        return await app.state.consensus_client.search(request.question)
    except ConsensusError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@app.get("/api/emergency/alerts")
async def emergency_alerts() -> dict[str, Any]:
    return {"alerts": app.state.sink.emergency_history(), "notification_channel": "application"}


@app.post("/api/emergency/notify")
async def emergency_notify(request: EmergencyNotificationRequest) -> dict[str, Any]:
    analyse_id = str(request.analyse_id)
    # Retries (including after a restart) return the original notification.
    existing = app.state.sink.emergency_alert(analyse_id)
    if existing:
        return {"ok": True, "created": False, "alert": existing}
    result = app.state.triage_results.get(analyse_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Analyse introuvable ou expirée. Relancez le pré-triage.")
    if result["statut_tri"] != "P1":
        raise HTTPException(status_code=409, detail="Cette analyse ne comporte pas d’alerte P1.")
    # Only the server's saved snapshot is used; the browser cannot replace
    # the priority, motif or constants when notifying.
    alert, created = app.state.sink.notify_emergency(result)
    return {"ok": True, "created": created, "alert": alert}


@app.post("/api/triage/analyse")
async def triage_analyse(request: TriageRequest) -> dict[str, Any]:
    monitoring = monitoring_snapshot() if request.use_monitoring else {"available": False, "fresh": False, "raw": {}, "source": "disabled"}
    # Historical packets remain visible on the dashboard, but are excluded
    # from the prompt and guardrails once stale. Never silently reuse them.
    if not monitoring.get("fresh"):
        monitoring["raw"] = {}
        monitoring["derived"] = {}
        monitoring["windows"] = {}
        monitoring["alerts"] = []
    manual_temp = numeric(request.temp)
    measured_temp = numeric(monitoring.get("raw", {}).get("temperature"))
    temperature = manual_temp if manual_temp is not None else measured_temp
    temperature_source = "saisie" if manual_temp is not None else monitoring.get("source") if measured_temp is not None else "non renseignee"
    payload = {
        "motif": request.motif,
        "symptoms": request.symptoms,
        "temp": temperature if temperature is not None else "N/A",
        "pain": request.pain,
        "red_flags": request.red_flags,
        "monitoring": monitoring,
        "temperature_source": temperature_source,
    }
    result = await run_triage_with_ollama(payload, request.language)
    result["contexte_mesures"] = monitoring
    result["origine_temperature"] = temperature_source
    result["analyse_ts"] = now_iso()
    result["analyse_id"] = str(uuid4())
    result["validation_iao_requise"] = True
    app.state.triage_results[result["analyse_id"]] = copy.deepcopy(result)
    if len(app.state.triage_results) > 100:
        del app.state.triage_results[next(iter(app.state.triage_results))]
    return result


async def set_source(mode: str, device_address: Optional[str]) -> dict[str, Any]:
    async with app.state.source_lock:
        if app.state.source is not None:
            await app.state.source.stop()
            app.state.source = None

        while not app.state.queue.empty():
            app.state.queue.get_nowait()
            app.state.queue.task_done()
        app.state.latest_payload = None
        app.state.engine = MetricsEngine()
        app.state.stream_error = None

        if mode == "ble":
            src = WatchBleHrSource(device_address=device_address)
        else:
            src = SimulatedSource(app.state.sim_config)
            mode = "sim"

        await src.start(app.state.queue)
        app.state.source = src
        app.state.active_source = mode

    return {"ok": True, "active_source": mode}


@app.post("/api/source")
async def source(cfg: SourceConfig) -> dict[str, Any]:
    try:
        return await set_source(cfg.mode.lower(), cfg.device_address)
    except Exception as ex:
        await set_source("sim", None)
        return {
            "ok": False,
            "active_source": "sim",
            "error": f"BLE indisponible, bascule automatique vers simulateur: {ex}",
        }


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    app.state.clients.add(websocket)
    try:
        if app.state.latest_payload is not None:
            await websocket.send_json(app.state.latest_payload)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        app.state.clients.discard(websocket)


async def processor_loop() -> None:
    while True:
        sample = await app.state.queue.get()
        try:
            payload = app.state.engine.process(sample)
            app.state.measurement_id += 1
            payload["measurement_id"] = app.state.measurement_id
            app.state.latest_payload = payload
        except Exception:
            logger.exception("Traitement d’une mesure impossible")
            app.state.stream_error = "Une mesure n’a pas pu être traitée."
            app.state.queue.task_done()
            continue
        try:
            app.state.sink.write(sample)
        except Exception:
            logger.exception("Enregistrement SQLite impossible; le flux reste actif")

        dead: list[WebSocket] = []
        for ws in tuple(app.state.clients):
            try:
                await asyncio.wait_for(ws.send_json(payload), timeout=1)
            except Exception:
                dead.append(ws)

        for ws in dead:
            app.state.clients.discard(ws)
        app.state.queue.task_done()


@app.on_event("startup")
async def startup() -> None:
    app.state.queue = asyncio.Queue()
    app.state.clients = set()
    app.state.source = None
    app.state.active_source = DEFAULT_SOURCE
    app.state.source_lock = asyncio.Lock()
    app.state.latest_payload = None
    app.state.measurement_id = 0
    app.state.stream_error = None
    app.state.triage_results = {}
    app.state.consensus_client = ConsensusClient()
    requested_profile = os.getenv("SIM_PROFILE", "mixed")
    try:
        requested_speed = float(os.getenv("SIM_SPEED", "1.0"))
        if not math.isfinite(requested_speed):
            requested_speed = 1.0
    except ValueError:
        requested_speed = 1.0
    app.state.sim_config = {"profile": requested_profile if requested_profile in PROFILES else "mixed", "speed": max(0.25, min(8.0, requested_speed))}
    app.state.engine = MetricsEngine()
    app.state.sink = SQLiteSink(DB_PATH)
    app.state.health_connect_snapshot = app.state.sink.health_connect_status()

    app.state.processor_task = asyncio.create_task(processor_loop())
    preferred_address = DEFAULT_BLE_ADDRESS if DEFAULT_SOURCE == "ble" else None
    try:
        await set_source(DEFAULT_SOURCE, preferred_address)
    except Exception:
        await set_source("sim", None)


@app.on_event("shutdown")
async def shutdown() -> None:
    if app.state.source is not None:
        await app.state.source.stop()
    app.state.processor_task.cancel()
    try:
        await app.state.processor_task
    except asyncio.CancelledError:
        pass
    app.state.sink.close()
