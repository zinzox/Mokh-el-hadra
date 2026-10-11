"""Lanceur local pour le MVP Garmin Venu.

Usage:
	python test.py

Notes:
	- SOURCE=sim (défaut) pour démo sans montre
	- SOURCE=ble + BLE_ADDR ou BLE_NAME pour connexion BLE Garmin
"""

import importlib
import os


if __name__ == "__main__":
	# Valeurs par défaut orientées Garmin Venu (utiles si le backend lit ces variables)
	os.environ.setdefault("SOURCE", "sim")
	os.environ.setdefault("BLE_NAME", "garmin")
	os.environ.setdefault("BLE_SCAN_TIMEOUT", "20")

	app_target = os.getenv("APP_TARGET", "app.main:app")
	uvicorn = importlib.import_module("uvicorn")
	uvicorn.run(app_target, host="127.0.0.1", port=8000, reload=True)
