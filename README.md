# tapo_switch_api

An interactive CLI that runs a small HTTP server for switching TP-Link Tapo (and Kasa) smart plugs and switches **locally** using [python-kasa](https://github.com/python-kasa/python-kasa).

Your TP-Link account email and password are used only to authenticate the local KLAP/AES session that Tapo devices require. Commands go straight to the device on your LAN, and nothing is sent to TP-Link's cloud.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m tapo_switch_api
```

On newer firmware, open the Tapo app and turn on **Me → Third-Party Services → Third-Party Compatibility**. Without it, local control fails with authentication errors.

## First run

```
tapo> login                      # TP-Link account email + password (saved to config.json, mode 600)
tapo> discover                   # numbered list of devices on the LAN
#  Tapo name  Parent  Model  IP            State  Identifier
1  Desk Lamp          P110   192.168.1.42  off
2  Fan        Strip   P300   192.168.1.50  on
tapo> alias desk 1               # by number, IP, or Tapo name ("alias fan Fan")
tapo> on desk                    # control it from the CLI
tapo> token generate             # optional: require a token on HTTP requests
```

The server starts automatically on `0.0.0.0:8080`. `serve status` prints ready-to-use URLs. Type `help` to see every command.

## HTTP API

All endpoints are `GET`. When a token is set, send it as `?token=…`, `Authorization: Bearer …` or `X-API-Key: …`.

| Endpoint | Description |
|---|---|
| `/switch?id=<identifier>&state=on\|off\|toggle` | Switch a device |
| `/status?id=<identifier>` | Current state (omit `id` for all configured devices) |
| `/devices` | Configured identifiers |
| `/health` | Liveness check (no token needed) |

```sh
curl "http://192.168.1.10:8080/switch?id=desk&state=on&token=YOUR_TOKEN"
# {"ok": true, "id": "desk", "name": "Desk Lamp", "state": "on", "host": "192.168.1.42", "model": "P110"}
```

`id` matches an identifier you set with `alias` first. Failing that, it matches the device's name in the Tapo app, case-insensitively. Errors return JSON with status 400 (bad parameters), 401 (bad token), 404 (unknown device) or 502 (device unreachable).

> Some apps fetch link previews, and that can fire a GET request. Keep the server on your LAN and set a token if anything else on the network could hit it.

## How it works

- Each identifier is stored with the device's ID, last IP and connection parameters. A restart therefore reconnects in one round trip, without a broadcast.
- If a device's IP changes, the program finds the device again by its ID: first at the old address, then with a LAN broadcast. It then updates `config.json`.
- Each outlet on a power strip or multi-gang switch gets its own number in `discover` and can have its own identifier.
- Commands to the same device are serialized. A failed command is retried once on a fresh connection.

## Options

```
python -m tapo_switch_api [--config PATH] [--port N] [--no-server] [-v]
```

To change the bind address, discovery broadcast address or autostart, edit `config.json`. You can also point at a different config with the `TAPO_SWITCH_CONFIG` env var.

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```
