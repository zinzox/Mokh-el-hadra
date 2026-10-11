"""Snapshot formatting and conservative demo rules, subject to clinical review.

Physiological alert bands reference RCP NEWS2 Chart 1 (2017):
https://rcp.ac.uk/media/alxev00t/news2-chart-1_the-news-scoring-system_0_0.pdf
The P1-P4 mapping is a prototype rule, NOT a NEWS2 score or validated triage.
Consciousness, oxygen treatment, age and individual baseline are not assessed.
"""
from __future__ import annotations

import math
import re
from typing import Any


def numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    match = re.fullmatch(r"\s*(-?\d+(?:[.,]\d+)?)\s*(?:°?\s*C|/\s*10)?\s*", str(value), re.IGNORECASE)
    if not match:
        return None
    number = float(match[1].replace(",", "."))
    return number if math.isfinite(number) else None


def vital_values(payload: dict[str, Any]) -> dict[str, float | None]:
    monitoring = payload.get("monitoring") or {}
    raw = monitoring.get("raw", {}) if monitoring.get("fresh") else {}
    return {key: numeric(raw.get(key)) for key in ("hr", "spo2", "respiration", "systolic", "diastolic")} | {"temperature": numeric(payload.get("temp"))}


def format_constants(payload: dict[str, Any]) -> dict[str, str]:
    values = vital_values(payload)
    def fmt(key: str, unit: str) -> str:
        return f"{values[key]:g} {unit}" if values[key] is not None else "N/A"
    tension = "N/A"
    if values["systolic"] is not None and values["diastolic"] is not None:
        tension = f"{values['systolic']:g}/{values['diastolic']:g} mmHg"
    return {"tension": tension, "frequence_cardiaque": fmt("hr", "bpm"), "spo2": fmt("spo2", "%"), "temperature": fmt("temperature", "°C"), "frequence_respiratoire": fmt("respiration", "/min")}


def guardrails(payload: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    values = vital_values(payload)
    reasons: list[str] = []
    vital_alerts: list[str] = []
    rank = 4
    severe = 0
    active = (payload.get("monitoring") or {}).get("raw", {}).get("phase") == "active"
    if any(payload.get("red_flags", {}).values()):
        rank = 1
        reasons.append("Au moins un signe d’alerte immédiate a été signalé.")
    def alert(message: str, level: int, critical: bool = False) -> None:
        nonlocal rank, severe
        vital_alerts.append(message)
        rank = min(rank, level)
        severe += int(critical)
    hr, spo2, resp, sbp, temp = (values[key] for key in ("hr", "spo2", "respiration", "systolic", "temperature"))
    if spo2 is not None and spo2 < 96:
        alert(f"SpO₂ basse : {spo2:g} %.", 2 if spo2 < 94 else 3, spo2 < 92)
    if sbp is not None:
        if sbp <= 90 or sbp >= 220:
            alert(f"Tension systolique très anormale : {sbp:g} mmHg.", 2, True)
        elif sbp <= 110:
            alert(f"Tension systolique basse : {sbp:g} mmHg.", 2 if sbp <= 100 else 3)
    if hr is not None:
        if hr <= 40 or (hr >= 131 and not active):
            alert(f"Fréquence cardiaque très anormale au repos : {hr:g} bpm.", 2, True)
        elif not active and (hr >= 111 or hr < 51):
            alert(f"Fréquence cardiaque anormale au repos : {hr:g} bpm.", 2)
        elif not active and hr > 90:
            alert(f"Fréquence cardiaque élevée au repos : {hr:g} bpm.", 3)
    if resp is not None:
        if resp <= 8 or (resp >= 25 and not active):
            alert(f"Fréquence respiratoire très anormale : {resp:g}/min.", 2, True)
        elif resp < 12 or (resp >= 21 and not active):
            alert(f"Fréquence respiratoire anormale : {resp:g}/min.", 3)
    if temp is not None:
        if temp <= 35:
            alert(f"Température très basse : {temp:g} °C.", 2, True)
        elif temp >= 39.1:
            alert(f"Fièvre élevée : {temp:g} °C.", 2)
        elif temp > 38 or temp <= 36:
            alert(f"Température anormale : {temp:g} °C.", 3)
    if severe >= 2:
        rank = 1
        reasons.append("Plusieurs constantes présentent une anomalie majeure dans cet instantané.")
    pain = numeric(payload.get("pain"))
    if pain is None or not 0 <= pain <= 10:
        rank = min(rank, 3)
        reasons.append("La douleur EVA n’est pas exploitable ; elle doit être vérifiée.")
    elif pain >= 7:
        rank = min(rank, 2)
        reasons.append(f"Douleur élevée : {pain:g}/10.")
    elif pain >= 3:
        rank = min(rank, 3)
        reasons.append(f"Douleur signalée : {pain:g}/10.")
    if not payload.get("motif", "").strip() and not payload.get("symptoms", "").strip():
        rank = min(rank, 3)
        reasons.append("Motif et symptômes absents : l’orientation doit être complétée par l’IAO.")
    reasons.extend(vital_alerts)
    if not reasons:
        reasons.append("Aucun critère d’escalade identifié dans les informations disponibles ; validation IAO nécessaire.")
    return f"P{rank}", reasons, vital_alerts


ACTIONS = {
    "P1": "Alerte soignant immédiate. Évaluation sans délai et validation de l’orientation par l’IAO.",
    "P2": "Évaluation prioritaire par l’IAO. Vérifier les constantes et les symptômes avant toute orientation.",
    "P3": "Évaluation médicale standard par l’IAO, avec vérification des symptômes et des constantes disponibles.",
    "P4": "Orientation en soins primaires à confirmer par l’IAO après vérification du contexte et des symptômes.",
}


def fallback_triage(payload: dict[str, Any]) -> dict[str, Any]:
    priority, reasons, alerts = guardrails(payload)
    return {
        "statut_tri": priority, "red_flags_detectes": any(payload.get("red_flags", {}).values()),
        "motif_principal": payload.get("motif") or "Motif principal non précisé",
        "duree_symptomes": payload.get("symptoms") or "Non précisé",
        "niveau_douleur_eva": payload.get("pain", "N/A"),
        "constantes_injectees": format_constants(payload),
        "justification_tri": reasons, "alertes_constantes": alerts,
        "recommandation_action": ACTIONS[priority],
        "moteur_analyse": "regles_locales", "garde_fou_applique": False,
    }
