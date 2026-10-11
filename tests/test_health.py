from __future__ import annotations

import copy
import json
import statistics
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
import urllib.error

from fastapi.testclient import TestClient

import app.main as backend
import app.consensus as consensus
from app.simulation import PROFILES, SimulatedSource
from app.triage_support import fallback_triage


class SimulationTests(unittest.TestCase):
    def samples(self, profile: str, count: int = 180, speed: float = 1):
        source = SimulatedSource({"profile": profile, "speed": speed}, seed=42)
        return [source.next_sample() for _ in range(count)]

    def test_each_profile_starts_in_selected_state(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                first = self.samples(profile, 1)[0]
                expected = "calm" if profile == "mixed" else "active" if profile == "activity" else profile
                self.assertEqual(first["phase"], expected)
                self.assertEqual(first["sim_profile"], profile)
                self.assertGreaterEqual(len(first["rr_ms"]), 3)

    def test_smooth_signals_and_plausible_steps(self):
        samples = self.samples("activity", 120)
        self.assertTrue(all(abs(b["hr"] - a["hr"]) <= 4 for a, b in zip(samples, samples[1:])))
        self.assertTrue(all(abs(b["temperature"] - a["temperature"]) <= 0.04 for a, b in zip(samples, samples[1:])))
        self.assertTrue(all(0 <= sample["steps_delta"] <= 2 for sample in samples))
        self.assertAlmostEqual(samples[-1]["steps"], 220, delta=2)
        self.assertLess(abs(samples[-1]["body_battery"] - samples[0]["body_battery"]), 2)

    def test_sleep_does_not_accumulate_steps(self):
        self.assertEqual(self.samples("sleep")[-1]["steps"], 0)

    def test_stress_does_not_invent_hypoxia(self):
        self.assertTrue(all(sample["spo2"] > 96 for sample in self.samples("stress", 600)))

    def test_acceleration_moves_forward_faster(self):
        slow, fast = self.samples("mixed", 10, 1), self.samples("mixed", 10, 8)
        self.assertEqual(slow[-1]["phase"], "calm")
        self.assertEqual(fast[-1]["phase"], "active")
        self.assertEqual(fast[-1]["simulated_seconds"], slow[-1]["simulated_seconds"] * 8)

    def test_seed_is_reproducible(self):
        first, second = self.samples("mixed", 60), self.samples("mixed", 60)
        for a, b in zip(first, second):
            a.pop("ts"); b.pop("ts")
        self.assertEqual(first, second)

    def test_scenarios_separate_from_normal_activity(self):
        sleep, active = self.samples("sleep"), self.samples("activity")
        self.assertGreater(statistics.mean(s["hr"] for s in active), statistics.mean(s["hr"] for s in sleep) + 40)
        self.assertTrue(all(s["temperature"] > 39.1 for s in self.samples("fever")))
        self.assertTrue(all(s["spo2"] < 91 for s in self.samples("respiratory")))
        self.assertTrue(all(s["systolic"] < 90 for s in self.samples("hypotension")))

    def test_recovery_recognised_after_cumulative_steps(self):
        engine = backend.MetricsEngine()
        baseline = engine.baseline_rest_hr
        packet = engine.process({"hr": 72, "rr_ms": [810, 830, 815], "steps": 2000, "steps_delta": 0, "cadence": 0, "phase": "recovery"})
        self.assertGreater(packet["windows"]["baseline_rest_hr"], baseline)


class TriageRulesTests(unittest.TestCase):
    def payload(self, raw=None, **overrides):
        return {"motif": "Douleur abdominale", "symptoms": "Depuis deux heures", "temp": "N/A", "pain": "0/10", "red_flags": {}, "monitoring": {"fresh": True, "raw": raw or {}}} | overrides

    def test_pain_and_red_flag_priorities(self):
        for priority, pain, flags in [("P1", "0/10", {"chest": True}), ("P2", "7/10", {}), ("P3", "3,5/10", {}), ("P4", "0/10", {})]:
            self.assertEqual(fallback_triage(self.payload(pain=pain, red_flags=flags))["statut_tri"], priority)

    def test_normal_temperature_does_not_create_urgency(self):
        self.assertEqual(fallback_triage(self.payload(temp="36.7"))["statut_tri"], "P4")

    def test_activity_context_changes_interpretation(self):
        activity = self.payload({"hr": 118, "respiration": 22, "phase": "active"})
        resting = self.payload({"hr": 118, "respiration": 22, "phase": "calm"})
        self.assertEqual(fallback_triage(activity)["statut_tri"], "P4")
        self.assertEqual(fallback_triage(resting)["statut_tri"], "P2")

    def test_major_vital_abnormalities_escalate(self):
        result = fallback_triage(self.payload({"spo2": 88, "respiration": 29, "hr": 119, "phase": "respiratory"}))
        self.assertEqual(result["statut_tri"], "P1")
        self.assertGreaterEqual(len(result["alertes_constantes"]), 2)

    def test_missing_context_and_invalid_pain_require_review(self):
        self.assertEqual(fallback_triage(self.payload(motif="", symptoms=""))["statut_tri"], "P3")
        self.assertEqual(fallback_triage(self.payload(pain="100/10"))["statut_tri"], "P3")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="venu-health-test-")
        self.db_patch = patch.object(backend, "DB_PATH", Path(self.temp.name) / "test.db")
        self.source_patch = patch.object(backend, "DEFAULT_SOURCE", "sim")
        self.env_patch = patch.dict("os.environ", {"SIM_PROFILE": "calm", "SIM_SPEED": "1", "SIM_SEED": "42"})
        self.model_patch = patch.object(backend, "local_ollama_generate", side_effect=RuntimeError("offline test"))
        for item in (self.db_patch, self.source_patch, self.env_patch, self.model_patch):
            item.start()
        self.client = TestClient(backend.app).__enter__()
        self.latest()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for item in (self.model_patch, self.env_patch, self.source_patch, self.db_patch):
            item.stop()
        self.temp.cleanup()

    def latest(self, profile=None):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            snapshot = self.client.get("/api/metrics/latest").json()
            if snapshot.get("fresh") and (profile is None or snapshot["raw"]["sim_profile"] == profile):
                return snapshot
            time.sleep(0.025)
        self.fail("No fresh sample arrived")

    def analyse(self, **overrides):
        response = self.client.post("/api/triage/analyse", json={"motif": "Douleur abdominale", "symptoms": "Depuis 2 heures", "pain": "0/10"} | overrides)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def change_profile(self, profile):
        self.assertEqual(self.client.post("/api/sim/config", json={"profile": profile, "speed": 1}).status_code, 200)
        self.assertTrue(self.client.post("/api/source", json={"mode": "sim"}).json()["ok"])
        return self.latest(profile)

    def test_http_and_websocket_receive_same_latest_measurement(self):
        snapshot = self.latest()
        self.assertIsNotNone(snapshot["derived"]["rmssd"])
        for key in ("temperature", "systolic", "diastolic", "respiration", "phase"):
            self.assertIsNotNone(snapshot["raw"][key])
        with self.client.websocket_connect("/ws") as ws:
            packet = ws.receive_json()
            self.assertEqual(packet["measurement_id"], snapshot["measurement_id"])
        self.assertTrue(self.client.get("/api/status").json()["processor_running"])

    def test_model_receives_snapshot_and_cannot_invent_constants(self):
        prompts = []
        def fake_model(prompt):
            prompts.append(prompt)
            return json.dumps({"statut_tri": "P4", "constantes_injectees": {"tension": "999/999", "spo2": "100%"}, "motif_principal": "invented", "recommandation_action": "Orientation a confirmer", "justification_tri": ["Contexte pris en compte"]})
        with patch.object(backend, "local_ollama_generate", side_effect=fake_model):
            result = self.analyse()
        raw = result["contexte_mesures"]["raw"]
        self.assertEqual(result["constantes_injectees"]["frequence_cardiaque"], f"{raw['hr']} bpm")
        self.assertEqual(result["constantes_injectees"]["tension"], f"{raw['systolic']}/{raw['diastolic']} mmHg")
        self.assertEqual(result["motif_principal"], "Douleur abdominale")
        self.assertEqual(result["moteur_analyse"], "ollama")
        self.assertIn('"simulated": true', prompts[0])
        self.assertIn('"rmssd"', prompts[0])
        self.assertIn('"hr_1m"', prompts[0])

    def test_selected_acute_profiles_affect_analysis(self):
        for profile, priority in [("fever", "P2"), ("respiratory", "P1"), ("hypotension", "P1")]:
            with self.subTest(profile=profile):
                self.change_profile(profile)
                with patch.object(backend, "local_ollama_generate", return_value='{"statut_tri":"P4"}') as model:
                    result = self.analyse()
                self.assertEqual(result["statut_tri"], priority)
                if priority == "P1":
                    model.assert_not_called()
                    self.assertTrue(result["analyse_prioritaire_immediate"])
                else:
                    self.assertTrue(result["garde_fou_applique"])
                self.assertTrue(result["contexte_mesures"]["simulated"])

    def test_red_flag_alert_returns_without_waiting_for_model(self):
        with patch.object(backend, "local_ollama_generate") as model:
            result = self.analyse(red_flags={"chest": True})
        model.assert_not_called()
        self.assertEqual(result["statut_tri"], "P1")
        self.assertTrue(result["analyse_prioritaire_immediate"])
        self.assertIn(result["analyse_id"], backend.app.state.triage_results)

    def test_emergency_notification_uses_saved_snapshot_and_is_idempotent(self):
        result = self.analyse(red_flags={"breath": True})
        self.change_profile("activity")
        request = {"analyse_id": result["analyse_id"], "motif": "invented", "constantes": {"spo2": "100 %"}}
        first = self.client.post("/api/emergency/notify", json=request)
        self.assertEqual(first.status_code, 200)
        first = first.json()
        self.assertTrue(first["created"])
        self.assertEqual(first["alert"]["constantes"], result["constantes_injectees"])
        self.assertEqual(first["alert"]["motif"], result["motif_principal"])
        self.assertTrue(first["alert"]["simulated"])
        self.assertEqual(first["alert"]["channel"], "application")
        # Saved notifications survive analysis-cache expiry or restart.
        backend.app.state.triage_results.clear()
        retry = self.client.post("/api/emergency/notify", json=request).json()
        self.assertFalse(retry["created"])
        self.assertEqual(retry["alert"], first["alert"])
        history = self.client.get("/api/emergency/alerts").json()["alerts"]
        self.assertEqual(history, [first["alert"]])
        other_connection = backend.SQLiteSink(backend.DB_PATH)
        try:
            self.assertEqual(other_connection.emergency_history(), history)
        finally:
            other_connection.close()

    def test_notification_rejects_unknown_or_non_p1_analysis(self):
        ordinary = self.analyse()
        self.assertNotEqual(ordinary["statut_tri"], "P1")
        self.assertEqual(self.client.post("/api/emergency/notify", json={"analyse_id": ordinary["analyse_id"]}).status_code, 409)
        self.assertEqual(self.client.post("/api/emergency/notify", json={"analyse_id": str(uuid4())}).status_code, 404)

    def test_health_connect_registration_and_import_store_measurements(self):
        register = self.client.post(
            "/api/integrations/health-connect/register",
            json={"device_id": "android-garmin-01", "patient_id": "patient-42", "display_name": "Samsung Galaxy"},
        )
        self.assertEqual(register.status_code, 200, register.text)
        token = register.json()["token"]
        self.assertTrue(token)

        payload = {
            "device_id": "android-garmin-01",
            "patient_id": "patient-42",
            "source": "garmin_connect",
            "measurements": [
                {
                    "record_type": "HeartRateRecord",
                    "value": 72,
                    "unit": "bpm",
                    "start_time": "2026-10-11T08:00:00+00:00",
                    "end_time": "2026-10-11T08:00:01+00:00",
                    "source_app": "com.garmin.connect",
                    "metadata": {"sample_count": 1},
                },
                {
                    "record_type": "StepsRecord",
                    "value": 1180,
                    "unit": "count",
                    "start_time": "2026-10-11T07:55:00+00:00",
                    "end_time": "2026-10-11T08:00:00+00:00",
                    "source_app": "com.garmin.connect",
                },
            ],
        }
        response = self.client.post(
            "/api/integrations/health-connect/import",
            json=payload,
            headers={"X-Connector-Token": token},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["inserted"], 2)
        self.assertEqual(body["duplicate_count"], 0)
        self.assertTrue(body["patient_id"] == "patient-42")

        second = self.client.post(
            "/api/integrations/health-connect/import",
            json=payload,
            headers={"X-Connector-Token": token},
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["inserted"], 0)
        self.assertEqual(second.json()["duplicate_count"], 2)

    def test_garmin_dashboard_exposes_health_connect_snapshot(self):
        response = self.client.get("/api/integrations/health-connect/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("garmin_connected", data)
        self.assertIn("last_sync", data)
        self.assertIn("source", data)
        self.assertIn("is_enabled", data)
        self.assertEqual(self.client.post("/api/emergency/notify", json={"analyse_id": "bad"}).status_code, 422)
        self.assertEqual(self.client.get("/api/emergency/alerts").json()["alerts"], [])

    def test_model_p1_also_can_be_notified(self):
        with patch.object(backend, "local_ollama_generate", return_value='{"statut_tri":"P1"}'):
            result = self.analyse()
        self.assertEqual(result["statut_tri"], "P1")
        self.assertEqual(result["moteur_analyse"], "ollama")
        response = self.client.post("/api/emergency/notify", json={"analyse_id": result["analyse_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["alert"]["priority"], "P1")

    def test_emergency_contact_configuration(self):
        self.assertEqual(self.client.get("/api/emergency/config").json(), {"phone": backend.EMERGENCY_PHONE, "notification_channel": "application"})

    def test_consensus_status_never_returns_key_or_consumes_search(self):
        with patch.object(backend, "consensus_api_key", return_value="test-secret"), patch.object(consensus, "search_papers") as search:
            status = self.client.get("/api/consensus/status")
        self.assertEqual(status.json(), {"configured": True, "provider": "consensus"})
        self.assertNotIn("test-secret", status.text)
        search.assert_not_called()

    def test_consensus_question_only_sources_and_cached_retry(self):
        papers = {"results": [{"title": "Test study", "journal_name": "Example Journal", "publish_year": 2023, "takeaway": "Reported finding", "doi": "10.1000/example", "url": "javascript:alert(1)"}]}
        with patch.object(consensus, "api_key", return_value="test-secret"), patch.object(consensus, "search_papers", return_value=papers) as search:
            first = self.client.post("/api/consensus/chat", json={"question": "  creatine and   cognition  ", "monitoring": {"patient": "must never be forwarded"}})
            self.assertEqual(first.status_code, 200)
            search.assert_called_once_with("creatine and cognition", "test-secret")
            self.assertFalse(first.json()["cached"])
            self.assertEqual(first.json()["papers"][0]["url"], "https://doi.org/10.1000/example")
            retry = self.client.post("/api/consensus/chat", json={"question": "creatine and cognition"})
            self.assertTrue(retry.json()["cached"])
            self.assertEqual(search.call_count, 1)
            self.assertNotIn("test-secret", first.text)

    def test_consensus_missing_key_and_invalid_questions(self):
        with patch.object(consensus, "api_key", return_value=""), patch.object(consensus, "search_papers") as search:
            self.assertEqual(self.client.post("/api/consensus/chat", json={"question": "research question"}).status_code, 503)
            for question in ("", "   ", "x", "x" * 1001):
                self.assertEqual(self.client.post("/api/consensus/chat", json={"question": question}).status_code, 422)
        search.assert_not_called()

    def test_consensus_transport_header_and_safe_errors(self):
        with patch.object(consensus, "api_key", return_value="test-secret"), patch.object(consensus.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"results":[]}'
            response = self.client.post("/api/consensus/chat", json={"question": "does sleep help recovery?"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["papers"], [])
            request = opener.return_value.open.call_args.args[0]
            self.assertTrue(request.full_url.startswith("https://api.consensus.app/v1/search?"))
            self.assertIn("query=does+sleep+help+recovery%3F", request.full_url)
            self.assertEqual(request.get_header("X-api-key"), "test-secret")
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 25)
        for status, expected in ((401, 502), (402, 503), (429, 429), (500, 502)):
            with self.subTest(status=status), patch.object(consensus, "api_key", return_value="test-secret"), patch.object(consensus.urllib.request, "build_opener") as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError("https://api.consensus.app", status, "test-secret", None, None)
                response = self.client.post("/api/consensus/chat", json={"question": f"new uncached question {status}"})
                self.assertEqual(response.status_code, expected)
                self.assertNotIn("test-secret", response.text)
        with patch.object(consensus, "api_key", return_value="test-secret"), patch.object(consensus.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.URLError(TimeoutError())
            self.assertEqual(self.client.post("/api/consensus/chat", json={"question": "timeout question"}).status_code, 504)

    def test_consensus_invalid_reply_is_reported_without_made_up_sources(self):
        with patch.object(consensus, "api_key", return_value="test-secret"), patch.object(consensus.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"unexpected":[]}'
            response = self.client.post("/api/consensus/chat", json={"question": "invalid response question"})
            self.assertEqual(response.status_code, 502)
        self.assertIsNone(consensus.safe_link("javascript:alert(1)"))
        self.assertIsNone(consensus.safe_link("https://[invalid"))

    def test_simulation_continues_during_consensus_request(self):
        entered, release = threading.Event(), threading.Event()
        def slow_search(query, key):
            entered.set()
            release.wait(timeout=5)
            return {"results": []}
        with patch.object(consensus, "api_key", return_value="test-secret"), patch.object(consensus, "search_papers", side_effect=slow_search), ThreadPoolExecutor(max_workers=1) as executor:
            initial_id = self.latest()["measurement_id"]
            future = executor.submit(self.client.post, "/api/consensus/chat", json={"question": "sleep and recovery"})
            try:
                self.assertTrue(entered.wait(timeout=2))
                time.sleep(1.15)
                self.assertGreater(self.latest()["measurement_id"], initial_id)
                self.assertFalse(future.done())
            finally:
                release.set()
            self.assertEqual(future.result(timeout=2).status_code, 200)

    def test_manual_temperature_overrides_simulated_temperature(self):
        result = self.analyse(temp="39,5 \u00b0C")
        self.assertEqual(result["origine_temperature"], "saisie")
        self.assertEqual(result["constantes_injectees"]["temperature"], "39.5 \u00b0C")
        self.assertEqual(result["statut_tri"], "P2")

    def test_stale_measurements_are_excluded(self):
        old = copy.deepcopy(backend.app.state.latest_payload)
        old["ts"] = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        old["raw"]["spo2"] = 80
        backend.app.state.latest_payload = old
        result = self.analyse()
        self.assertFalse(result["contexte_mesures"]["fresh"])
        self.assertEqual(result["contexte_mesures"]["raw"], {})
        self.assertEqual(result["constantes_injectees"]["spo2"], "N/A")

    def test_ble_missing_values_stay_missing(self):
        packet = copy.deepcopy(backend.app.state.latest_payload)
        packet["source"] = "ble"
        packet["raw"] = {"hr": 72, "rr_ms": [], "spo2": None, "temperature": None}
        backend.app.state.active_source = "ble"
        backend.app.state.latest_payload = packet
        result = self.analyse()
        self.assertEqual(result["constantes_injectees"]["frequence_cardiaque"], "72 bpm")
        self.assertEqual(result["constantes_injectees"]["tension"], "N/A")
        self.assertEqual(result["constantes_injectees"]["spo2"], "N/A")
        self.assertFalse(result["contexte_mesures"]["simulated"])

    def test_invalid_model_reply_uses_local_rules(self):
        for reply in ('[]', '{"statut_tri":"P0"}', 'not json'):
            with patch.object(backend, "local_ollama_generate", return_value=reply):
                result = self.analyse(pain="7/10")
            self.assertEqual(result["statut_tri"], "P2")
            self.assertEqual(result["moteur_analyse"], "regles_locales")

    def test_simulation_continues_while_inference_is_waiting(self):
        entered, release = threading.Event(), threading.Event()
        def slow_model(prompt):
            entered.set()
            release.wait(timeout=5)
            return '{"statut_tri":"P4"}'
        with patch.object(backend, "local_ollama_generate", side_effect=slow_model), ThreadPoolExecutor(max_workers=1) as executor:
            initial_id = self.latest()["measurement_id"]
            future = executor.submit(self.analyse)
            try:
                self.assertTrue(entered.wait(timeout=2))
                time.sleep(1.15)
                started = time.monotonic()
                snapshot = self.latest()
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertGreater(snapshot["measurement_id"], initial_id)
                self.assertFalse(future.done())
            finally:
                release.set()
            result = future.result(timeout=2)
        self.assertEqual(result["contexte_mesures"]["measurement_id"], initial_id)

    def test_bad_config_is_rejected(self):
        for payload in ({"profile": "unknown"}, {"speed": 0}, {"speed": 99}):
            self.assertEqual(self.client.post("/api/sim/config", json=payload).status_code, 422)


if __name__ == "__main__":
    unittest.main()
