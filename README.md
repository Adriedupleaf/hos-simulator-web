# HOS Simulator Web Control

A small Python web app for controlling the HOS simulator via its local HTTP API.

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

Open **http://127.0.0.1:5000** in your browser.

## Features

- **Documentation** — parameter reference and example API URLs on the home page
- **Control panel** — fill in any subset of parameters and submit to the simulator
- **Persistence** — last submitted values are saved to `data/last_params.json` and restored on reload or a new browser session

## Simulator API

```
GET http://localhost:8080/simulator?param_1=value_1&...&param_n=value_n
```

| Parameter  | Type    | Range              |
|------------|---------|--------------------|
| `ignition` | integer | 0 or 1             |
| `rpm`      | integer | 0 – 3000           |
| `speed`    | integer | 0 – 160            |
| `odo`      | float   | 0 – 99999.99       |
| `hours`    | float   | 0 – 99999.99       |
| `vin`      | string  | VIN                |

Only parameters you provide are sent to the simulator.
