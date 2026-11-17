# PrintWatch

A Hermes Agent plugin for controlling 3D printers over the local network — no cloud,
no rooting, stock firmware.

*Maintained by the owner's Hermes agent under his direction.*

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
| `printer_print` | Start a print by filename (as listed by `printer_files`) |
| `printer_upload` | Send a local `.gcode` to the printer (auto-packaged into the printer's dialect) and optionally start it |
| `printer_cancel` | Cancel the running job (asks for your approval in chat first) |
| `printer_pause_resume` | Pause / resume |
| `printer_snapshot` | Grab a camera frame and drop it in your chat |
| `printer_slice` | Slice model files (.stl/.3mf/.obj) into a printable package on the agent host |

## Headless slice-to-print (the fun part)

**v0.5.0 — model files in, print out.** `printer_slice` takes `.stl`/`.3mf`/`.obj` file(s)
and slices them on the agent host with PrusaSlicer headless + a bundled Anycubic Kobra 3 V2
profile (converted from Anycubic's own Slicer Next preset — the machine's real start macro,
limits, and temps, never invented). It returns the printable package path plus layer count,
estimated time, and filament estimate. Multiple files slice as ONE combined plate. Requires
`prusa-slicer` on the agent host (`apt install prusa-slicer`).

`printer_upload` takes a raw `.gcode` (or the package printer_slice produced) and repackages
it into the exact `.gcode.3mf` dialect Anycubic's firmware requires (see `kobra_dialect.py`) —
header blocks, `M75`, uppercase-MD5 sidecar, `X-ACNext` slice info — then uploads it over
LAN and starts the print. Bambu `.gcode` files are packed into a 3mf the same way.
No Anycubic account, no Slicer Next, no hands.

Text your agent an STL; it prints. Slicing is host-side only (prompt-free) — the actual
hardware step stays behind the approval gate.

## Security posture

- **Stock firmware only** — LAN Mode is a built-in feature; nothing is flashed or rooted
- **Local network only** — no cloud relay, no telemetry
- **Hardware actions are approval-gated** — print/upload/pause/resume/cancel route through a `pre_tool_call` hook into Hermes's own human-approval gate (once/session/always/deny; fails closed when no human is present, e.g. under cron)
- **Credentials via `agent.secret_scope`** — config values are read through Hermes's
  scoped secret layer, never `os.environ`
- Session tokens are per-session, in-memory, never logged or persisted
- Bambu RTSP snapshots run ffmpeg with the access code in the local process argv for
  the ~2s capture window (local-only exposure; documented, error output scrubbed)

## Install

```
hermes plugins install <this-repo>
```

Requires Python 3.10+. Dependencies: `paho-mqtt`, `requests`, `cryptography`.

## License

MIT
