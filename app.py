"""HOS Simulator control panel — web UI for the localhost:8080 simulator API."""

import json
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

SIMULATOR_URL = "http://localhost:8080/simulator"
STATE_FILE = Path(__file__).parent / "data" / "last_params.json"

PARAMS = {
    "ignition": {
        "label": "Ignition",
        "type": "integer",
        "description": "Set the ignition on or off.",
        "min": 0,
        "max": 1,
        "step": 1,
        "placeholder": "0 or 1",
    },
    "rpm": {
        "label": "RPM",
        "type": "integer",
        "description": "Set the RPM value.",
        "min": 0,
        "max": 3000,
        "step": 1,
        "placeholder": "0 – 3000",
    },
    "speed": {
        "label": "Speed",
        "type": "integer",
        "description": "Set the speed value (km/h or mph depending on simulator).",
        "min": 0,
        "max": 160,
        "step": 1,
        "placeholder": "0 – 160",
    },
    "odo": {
        "label": "Odometer",
        "type": "float",
        "description": "Set the start odometer value.",
        "min": 0,
        "max": 99999.99,
        "step": 0.01,
        "placeholder": "0 – 99999.99",
    },
    "hours": {
        "label": "Engine Hours",
        "type": "float",
        "description": "Set the start engine hours value.",
        "min": 0,
        "max": 99999.99,
        "step": 0.01,
        "placeholder": "0 – 99999.99",
    },
    "vin": {
        "label": "VIN",
        "type": "string",
        "description": "Set the vehicle identification number.",
        "placeholder": "e.g. 3WKDP49X9KF300341",
    },
}


def load_last_params() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_last_params(params: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(params, indent=2), encoding="utf-8")


def parse_submitted_params(form_data) -> tuple[dict, list[str]]:
    """Extract and validate parameters from form submission."""
    parsed = {}
    errors = []

    for name, meta in PARAMS.items():
        raw = form_data.get(name, "").strip()
        if not raw:
            continue

        if meta["type"] == "integer":
            try:
                value = int(raw)
            except ValueError:
                errors.append(f"{meta['label']}: must be an integer.")
                continue
            if value < meta["min"] or value > meta["max"]:
                errors.append(
                    f"{meta['label']}: must be between {meta['min']} and {meta['max']}."
                )
                continue
            parsed[name] = value

        elif meta["type"] == "float":
            try:
                value = float(raw)
            except ValueError:
                errors.append(f"{meta['label']}: must be a number.")
                continue
            if value < meta["min"] or value > meta["max"]:
                errors.append(
                    f"{meta['label']}: must be between {meta['min']} and {meta['max']}."
                )
                continue
            parsed[name] = value

        else:
            parsed[name] = raw

    if not parsed:
        errors.append("Provide at least one parameter to submit.")

    return parsed, errors


def build_simulator_url(params: dict) -> str:
    query = "&".join(f"{key}={requests.utils.quote(str(value))}" for key, value in params.items())
    return f"{SIMULATOR_URL}?{query}"


@app.route("/")
def index():
    last_params = load_last_params()
    return render_template(
        "index.html",
        params=PARAMS,
        last_params=last_params,
        simulator_url=SIMULATOR_URL,
    )


@app.route("/api/last-params", methods=["GET"])
def get_last_params():
    return jsonify(load_last_params())


@app.route("/api/submit", methods=["POST"])
def submit():
    if request.is_json:
        form_data = request.get_json(silent=True) or {}
    else:
        form_data = request.form

    parsed, errors = parse_submitted_params(form_data)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    url = build_simulator_url(parsed)

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.ConnectionError:
        return jsonify(
            {
                "ok": False,
                "errors": [
                    "Could not connect to the simulator at "
                    f"{SIMULATOR_URL}. Is it running on port 8080?"
                ],
                "url": url,
            }
        ), 502
    except requests.Timeout:
        return jsonify(
            {
                "ok": False,
                "errors": ["Simulator request timed out."],
                "url": url,
            }
        ), 504
    except requests.RequestException as exc:
        return jsonify(
            {
                "ok": False,
                "errors": [f"Simulator request failed: {exc}"],
                "url": url,
            }
        ), 502

    save_last_params(parsed)

    return jsonify(
        {
            "ok": True,
            "params": parsed,
            "url": url,
            "simulator_status": response.status_code,
            "simulator_body": response.text[:2000],
        }
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
