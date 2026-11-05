"""PrintWatch — Klipper/Moonraker + Anycubic LAN Mode tools for Hermes.

Ask your Hermes what the printers are doing, start/stop prints by name,
and monitor progress. Multi-printer aware.

Config:
  PRINTWATCH_MOONRAKER_URL  — single Klipper/Moonraker printer (optional)
  PRINTWATCH_PRINTERS       — JSON map of Anycubic LAN printers:
                              '{"kobra-1": {"host": "192.168.50.215"}, ...}'
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

# ================================================================ schemas

STATUS_SCHEMA = {
    "name": "printer_status",
    "description": (
        "Get the current state of one or all configured 3D printers: print "
        "progress, temperatures, job phase, and errors. Call without arguments "
        "for a summary of every printer; pass printer for just one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "printer": {
                "type": "string",
                "description": "Optional printer name (e.g. 'kobra-1'). Omit for all printers.",
            },
        },
    },
}

FILES_SCHEMA = {
    "name": "printer_files",
    "description": (
        "List print jobs stored on a printer (Anycubic local storage or "
        "Klipper virtual SD). Use before printer_print to find exact names."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "printer": {
                "type": "string",
                "description": "Printer name (e.g. 'kobra-1'). Required if several are configured.",
            },
        },
    },
}

PRINT_SCHEMA = {
    "name": "printer_print",
    "description": "Start printing a file already on the printer, by name (as shown by printer_files).",
    "parameters": {
        "type": "object",
        "properties": {
            "printer": {"type": "string", "description": "Printer name."},
            "filename": {"type": "string", "description": "Exact file name/path as shown by printer_files."},
        },
        "required": ["filename"],
    },
}

CANCEL_SCHEMA = {
    "name": "printer_cancel",
    "description": "Cancel the current print job. ALWAYS confirm with the user first.",
    "parameters": {
        "type": "object",
        "properties": {
            "printer": {"type": "string", "description": "Printer name."},
            "confirm": {"type": "boolean", "description": "Must be true to actually cancel."},
        },
        "required": ["confirm"],
    },
}

PAUSE_SCHEMA = {
    "name": "printer_pause_resume",
    "description": "Pause or resume the current print.",
    "parameters": {
        "type": "object",
        "properties": {
            "printer": {"type": "string", "description": "Printer name."},
            "action": {"type": "string", "enum": ["pause", "resume"]},
        },
        "required": ["action"],
    },
}

SNAPSHOT_SCHEMA = {
    "name": "printer_snapshot",
    "description": (
        "Grab a camera snapshot from a printer. Returns the image file path — "
        "share it with the user via MEDIA: syntax. Fails cleanly if the "
        "printer has no camera."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "printer": {"type": "string", "description": "Printer name."},
        },
    },
}

# ================================================================ config


def _printers() -> dict[str, dict]:
    out: dict[str, dict] = {}
    raw = os.environ.get("PRINTWATCH_PRINTERS", "").strip()
    if raw:
        try:
            for name, cfg in json.loads(raw).items():
                out[name.strip()] = {**cfg, "kind": "anycubic"}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"PRINTWATCH_PRINTERS is not valid JSON: {e}") from e
    moon = os.environ.get("PRINTWATCH_MOONRAKER_URL", "").strip()
    if moon:
        out["moonraker"] = {"kind": "moonraker", "host": moon.rstrip("/")}
    if not out:
        raise RuntimeError(
            "No printers configured — set PRINTWATCH_PRINTERS (JSON map of "
            "Anycubic LAN printers) or PRINTWATCH_MOONRAKER_URL"
        )
    return out


def _resolve(name: str | None, printers: dict) -> tuple[str, dict]:
    if name:
        if name in printers:
            return name, printers[name]
        close = [n for n in printers if name.lower() in n.lower()]
        if len(close) == 1:
            return close[0], printers[close[0]]
        raise RuntimeError(f"unknown printer {name!r}; configured: {', '.join(printers)}")
    if len(printers) == 1:
        return next(iter(printers.items()))
    raise RuntimeError(
        f"several printers configured ({', '.join(printers)}) — pass printer=<name>"
    )


# ================================================================ anycubic handlers

def _anycubic_status(host: str) -> str:
    try:
        import anycubic  # dev context (plugin dir on sys.path)
    except ImportError:
        from . import anycubic  # installed-package context

    _, reports = anycubic.snapshot_reports(host, wait=4.0)
    info = next((r for r in reversed(reports) if r.get("type") == "info"), None)
    temp = next((r for r in reversed(reports) if r.get("type") == "tempature"), None)
    if not info:
        raise RuntimeError(f"{host}: connected but no info report arrived")
    d = info.get("data", {})
    name = d.get("printerName", host)
    state = d.get("state", "?")
    t = (temp or {}).get("data") or d.get("temp") or {}
    lines = [
        f"{name} [{state}]",
        f"  nozzle {t.get('curr_nozzle_temp', '?')}/{t.get('target_nozzle_temp', 0)}C — "
        f"bed {t.get('curr_hotbed_temp', '?')}/{t.get('target_hotbed_temp', 0)}C",
    ]
    project = d.get("project")
    # Cosmetic residue: a stopped job's envelope can linger as state "stopping"/
    # "stoped" while top-level state is "free" (verified on fleet 2026-11-05).
    # Top-level state is authoritative — free + stopping/stoped = idle.
    if project and not (state == "free" and project.get("state") in ("stopping", "stoped")):
        fn = (project.get("filename") or "?").rsplit("/", 1)[-1]
        remain = project.get("remain_time", 0) or 0
        remain_s = f", ~{remain // 60}m remaining" if remain else ""
        lines.append(
            f"  JOB: {fn} — {project.get('state', '?')}, {project.get('progress', 0)}% "
            f"({project.get('curr_layer', 0)}/{project.get('total_layers', 0)} layers{remain_s})"
        )
        if project.get("pause") == 1:
            lines.append("  NOTE: job is PAUSED — ask the user whether to resume or cancel")
    return "\n".join(lines)


def _anycubic_files(host: str) -> str:
    import uuid as _uuid

    try:
        import anycubic
    except ImportError:
        from . import anycubic

    s = anycubic.handshake(host)
    ps = anycubic.PrinterSession(s)
    try:
        if not ps.connected.wait(8):
            raise RuntimeError(f"{host}: broker connection failed")
        body = json.dumps({
            "type": "file", "action": "listLocal",
            "timestamp": int(time.time() * 1000),
            "msgid": _uuid.uuid4().hex, "data": {"path": "/"},
        })
        ps._c.publish(
            f"{anycubic.TOPIC_ROOT}/slicer/printer/{s.model_id}/{s.device_id}/file", body
        )
        time.sleep(3.5)
        reports = list(ps.reports)
    finally:
        ps.stop()
    rep = next((r for r in reports if r.get("type") == "file"), None)
    if not rep:
        raise RuntimeError(f"{host}: no file listing received")
    records = (rep.get("data") or {}).get("records") or []
    if not records:
        return f"{s.model_name or host}: local storage is empty."
    lines = [f"{s.model_name or host}: {len(records)} file(s):"]
    for rec in records[:15]:
        lines.append(f"  - {rec.get('filename')} ({rec.get('size', 0) / 1e6:.1f} MB)")
    return "\n".join(lines)


# ================================================================ moonraker handlers

def _m_base() -> str:
    url = os.environ.get("PRINTWATCH_MOONRAKER_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("PRINTWATCH_MOONRAKER_URL is not set (e.g. http://<printer-ip>:7125)")
    return url


def _m_headers() -> dict:
    key = os.environ.get("PRINTWATCH_MOONRAKER_API_KEY", "")
    return {"X-Api-Key": key} if key else {}


def _m_query(objects: str) -> dict:
    r = requests.get(f"{_m_base()}/printer/objects/query", params={"objects": objects},
                     headers=_m_headers(), timeout=5)
    r.raise_for_status()
    return r.json().get("result", {}).get("status", {})


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def _m_status() -> str:
    st = _m_query("print_stats display_status extruder heater_bed")
    ps = st.get("print_stats", {})
    state = ps.get("state", "unknown")
    msg = [f"Printer state: {state}"]
    if state == "printing":
        progress = float(st.get("display_status", {}).get("progress", 0) or 0)
        elapsed = float(ps.get("print_duration", 0) or 0)
        eta = ""
        if progress > 0.01:
            remaining = elapsed * (1 - progress) / progress
            eta = f", ETA {time.strftime('%H:%M', time.localtime(time.time() + remaining))}"
        msg.append(f"Printing {ps.get('filename', '?')} — {progress*100:.1f}% done"
                   f" ({_fmt_duration(elapsed)} elapsed{eta})")
    elif state == "error":
        msg.append(f"PRINT ERROR: {ps.get('message', 'unknown error')}")
    elif state == "paused":
        msg.append("Print PAUSED.")
    ext, bed = st.get("extruder", {}), st.get("heater_bed", {})
    msg.append(f"Nozzle {float(ext.get('temperature', 0) or 0):.0f}/"
               f"{float(ext.get('target', 0) or 0):.0f}C — "
               f"Bed {float(bed.get('temperature', 0) or 0):.0f}/"
               f"{float(bed.get('target', 0) or 0):.0f}C")
    note = st.get("display_status", {}).get("message")
    if note and note != "--":
        msg.append(f"Status: {note}")
    return "\n".join(msg)


def _m_files() -> str:
    r = requests.get(f"{_m_base()}/server/files/list", params={"root": "gcodes"},
                     headers=_m_headers(), timeout=5)
    r.raise_for_status()
    files = r.json().get("result", [])
    if not files:
        return "No print files on the printer."
    lines = [f"{len(files)} files on printer (newest first):"]
    for f in sorted(files, key=lambda x: x.get("modified", 0), reverse=True)[:15]:
        lines.append(f"- {f.get('path')} ({f.get('size', 0) / 1e6:.1f} MB)")
    if len(files) > 15:
        lines.append(f"... and {len(files) - 15} more")
    return "\n".join(lines)


def _m_print(filename: str) -> str:
    r = requests.post(f"{_m_base()}/printer/print/start", params={"filename": filename},
                      headers=_m_headers(), timeout=5)
    r.raise_for_status()
    return f"Print started: {filename}"


def _m_cancel(confirm: bool = False) -> str:
    if not confirm:
        return "Not cancelling — call again with confirm=true to stop the print."
    r = requests.post(f"{_m_base()}/printer/print/cancel", headers=_m_headers(), timeout=5)
    r.raise_for_status()
    return "Print cancelled."


def _m_pause_resume(action: str) -> str:
    action = action.lower().strip()
    if action not in ("pause", "resume"):
        return "action must be 'pause' or 'resume'"
    r = requests.post(f"{_m_base()}/printer/print/{action}", headers=_m_headers(), timeout=5)
    r.raise_for_status()
    return f"Print {action}d."


# ================================================================ dispatchers

def _status(printer: str = None) -> str:
    printers = _printers()
    if printer:
        _, cfg = _resolve(printer, printers)
        return _anycubic_status(cfg["host"]) if cfg["kind"] == "anycubic" else _m_status()
    results = {}

    def one(item):
        name, cfg = item
        try:
            return name, (_anycubic_status(cfg["host"]) if cfg["kind"] == "anycubic"
                          else _m_status())
        except Exception as e:
            return name, f"ERROR: {type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=4) as ex:
        for name, text in ex.map(one, printers.items()):
            results[name] = text
    return "\n\n".join(f"== {n} ==\n{t}" for n, t in results.items())


def _files(printer: str = None) -> str:
    printers = _printers()
    _, cfg = _resolve(printer, printers)
    if cfg["kind"] == "anycubic":
        return _anycubic_files(cfg["host"])
    return _m_files()


def _print_cmd(printer: str = None, filename: str = "") -> str:
    try:
        import anycubic
    except ImportError:
        from . import anycubic

    printers = _printers()
    name, cfg = _resolve(printer, printers)
    if cfg["kind"] == "anycubic":
        rep = anycubic.print_control(cfg["host"], "start", filename or None)
        if rep.get("code") == 200:
            return f"Print started on {name}: {filename}"
        return (f"{name}: printer rejected the start "
                f"(code {rep.get('code')}: {rep.get('msg', '?')})")
    return _m_print(filename)


def _cancel(printer: str = None, confirm: bool = False) -> str:
    try:
        import anycubic
    except ImportError:
        from . import anycubic

    printers = _printers()
    name, cfg = _resolve(printer, printers)
    if not confirm:
        return "Not cancelling — call again with confirm=true to stop the print."
    if cfg["kind"] == "anycubic":
        rep = anycubic.print_control(cfg["host"], "stop")
        return f"{name}: print cancelled (code {rep.get('code')})." if rep.get("code") == 200 \
            else f"{name}: cancel rejected (code {rep.get('code')})"
    return _m_cancel(confirm)


def _pause_resume(printer: str = None, action: str = "") -> str:
    try:
        import anycubic
    except ImportError:
        from . import anycubic

    printers = _printers()
    name, cfg = _resolve(printer, printers)
    action = (action or "").lower().strip()
    if action not in ("pause", "resume"):
        return "action must be 'pause' or 'resume'"
    if cfg["kind"] == "anycubic":
        rep = anycubic.print_control(cfg["host"], action)
        if rep.get("code") == 200:
            return f"{name}: print {action}d."
        return f"{name}: {action} rejected (code {rep.get('code')}: {rep.get('msg', '?')})"
    return _m_pause_resume(action)


def _snapshot(printer: str = None) -> str:
    import tempfile

    try:
        import anycubic
    except ImportError:
        from . import anycubic

    printers = _printers()
    name, cfg = _resolve(printer, printers)
    if cfg["kind"] != "anycubic":
        return "Snapshots are Anycubic-LAN only right now (Moonraker camera = roadmap)."
    out = os.path.join(tempfile.gettempdir(), "pw-cam")
    os.makedirs(out, exist_ok=True)
    path = anycubic.camera_snapshot(cfg["host"], os.path.join(out, f"{name}.jpg"))
    return path


# ================================================================ register

def register(ctx) -> None:
    ctx.register_tool(name="printer_status", toolset="printwatch", schema=STATUS_SCHEMA, handler=_status)
    ctx.register_tool(name="printer_files", toolset="printwatch", schema=FILES_SCHEMA, handler=_files)
    ctx.register_tool(name="printer_print", toolset="printwatch", schema=PRINT_SCHEMA, handler=_print_cmd)
    ctx.register_tool(name="printer_cancel", toolset="printwatch", schema=CANCEL_SCHEMA, handler=_cancel)
    ctx.register_tool(name="printer_pause_resume", toolset="printwatch", schema=PAUSE_SCHEMA, handler=_pause_resume)
    ctx.register_tool(name="printer_snapshot", toolset="printwatch", schema=SNAPSHOT_SCHEMA, handler=_snapshot)
