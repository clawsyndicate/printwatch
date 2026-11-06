# PrintWatch

A Hermes Agent plugin for controlling 3D printers over the local network — no cloud,
no rooting, stock firmware.

**Three protocol families:**
- **Anycubic Kobra** printers via the built-in **LAN Mode** API (reverse-engineered,
  clean-room implementation — handshake, MQTT telemetry, file management, print control,
  camera snapshots, and a full headless slice-to-print pipeline)
- **Bambu Lab** printers (X1/P1/A1 family) via their local MQTT/FTPS interface —
  status, control, file upload, RTSP snapshots; just host + serial + LAN access code
- **Klipper/Moonraker** printers via HTTP (standard open protocol)

## Why

Printer control shouldn't require a vendor cloud account. PrintWatch talks to your
printers the way your slicer does — over your own LAN — so an agent can answer
"how are the printers?", start a print from a file you send it, and push you a
photo when something needs eyes.

## Tools

| Tool | What it does |
|---|---|
| `printer_status` | Live status: temps, progress, layer, state across the fleet |
| `printer_files` | List files on printer storage |
| `printer_print` | Start a print by filename (or upload + start) |
| `printer_cancel` | Cancel the running job |
| `printer_pause_resume` | Pause / resume |
| `printer_snapshot` | Grab a camera frame and drop it in your chat |

## Headless slice-to-print (the fun part)

PrintWatch can take a raw `.gcode` from any slicer and repackage it into the exact
`.gcode.3mf` dialect Anycubic's firmware requires (see `kobra_dialect.py`) — header
blocks, `M75`, uppercase-MD5 sidecar, `X-ACNext` slice info — then upload it over
LAN and start the print. No Anycubic account, no Slicer Next, no hands.

## Security posture

- **Stock firmware only** — LAN Mode is a built-in feature; nothing is flashed or rooted
- **Local network only** — no cloud relay, no telemetry, credentials stay in your session
- Session tokens are per-session, in-memory, never logged or persisted

## Install

```
hermes plugins install <this-repo>
```

Requires Python 3.10+. Dependencies: `paho-mqtt`, `requests`, `cryptography`.

## License

MIT
