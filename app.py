"""HOS Simulator control panel — a live, shared control surface for the simulator API."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)
# Pick up template edits without a restart, even with the debugger switched off.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Changes on every restart, so clients know when to stop trusting their own
# revision high-water mark and adopt whatever the server now says.
INSTANCE_ID = os.urandom(4).hex()

SIMULATOR_URL = os.environ.get("SIMULATOR_URL", "http://localhost:8080/simulator")
REQUEST_TIMEOUT = 10
HEARTBEAT_SECONDS = 15
BODY_LIMIT = 500
HISTORY_LIMIT = 8

DATA_DIR = Path(__file__).parent / "data"
STATE_FILE = DATA_DIR / "state.json"
LEGACY_STATE_FILE = DATA_DIR / "last_params.json"

TRUTHY = {"1", "true", "on", "yes"}
FALSY = {"0", "false", "off", "no"}

# Single source of truth: drives the form controls, the live tiles and server-side
# validation. `control` picks the widget; `min`/`max`/`step`/`maxlength` are rendered
# straight onto the input so the browser enforces the same limits the server does.
PARAMS = {
    "ignition": {
        "label": "Ignition",
        "control": "toggle",
        "type": "integer",
        "min": 0,
        "max": 1,
        "description": "Key position. Sends 1 when on, 0 when off.",
        "on_label": "On",
        "off_label": "Off",
    },
    "rpm": {
        "label": "RPM",
        "control": "range",
        "type": "integer",
        "min": 0,
        "max": 3000,
        "step": 1,
        "unit": "rpm",
        "description": "Engine revolutions per minute.",
    },
    "speed": {
        "label": "Speed",
        "control": "range",
        "type": "integer",
        "min": 0,
        "max": 160,
        "step": 1,
        "unit": "km/h",
        "description": "Road speed reported by the vehicle.",
    },
    # The simulator owns these two: it increments them on its own and only accepts an
    # override, after which it carries on counting from whatever we set. `counts_up`
    # says so in the UI; `steps` renders the nudge buttons.
    "odo": {
        "label": "Odometer",
        "control": "number",
        "type": "float",
        "min": 0,
        "max": 99999.99,
        "step": "any",
        "unit": "km",
        "description": "Overrides the counter — the simulator keeps counting up from here.",
        "counts_up": True,
        "steps": (-100, -10, 10, 100),
    },
    "hours": {
        "label": "Engine hours",
        "control": "number",
        "type": "float",
        "min": 0,
        "max": 99999.99,
        "step": "any",
        "unit": "h",
        "description": "Overrides the meter — the simulator keeps counting up from here.",
        "counts_up": True,
        "steps": (-100, -10, 10, 100),
    },
    "vin": {
        "label": "VIN",
        "control": "text",
        "type": "string",
        "maxlength": 17,
        "pattern": "[A-Za-z0-9]{1,17}",
        "description": "Vehicle identification number. Letters and digits only.",
    },
}

# Predefined profiles — one tap fills the form, the operator still presses Apply.
PROFILES = [
    {
        "id": "engine_off",
        "label": "Engine off",
        "hint": "Key out, everything at rest",
        "values": {"ignition": 0, "rpm": 0, "speed": 0},
    },
    {
        "id": "idling",
        "label": "Idling",
        "hint": "Key on, engine at idle",
        "values": {"ignition": 1, "rpm": 650, "speed": 0},
    },
    {
        "id": "city",
        "label": "City driving",
        "hint": "50 km/h, part throttle",
        "values": {"ignition": 1, "rpm": 1400, "speed": 50},
    },
    {
        "id": "highway",
        "label": "Highway cruise",
        "hint": "105 km/h, top gear",
        "values": {"ignition": 1, "rpm": 1650, "speed": 105},
    },
]

# Event profiles — ordered, multi-request sequences the server plays out on its own.
# Each step is one GET to the simulator; `delay_after` is the pause before the next
# step. A step repeats a parameter from the step before it when the simulator needs
# it carried over (power-up resends ignition alongside the new RPM). Sequences run on
# a background thread, so a step may sit idle for a minute without holding a request.
EVENTS = [
    {
        "id": "power_up",
        "label": "Power up",
        "hint": "Ignition on, then 600 rpm",
        "steps": [
            {"params": {"ignition": 1}, "delay_after": 1.0, "note": "ignition on"},
            {"params": {"ignition": 1, "rpm": 600}, "note": "600 rpm, ignition carried over"},
        ],
    },
    {
        "id": "power_off",
        "label": "Power off",
        "hint": "Roll to a stop, then engine and ignition off",
        "steps": [
            {"params": {"speed": 0}, "delay_after": 1.0, "note": "roll to a stop"},
            {"params": {"rpm": 0, "ignition": 0}, "note": "0 rpm and ignition off"},
        ],
    },
    {
        "id": "stop_resume",
        "label": "Stop & resume",
        "hint": "0 → 30 → 0 → 30 km/h, then stays at 30",
        "steps": [
            {"params": {"speed": 0}, "delay_after": 60.0, "note": "stopped for a minute"},
            {"params": {"speed": 30}, "delay_after": 10.0, "note": "moving at 30 km/h"},
            {"params": {"speed": 0}, "delay_after": 30.0, "note": "stopped again"},
            {"params": {"speed": 30}, "note": "back to 30 km/h and stays there"},
        ],
    },
    {
        "id": "traffic_crawl",
        "label": "Traffic crawl",
        "hint": "Stop-and-go at 5 km/h, engine idling",
        "steps": [
            {
                "params": {"ignition": 1, "rpm": 750, "speed": 5},
                "delay_after": 20.0,
                "note": "crawling at 5 km/h",
            },
            {"params": {"rpm": 650, "speed": 0}, "delay_after": 15.0, "note": "stopped in the queue"},
            {"params": {"rpm": 750, "speed": 5}, "delay_after": 25.0, "note": "crawling again"},
            {"params": {"rpm": 650, "speed": 0}, "delay_after": 10.0, "note": "stopped again"},
            {"params": {"rpm": 800, "speed": 5}, "note": "crawling at 5 km/h and stays there"},
        ],
    },
]

# Predefined VINs, offered as one-tap chips on the VIN field.
VIN_PRESETS = [
    {"label": "Truck 1", "sublabel": "Freightliner Cascadia", "vin": "3WKDP49X9KF300341"},
    {"label": "Truck 2", "sublabel": "Volvo VNL 760", "vin": "4V4NC9EH8DN563912"},
    {"label": "Truck 3", "sublabel": "Freightliner M2", "vin": "1FUJGHDV8CLBP8834"},
]


def _fmt_number(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _limit_text(meta: dict) -> str:
    """Human-readable limit, shown on the control itself."""
    if meta["control"] == "toggle":
        return f"{meta['on_label']} / {meta['off_label']}"
    if meta["type"] == "string":
        return f"up to {meta['maxlength']} characters"
    unit = f" {meta['unit']}" if meta.get("unit") else ""
    return f"{_fmt_number(meta['min'])} – {_fmt_number(meta['max'])}{unit}"


for _meta in PARAMS.values():
    _meta["limit"] = _limit_text(_meta)
    _meta["step_buttons"] = [
        {"delta": _delta, "label": ("+" if _delta > 0 else "−") + _fmt_number(abs(_delta))}
        for _delta in _meta.get("steps", ())
    ]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


class LiveState:
    """The configuration currently applied to the simulator, shared by every client.

    Every submit merges into one snapshot and bumps a revision, so a device that
    joins late — or that was not the one making the change — converges on the same
    view. Recent request/response exchanges ride along in the same broadcast. Waiters
    block on the condition variable until the revision moves, which is what the SSE
    stream rides on.
    """

    def __init__(self, path: Path, legacy_path: Path | None = None) -> None:
        self._path = path
        self._cond = threading.Condition()
        self._params: dict = {}
        self._revision = 0
        self._updated_at: float | None = None
        self._updated_by: dict | None = None
        self._changed: list[str] = []
        self._history: list[dict] = []
        self._running: dict | None = None
        self._load(legacy_path)

    def _load(self, legacy_path: Path | None) -> None:
        raw = _read_json(self._path)
        if raw is None and legacy_path is not None:
            legacy = _read_json(legacy_path)
            if isinstance(legacy, dict):
                raw = {"params": legacy}
        if not isinstance(raw, dict):
            return
        params = raw.get("params")
        if isinstance(params, dict):
            self._params = {k: v for k, v in params.items() if k in PARAMS}
        try:
            self._revision = int(raw.get("revision") or 0)
        except (TypeError, ValueError):
            self._revision = 0
        updated_at = raw.get("updated_at")
        self._updated_at = updated_at if isinstance(updated_at, (int, float)) else None
        updated_by = raw.get("updated_by")
        self._updated_by = updated_by if isinstance(updated_by, dict) else None

    def _event_locked(self, kind: str) -> dict:
        return {
            "kind": kind,
            "instance": INSTANCE_ID,
            "params": dict(self._params),
            "revision": self._revision,
            "updated_at": self._updated_at,
            "updated_by": dict(self._updated_by) if self._updated_by else None,
            "changed": list(self._changed),
            "history": list(self._history),
            "running": dict(self._running) if self._running else None,
            "server_time": time.time(),
        }

    def _persist_locked(self) -> None:
        payload = {
            "params": self._params,
            "revision": self._revision,
            "updated_at": self._updated_at,
            "updated_by": self._updated_by,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            app.logger.warning("Could not persist state to %s", self._path)

    def _record_locked(self, exchange: dict | None) -> None:
        if exchange is None:
            return
        self._history.insert(0, exchange)
        del self._history[HISTORY_LIMIT:]

    def snapshot(self, kind: str = "snapshot") -> dict:
        with self._cond:
            return self._event_locked(kind)

    def apply(self, params: dict, source: dict, exchange: dict | None = None) -> dict:
        """Merge a successful exchange into the shared state and wake every listener."""
        with self._cond:
            self._changed = [k for k, v in params.items() if self._params.get(k) != v]
            self._params.update(params)
            self._revision += 1
            self._updated_at = time.time()
            self._updated_by = source
            self._record_locked(exchange)
            self._persist_locked()
            event = self._event_locked("update")
            self._cond.notify_all()
            return event

    def set_running(self, running: dict | None) -> dict:
        """Publish event-sequence progress. Transient, so it is never persisted."""
        with self._cond:
            self._running = running
            self._changed = []
            self._revision += 1
            event = self._event_locked("update")
            self._cond.notify_all()
            return event

    def record_failure(self, exchange: dict) -> dict:
        """Publish a failed exchange without touching the applied configuration."""
        with self._cond:
            self._changed = []
            self._revision += 1
            self._record_locked(exchange)
            self._persist_locked()  # keep the stored revision level with what we broadcast
            event = self._event_locked("update")
            self._cond.notify_all()
            return event

    def wait(self, since_revision: int, timeout: float) -> dict | None:
        """Block until the revision moves past `since_revision`, or time out."""
        with self._cond:
            if self._revision != since_revision:
                return self._event_locked("update")
            self._cond.wait(timeout)
            if self._revision != since_revision:
                return self._event_locked("update")
            return None


live = LiveState(STATE_FILE, LEGACY_STATE_FILE)


def _as_bool(raw):
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    return None


def _coerce(meta: dict, raw) -> tuple[object | None, str | None]:
    """Turn one submitted value into the type the simulator expects."""
    if meta["control"] == "toggle":
        flag = _as_bool(raw)
        if flag is None:
            return None, f"{meta['label']}: must be on/off (1 or 0)."
        return int(flag), None

    text = str(raw).strip()

    if meta["type"] == "string":
        text = text.upper()
        if len(text) > meta["maxlength"]:
            return None, f"{meta['label']}: at most {meta['maxlength']} characters."
        if not text.isalnum():
            return None, f"{meta['label']}: letters and digits only."
        return text, None

    try:
        value = int(text) if meta["type"] == "integer" else round(float(text), 2)
    except ValueError:
        expected = "a whole number" if meta["type"] == "integer" else "a number"
        return None, f"{meta['label']}: must be {expected}."

    if value < meta["min"] or value > meta["max"]:
        return None, f"{meta['label']}: must be between {meta['min']} and {meta['max']}."
    return value, None


def parse_submitted_params(payload: dict) -> tuple[dict, list[str]]:
    """Extract and validate parameters from a submit payload."""
    parsed: dict = {}
    errors: list[str] = []

    for name, meta in PARAMS.items():
        if name not in payload:
            continue
        raw = payload[name]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        value, error = _coerce(meta, raw)
        if error:
            errors.append(error)
        else:
            parsed[name] = value

    if not parsed and not errors:
        errors.append("Provide at least one parameter to submit.")

    return parsed, errors


def _duration_label(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}m {rest}s" if rest else f"{minutes}m"


# Resolve the event definitions once, at import: a typo in a step is a startup failure
# rather than something that surfaces halfway through a running sequence.
for _event in EVENTS:
    if _event["id"] == "stop":
        raise RuntimeError("Event id 'stop' collides with the /api/events/stop route.")
    for _step in _event["steps"]:
        _parsed, _errors = parse_submitted_params(_step["params"])
        if _errors:
            raise RuntimeError(f"Event '{_event['id']}' has an invalid step: {_errors}")
        _step["parsed"] = _parsed
    _event["duration"] = sum(_step.get("delay_after") or 0 for _step in _event["steps"][:-1])
    _event["duration_label"] = _duration_label(_event["duration"])


def build_simulator_url(params: dict) -> str:
    return f"{SIMULATOR_URL}?{urlencode(params)}"


def _client_source(payload: dict) -> dict:
    client_id = str(payload.get("client_id") or request.headers.get("X-Client-Id") or "")[:64]
    return {"client_id": client_id, "address": request.remote_addr}


def _dispatch(params: dict, label: str, source: dict) -> tuple[dict, int | None]:
    """Make one simulator call. Returns the exchange record, plus a status on failure."""
    url = build_simulator_url(params)
    exchange = {
        "ok": False,
        "method": "GET",
        "url": url,
        "params": params,
        "label": label,
        "status": None,
        "body": "",
        "error": None,
        "at": time.time(),
        "source": source,
    }

    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.ConnectionError:
        exchange["error"] = (
            f"Could not connect to the simulator at {SIMULATOR_URL}. Is it running on port 8080?"
        )
        return exchange, 502
    except requests.Timeout:
        exchange["error"] = f"Simulator request timed out after {REQUEST_TIMEOUT}s."
        return exchange, 504
    except requests.RequestException as exc:
        exchange["error"] = f"Simulator request failed: {exc}"
        if getattr(exc, "response", None) is not None:
            exchange["status"] = exc.response.status_code
            exchange["body"] = exc.response.text[:BODY_LIMIT]
        return exchange, 502

    exchange["ok"] = True
    exchange["status"] = response.status_code
    exchange["body"] = response.text[:BODY_LIMIT]
    return exchange, None


class EventRunner:
    """Plays one event sequence at a time on a background thread.

    A sequence can span minutes, so the HTTP request only starts it; progress and
    completion reach every device through the same live-state broadcast as everything
    else. The pause between steps is a cancellable wait, so Stop takes effect at once
    instead of after the current delay.
    """

    def __init__(self, state: LiveState) -> None:
        self._state = state
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @staticmethod
    def _progress(event: dict, step: int, phase: str, next_at: float | None, source: dict) -> dict:
        return {
            "event": event["id"],
            "label": event["label"],
            "step": step,
            "total": len(event["steps"]),
            "phase": phase,
            "note": event["steps"][step - 1]["note"] if step else None,
            "next_at": next_at,
            "started_by": source,
        }

    def start(self, event: dict, source: dict) -> str | None:
        """Begin a sequence. Returns an error message, or None once it is under way."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return "Another event is already running."
            cancel = threading.Event()
            self._cancel = cancel
            self._thread = threading.Thread(
                target=self._run,
                args=(event, source, cancel),
                name=f"event-{event['id']}",
                daemon=True,
            )
            # Publish before starting, so the device that asked sees it immediately.
            self._state.set_running(self._progress(event, 0, "starting", None, source))
            self._thread.start()
        return None

    def cancel(self) -> bool:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False
            self._cancel.set()
        return True

    def _run(self, event: dict, source: dict, cancel: threading.Event) -> None:
        total = len(event["steps"])
        try:
            for index, step in enumerate(event["steps"], start=1):
                if cancel.is_set():
                    break

                self._state.set_running(self._progress(event, index, "sending", None, source))
                label = f"{event['label']} · step {index}/{total} — {step['note']}"
                exchange, failure = _dispatch(step["parsed"], label, source)
                if failure:
                    self._state.record_failure(exchange)
                    break
                self._state.apply(step["parsed"], source, exchange)

                delay = step.get("delay_after") or 0
                if not delay or index >= total:
                    continue
                self._state.set_running(
                    self._progress(event, index, "waiting", time.time() + delay, source)
                )
                if cancel.wait(delay):  # True means Stop was pressed
                    break
        finally:
            self._state.set_running(None)


runner = EventRunner(live)


def _sse(event: str, payload: dict, event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(payload)}")
    return "\n".join(lines) + "\n\n"


@app.route("/")
def index():
    snapshot = live.snapshot()
    return render_template(
        "index.html",
        params=PARAMS,
        profiles=PROFILES,
        events=EVENTS,
        vin_presets=VIN_PRESETS,
        state=snapshot,
        simulator_url=SIMULATOR_URL,
        # Everything the page's script needs, handed over as one JSON island.
        bootstrap={
            "params": PARAMS,
            "profiles": PROFILES,
            "events": EVENTS,
            "state": snapshot,
            "heartbeat": HEARTBEAT_SECONDS,
        },
    )


@app.route("/api/state", methods=["GET"])
def get_state():
    """Polling fallback for clients that cannot hold an SSE connection open."""
    return jsonify(live.snapshot())


@app.route("/api/stream", methods=["GET"])
def stream_state():
    """Server-sent events: one `state` event per change, plus periodic `ping`."""
    resume_from = request.headers.get("Last-Event-ID") or request.args.get("since")
    try:
        since = int(resume_from)
    except (TypeError, ValueError):
        since = None

    def events():
        snapshot = live.snapshot()
        revision = snapshot["revision"]
        if since != revision:
            yield _sse("state", snapshot, revision)
        while True:
            update = live.wait(revision, HEARTBEAT_SECONDS)
            if update is None:
                yield _sse("ping", {"server_time": time.time()})
                continue
            revision = update["revision"]
            yield _sse("state", update, revision)

    return Response(
        events(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/submit", methods=["POST"])
def submit():
    payload = request.get_json(silent=True) if request.is_json else None
    if payload is None:
        payload = request.form.to_dict()
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "errors": ["Expected a JSON object of parameters."]}), 400

    source = _client_source(payload)
    parsed, errors = parse_submitted_params(payload)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    exchange, failure = _dispatch(parsed, "Manual apply", source)
    if failure:
        state = live.record_failure(exchange)
        return jsonify(
            {
                "ok": False,
                "errors": [exchange["error"]],
                "url": exchange["url"],
                "exchanges": [exchange],
                "state": state,
            }
        ), failure

    state = live.apply(parsed, source, exchange)
    return jsonify(
        {
            "ok": True,
            "params": parsed,
            "url": exchange["url"],
            "simulator_status": exchange["status"],
            "simulator_body": exchange["body"],
            "exchanges": [exchange],
            "state": state,
        }
    )


@app.route("/api/events/stop", methods=["POST"])
def stop_event():
    """Cancel the running sequence. The current step's pause is interrupted at once."""
    stopped = runner.cancel()
    return jsonify(
        {
            "ok": stopped,
            "errors": [] if stopped else ["No event is running."],
            "state": live.snapshot(),
        }
    ), (200 if stopped else 409)


@app.route("/api/events/<event_id>", methods=["POST"])
def run_event(event_id: str):
    """Start a predefined sequence. It plays out in the background, one call per step."""
    event = next((item for item in EVENTS if item["id"] == event_id), None)
    if event is None:
        return jsonify({"ok": False, "errors": [f"Unknown event '{event_id}'."]}), 404

    payload = request.get_json(silent=True) or {}
    error = runner.start(event, _client_source(payload))
    if error:
        return jsonify(
            {"ok": False, "event": event_id, "errors": [error], "state": live.snapshot()}
        ), 409

    return jsonify(
        {
            "ok": True,
            "event": event_id,
            "steps": len(event["steps"]),
            "duration": event["duration"],
            "state": live.snapshot(),
        }
    ), 202


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "6969"))
    debug = os.environ.get("DEBUG", "").strip().lower() in TRUTHY

    if debug and host not in {"127.0.0.1", "localhost"}:
        # The Werkzeug debugger is remote code execution for anyone who can reach it.
        raise SystemExit("Refusing to run with DEBUG=1 on a non-loopback host. Set HOST=127.0.0.1.")

    print(f" * Control panel on http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    print(f" * Reachable from other devices on this network at http://<this-machine-ip>:{port}")
    app.run(host=host, port=port, debug=debug, threaded=True)
