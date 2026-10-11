"""Correlated, seeded demonstration signals; these are never patient measurements."""
from __future__ import annotations

import asyncio
import math
import os
import random
from datetime import datetime, timezone
from typing import Any


# Target values for coherent adult demonstration scenarios. Blood pressure and
# temperature are synthetic scenario inputs, not measurements from Garmin BLE.
TARGETS = {
    "calm": dict(hr=66, spo2=98, respiration=16, temperature=36.7, systolic=119, diastolic=76, stress=12, hrv=42, cadence=0, battery=-0.025),
    "active": dict(hr=118, spo2=98, respiration=22, temperature=37.0, systolic=144, diastolic=78, stress=32, hrv=20, cadence=110, battery=-0.16),
    "stress": dict(hr=102, spo2=98, respiration=20, temperature=36.8, systolic=137, diastolic=87, stress=78, hrv=13, cadence=0, battery=-0.10),
    "recovery": dict(hr=76, spo2=98, respiration=17, temperature=36.8, systolic=122, diastolic=77, stress=19, hrv=36, cadence=0, battery=0.04),
    "sleep": dict(hr=54, spo2=97.6, respiration=13, temperature=36.4, systolic=108, diastolic=67, stress=7, hrv=52, cadence=0, battery=0.18),
    "fever": dict(hr=108, spo2=97, respiration=23, temperature=39.3, systolic=115, diastolic=72, stress=61, hrv=16, cadence=0, battery=-0.10),
    "respiratory": dict(hr=119, spo2=88.5, respiration=29, temperature=37.5, systolic=126, diastolic=79, stress=75, hrv=12, cadence=0, battery=-0.14),
    "hypotension": dict(hr=136, spo2=96.5, respiration=24, temperature=36.5, systolic=85, diastolic=54, stress=80, hrv=10, cadence=0, battery=-0.14),
}
PROFILES = ("mixed", "calm", "activity", "stress", "recovery", "sleep", "fever", "respiratory", "hypotension")


class SimulatedSource:
    def __init__(self, config: dict[str, Any], seed: int | None = None) -> None:
        self.config = config
        configured_seed = os.getenv("SIM_SEED") if seed is None else seed
        self.rng = random.Random(configured_seed)
        self._task: asyncio.Task | None = None
        self._running = False
        self._profile = ""
        self.elapsed = 0.0
        self.phase_elapsed = 0.0
        self.phase_index = 0
        self.beat_elapsed = 0.0
        self.beat_fraction = 0.0
        self.step_fraction = 0.0
        self.steps_total = 0
        self._reset()

    def _phases(self) -> list[tuple[str, float]]:
        if self._profile == "mixed":
            return [("calm", 70), ("active", 120), ("recovery", 150), ("stress", 70), ("recovery", 100), ("sleep", 240)]
        phase = "active" if self._profile == "activity" else self._profile
        return [(phase, 3600)]

    def _reset(self) -> None:
        self._profile = str(self.config.get("profile", "mixed"))
        if self._profile not in PROFILES:
            self._profile = "mixed"
        self.elapsed = self.phase_elapsed = self.beat_elapsed = 0.0
        self.phase_index = self.steps_total = 0
        self.beat_fraction = self.step_fraction = 0.0
        initial = TARGETS[self._phases()[0][0]]
        self.state = {key: float(initial[key]) + self.rng.uniform(-1, 1) * noise for key, noise in {
            "hr": 2, "spo2": 0.2, "respiration": 0.5, "temperature": 0.08,
            "systolic": 2, "diastolic": 1.5, "stress": 3,
        }.items()}
        self.body_battery = self.rng.uniform(60, 82) if self._profile not in {"fever", "respiratory", "hypotension", "stress"} else self.rng.uniform(35, 55)
        self.sleep_score = self.rng.randint(65, 88)
        self.first_sample = True

    def next_sample(self, real_seconds: float = 1.0) -> dict[str, Any]:
        if str(self.config.get("profile", "mixed")) != self._profile:
            self._reset()
        speed = max(0.25, min(8.0, float(self.config.get("speed", 1.0))))
        dt = real_seconds * speed
        phases = self._phases()
        self.phase_elapsed += dt
        while self.phase_elapsed >= phases[self.phase_index][1]:
            self.phase_elapsed -= phases[self.phase_index][1]
            self.phase_index = (self.phase_index + 1) % len(phases)
        phase = phases[self.phase_index][0]
        target = TARGETS[phase]
        self.elapsed += dt

        # Different response times avoid independent random jumps. Normal stress
        # preserves oxygenation; hypoxia is confined to the respiratory scenario.
        for key, tau, amplitude, noise in [
            ("hr", 14, 2.5, 0.35), ("spo2", 20, 0.12, 0.025),
            ("respiration", 16, 0.6, 0.07), ("temperature", 90, 0.03, 0.004),
            ("systolic", 25, 2.5, 0.25), ("diastolic", 25, 1.5, 0.18),
            ("stress", 20, 3.0, 0.4),
        ]:
            offset = math.sin(self.elapsed / 28 + len(key)) * amplitude
            alpha = 1 - math.exp(-dt / tau)
            self.state[key] += (target[key] + offset - self.state[key]) * alpha + self.rng.gauss(0, noise) * math.sqrt(dt)

        self.state["spo2"] = max(80, min(100, self.state["spo2"]))
        self.state["hr"] = max(40, min(180, self.state["hr"]))
        self.state["stress"] = max(0, min(100, self.state["stress"]))
        self.step_fraction += target["cadence"] * dt / 60
        steps_delta = int(self.step_fraction)
        self.step_fraction -= steps_delta
        self.steps_total += steps_delta
        self.body_battery = max(0, min(100, self.body_battery + target["battery"] * dt / 60))

        # RR intervals come from simulated individual beats and respiratory sinus
        # variation. Seed the first packet with recent beats to initialise HRV.
        self.beat_fraction += self.state["hr"] * real_seconds / 60
        count = int(self.beat_fraction)
        self.beat_fraction -= count
        if self.first_sample:
            count = max(count, 4)
        rr = []
        center = 60000 / self.state["hr"]
        for _ in range(count):
            self.beat_elapsed += center / 1000
            respiratory_wave = math.sin(self.beat_elapsed * self.state["respiration"] * 2 * math.pi / 60)
            interval = center + target["hrv"] * respiratory_wave + self.rng.gauss(0, target["hrv"] * 0.12)
            rr.append(round(max(300, min(1800, interval)), 1))
        self.first_sample = False
        return {
            "ts": datetime.now(timezone.utc).isoformat(), "source": "sim",
            "phase": phase, "sim_profile": self._profile, "simulated_seconds": round(self.elapsed, 1),
            "hr": round(self.state["hr"]), "rr_ms": rr,
            "spo2": round(self.state["spo2"], 1), "respiration": round(self.state["respiration"], 1),
            "temperature": round(self.state["temperature"], 2),
            "systolic": round(self.state["systolic"]), "diastolic": round(self.state["diastolic"]),
            "stress": round(self.state["stress"]), "body_battery": round(self.body_battery),
            "sleep_score": self.sleep_score, "steps": self.steps_total, "steps_delta": steps_delta,
            "cadence": target["cadence"], "intensity": "moderate" if phase == "active" else "low",
        }

    async def start(self, queue: asyncio.Queue) -> None:
        self._running = True

        async def runner() -> None:
            while self._running:
                await queue.put(self.next_sample())
                await asyncio.sleep(1)

        self._task = asyncio.create_task(runner(), name="simulated-signals")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
