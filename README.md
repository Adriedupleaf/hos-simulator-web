# HOS Simulator Web Control

A Python web app for driving the HOS simulator through its local HTTP API — with a live
view of the applied configuration that every connected device shares.

## Prerequisites

- Python 3.10+
- The HOS simulator running with its API on `http://localhost:8080`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Open **http://127.0.0.1:5000**. The app binds `0.0.0.0` by default, so a second laptop on
the same network can open `http://<this-machine-ip>:5000` and control the same simulator —
both devices see each other's changes as they happen.

| Variable        | Default                            | Notes                                      |
|-----------------|------------------------------------|--------------------------------------------|
| `HOST`          | `0.0.0.0`                          | Use `127.0.0.1` to keep it on this machine |
| `PORT`          | `5000`                             |                                            |
| `SIMULATOR_URL` | `http://localhost:8080/simulator`  |                                            |
| `DEBUG`         | off                                | Only allowed with `HOST=127.0.0.1`         |

`DEBUG=1` enables the Werkzeug debugger, which executes arbitrary code for anyone who can
reach it, so the app refuses to start with it on unless the host is loopback. Template
edits reload without it.

## The interface

**Live configuration** — a strip across the page header showing the parameters currently
applied, updated the instant anyone submits from any device. The header is sticky, so the
values stay in view while you work the controls. Beside the title, a pill reports whether
you are seeing live updates:

| Indicator                     | Meaning                                            |
|-------------------------------|----------------------------------------------------|
| `Live`                        | Streaming over server-sent events                  |
| `Live · polling`              | Stream unavailable, refreshing every 3s instead    |
| `Offline · last known values` | Server unreachable — what you see may be stale     |

When another device changes something, the affected tiles flash and a note names the
device. Fields you are mid-edit are never overwritten: they get flagged with the live value
and a **use** button, and **Use live values** discards all local edits at once.

**Request & response** — the last 8 calls to the simulator, newest first: the exact URL
sent, the reply, and which device triggered it. Failures are recorded too. It is shared, so
you also see calls made from other devices.

**Events** — multi-step sequences the server plays out on its own, one request per step:

| Event     | Step 1       | Wait | Step 2                |
|-----------|--------------|------|-----------------------|
| Power up  | `ignition=1` | 1s   | `ignition=1&rpm=600`  |
| Power off | `speed=0`    | 1s   | `rpm=0&ignition=0`    |

Power-up step 2 resends `ignition` alongside the new RPM, as the simulator expects. If a
step fails the sequence stops there and the failure lands in Request & response.

**Profiles** — fill the form in one tap (engine off, idling, city driving, highway cruise);
you still press Apply. **VIN presets** — three predefined VINs as one-tap chips.

**Parameters** — each control carries its own limits: ignition is a switch, RPM and speed
are sliders bound to number boxes, and every range is enforced by the browser *and*
re-validated on the server. Empty fields are left untouched.

Odometer and engine hours are counters the simulator increments on its own — it only
accepts an override, and then carries on counting from the value you set. Each has
**−100 / −10 / +10 / +100** buttons that offset the box's current contents, falling back to
the last value applied when the box is empty, clamped to the field's range. Nothing is sent
until you press Apply.

## This app's HTTP API

| Method | Path               | Purpose                                                  |
|--------|--------------------|----------------------------------------------------------|
| `GET`  | `/`                | The control panel                                        |
| `GET`  | `/api/state`       | Current shared state (polling fallback)                  |
| `GET`  | `/api/stream`      | Server-sent events: `state` per change, `ping` every 15s |
| `POST` | `/api/submit`      | JSON body of parameters → one simulator call             |
| `POST` | `/api/events/<id>` | Run `power_up` or `power_off`                            |

## Simulator API

```
GET http://localhost:8080/simulator?param_1=value_1&...&param_n=value_n
```

| Parameter  | Type    | Range               |
|------------|---------|---------------------|
| `ignition` | integer | 0 or 1              |
| `rpm`      | integer | 0 – 3000            |
| `speed`    | integer | 0 – 160             |
| `odo`      | float   | 0 – 99999.99        |
| `hours`    | float   | 0 – 99999.99        |
| `vin`      | string  | VIN, up to 17 chars |

Only parameters you provide are sent to the simulator.

## Notes

- The simulator API is write-only, so "live configuration" is what *this panel* has
  applied. Anything sent to the simulator by bypassing the panel is invisible to it.
- Applied parameters survive a restart via `data/state.json`; the exchange log is
  in-memory only.
- State and the event stream live in process memory, so run a single process — multiple
  workers would each keep their own view.
