"""PrintWatch — Klipper/Moonraker + Anycubic LAN Mode tools for Hermes.

Ask your Hermes what the printers are doing, start/stop prints by name,
and monitor progress. Multi-printer aware.

Config (read through agent.secret_scope — never os.environ — inside Hermes):
  PRINTWATCH_PRINTERS       — JSON map of Anycubic LAN printers:
                              '{"kobra-1": {"host": "192.168.50.215"}, ...}'
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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

UPLOAD_SCHEMA = {
    "name": "printer_upload",
    "description": (
        "Upload a local .gcode file to a printer and start printing it. Anycubic: "
        "raw gcode is auto-wrapped into the printer's dialect package. Bambu: gcode "
        "is packed into a 3mf and uploaded over FTPS. Moonraker: uploaded to the "
        "gcodes root. WARNING: this starts a physical print — confirm with the user first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "printer": {"type": "string", "description": "Printer name."},
            "path": {
                "type": "string",
                "description": "Full local path to the .gcode file (e.g. /home/you/print.gcode).",
            },
            "auto_print": {
                "type": "boolean",
                "description": "Start the print after upload (default true). Set false to only upload.",
            },
        },
        "required": ["path"],
    },
}

CANCEL_SCHEMA = {
    "name": "printer_cancel",
    "description": "Cancel the current print job. Asks for your approval in chat before it runs.",
    "parameters": {
        "type": "object",
        "properties": {
            "printer": {"type": "string", "description": "Printer name."},
        },
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


def _secret(name: str) -> str:
    """Scoped credential read via agent.secret_scope (profile-aware, with the
    upstream env fall-through). Outside Hermes (standalone dev runs) falls back
    to the plain environment."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return os.environ.get(name, "").strip()
    return (get_secret(name) or "").strip()


def _plugin_data_dir() -> str:
    """The plugin's data dir via plugins.plugin_storage (follows the active Hermes
    profile); standalone dev falls back to ~/.hermes/plugin-data/printwatch."""
    try:
        from plugins.plugin_storage import plugin_data_dir
        return str(plugin_data_dir("printwatch"))
    except ImportError:
        base = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        out = os.path.join(base, "plugin-data", "printwatch")
        os.makedirs(out, exist_ok=True)
        return out


def _printers() -> dict[str, dict]:
    out: dict[str, dict] = {}
    raw = _secret("PRINTWATCH_PRINTERS")
    if raw:
        try:
            for name, cfg in json.loads(raw).items():
                out[name.strip()] = {**cfg, "kind": "anycubic"}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"PRINTWATCH_PRINTERS is not valid JSON: {e}") from e
    moon = _secret("PRINTWATCH_MOONRAKER_URL")
    if moon:
        out["moonraker"] = {"kind": "moonraker", "host": moon.rstrip("/")}
    raw_b = _secret("PRINTWATCH_BAMBU")
    if raw_b:
        try:
            for name, cfg in json.loads(raw_b).items():
                out[name.strip()] = {**cfg, "kind": "bambu"}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"PRINTWATCH_BAMBU is not valid JSON: {e}") from e
    if not out:
        raise RuntimeError(
            "No printers configured — set PRINTWATCH_PRINTERS (JSON map of "
            "Anycubic LAN printers), PRINTWATCH_BAMBU (JSON map with host/serial/"
            "access_code), or PRINTWATCH_MOONRAKER_URL"
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
    url = _secret("PRINTWATCH_MOONRAKER_URL").rstrip("/")
    if not url:
        raise RuntimeError("PRINTWATCH_MOONRAKER_URL is not set (e.g. http://<printer-ip>:7125)")
    return url


def _m_headers() -> dict:
    key = _secret("PRINTWATCH_MOONRAKER_API_KEY")
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


def _m_cancel() -> str:
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


# ================================================================ bambu handlers

def _bambu_mod():
    try:
        import bambu  # dev context (plugin dir on sys.path)
    except ImportError:
        from . import bambu  # installed-package context
    return bambu


def _bambu_args(cfg: dict) -> tuple[str, str, str]:
    for k in ("host", "serial", "access_code"):
        if not cfg.get(k):
            raise RuntimeError(f"PRINTWATCH_BAMBU entry missing {k!r}")
    return cfg["host"], cfg["serial"], cfg["access_code"]


def _bambu_status(cfg: dict) -> str:
    st = _bambu_mod().status(*_bambu_args(cfg))
    lines = [f"state: {st['state']}"]
    if st.get("progress") is not None:
        lines.append(f"progress: {st['progress']}%  layer: {st.get('layer')}/{st.get('total_layers')}")
    if st.get("nozzle_temp") is not None:
        lines.append(f"nozzle: {st['nozzle_temp']}C  bed: {st['bed_temp']}C"
                     + (f"  chamber: {st['chamber_temp']}C" if st.get("chamber_temp") else ""))
    if st.get("filename"):
        lines.append(f"job: {st['filename']}")
    if st.get("remain_time"):
        lines.append(f"remaining: {st['remain_time']}")
    return "\n".join(lines)


# ================================================================ dispatchers
#
# Hermes calls handlers as handler(args, **kwargs) where args is the dict of
# schema parameters. Handlers NEVER raise — every error comes back as a string
# so the agent sees it instead of a dead tool call.

def _err(e: Exception) -> str:
    return f"ERROR: {type(e).__name__}: {e}"


def _status(args: dict, **kwargs) -> str:
    try:
        printer = (args or {}).get("printer")
        return _status_impl(printer)
    except Exception as e:
        return _err(e)


def _status_impl(printer: str | None = None) -> str:
    printers = _printers()

    def _one_status(cfg):
        if cfg["kind"] == "anycubic":
            return _anycubic_status(cfg["host"])
        if cfg["kind"] == "bambu":
            return _bambu_status(cfg)
        return _m_status()

    if printer:
        _, cfg = _resolve(printer, printers)
        return _one_status(cfg)
    results = {}

    def one(item):
        name, cfg = item
        try:
            return name, _one_status(cfg)
        except Exception as e:
            return name, f"ERROR: {type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=4) as ex:
        for name, text in ex.map(one, printers.items()):
            results[name] = text
    return "\n\n".join(f"== {n} ==\n{t}" for n, t in results.items())


def _files(args: dict, **kwargs) -> str:
    try:
        return _files_impl((args or {}).get("printer"))
    except Exception as e:
        return _err(e)


def _files_impl(printer: str | None = None) -> str:
    printers = _printers()
    _, cfg = _resolve(printer, printers)
    if cfg["kind"] == "anycubic":
        return _anycubic_files(cfg["host"])
    if cfg["kind"] == "bambu":
        host, serial, code = _bambu_args(cfg)
        entries = _bambu_mod().list_files(host, serial, code)
        files = [e for e in entries if not e.get("is_dir")]
        if not files:
            return "storage: no files"
        return "\n".join(f"{e['filename']} ({e['size']/1e6:.1f} MB)" for e in files)
    return _m_files()


def _print_cmd(args: dict, **kwargs) -> str:
    try:
        a = args or {}
        return _print_cmd_impl(a.get("printer"), a.get("filename", ""))
    except Exception as e:
        return _err(e)


def _print_cmd_impl(printer: str | None = None, filename: str = "") -> str:
    printers = _printers()
    name, cfg = _resolve(printer, printers)
    if cfg["kind"] == "bambu":
        host, serial, code = _bambu_args(cfg)
        rep = _bambu_mod().print_control(host, serial, code, "start", filename or None)
        if rep.get("code") == 200:
            return f"Print started on {name}: {filename}"
        return (f"{name}: printer rejected the start "
                f"(result {rep.get('result')}: {rep.get('msg', '?')})")

    try:
        import anycubic
    except ImportError:
        from . import anycubic

    if cfg["kind"] == "anycubic":
        rep = anycubic.print_control(cfg["host"], "start", filename or None)
        if rep.get("code") == 200:
            return f"Print started on {name}: {filename}"
        return (f"{name}: printer rejected the start "
                f"(code {rep.get('code')}: {rep.get('msg', '?')})")
    return _m_print(filename)


def _cancel(args: dict, **kwargs) -> str:
    try:
        a = args or {}
        return _cancel_impl(a.get("printer"))
    except Exception as e:
        return _err(e)


def _cancel_impl(printer: str | None = None) -> str:
    printers = _printers()
    name, cfg = _resolve(printer, printers)
    if cfg["kind"] == "bambu":
        host, serial, code = _bambu_args(cfg)
        rep = _bambu_mod().print_control(host, serial, code, "stop")
        return f"{name}: print cancelled (result {rep.get('result')})."
    try:
        import anycubic
    except ImportError:
        from . import anycubic

    if cfg["kind"] == "anycubic":
        rep = anycubic.print_control(cfg["host"], "stop")
        return f"{name}: print cancelled (code {rep.get('code')})." if rep.get("code") == 200 \
            else f"{name}: cancel rejected (code {rep.get('code')})"
    return _m_cancel()


def _pause_resume(args: dict, **kwargs) -> str:
    try:
        a = args or {}
        return _pause_resume_impl(a.get("printer"), a.get("action", ""))
    except Exception as e:
        return _err(e)


def _pause_resume_impl(printer: str | None = None, action: str = "") -> str:
    printers = _printers()
    name, cfg = _resolve(printer, printers)
    action = (action or "").lower().strip()
    if action not in ("pause", "resume"):
        return "action must be 'pause' or 'resume'"
    if cfg["kind"] == "bambu":
        host, serial, code = _bambu_args(cfg)
        rep = _bambu_mod().print_control(host, serial, code, action)
        if rep.get("code") == 200:
            return f"{name}: print {action}d."
        return f"{name}: {action} rejected (result {rep.get('result')}: {rep.get('msg', '?')})"

    try:
        import anycubic
    except ImportError:
        from . import anycubic

    if cfg["kind"] == "anycubic":
        rep = anycubic.print_control(cfg["host"], action)
        if rep.get("code") == 200:
            return f"{name}: print {action}d."
        return f"{name}: {action} rejected (code {rep.get('code')}: {rep.get('msg', '?')})"
    return _m_pause_resume(action)


def _snapshot(args: dict, **kwargs) -> str:
    try:
        return _snapshot_impl((args or {}).get("printer"))
    except Exception as e:
        return _err(e)


def _snapshot_impl(printer: str | None = None) -> str:
    printers = _printers()
    name, cfg = _resolve(printer, printers)
    out = os.path.join(_plugin_data_dir(), "cam")
    os.makedirs(out, exist_ok=True)
    if cfg["kind"] == "bambu":
        host, serial, code = _bambu_args(cfg)
        return _bambu_mod().camera_snapshot(host, serial, code,
                                            os.path.join(out, f"{name}.jpg"))

    try:
        import anycubic
    except ImportError:
        from . import anycubic

    if cfg["kind"] != "anycubic":
        return "Snapshots: supported for Anycubic-LAN and Bambu (RTSP) printers."
    path = anycubic.camera_snapshot(cfg["host"], os.path.join(out, f"{name}.jpg"))
    return path


def _upload(args: dict, **kwargs) -> str:
    try:
        a = args or {}
        return _upload_impl(a.get("printer"), str(a.get("path", "")),
                            bool(a.get("auto_print", True)))
    except Exception as e:
        return _err(e)


def _upload_impl(printer: str | None = None, path: str = "", auto: bool = True) -> str:
    path = (path or "").strip()
    if not path:
        return "ERROR: path is required — give the full local path to a .gcode file."
    if not os.path.isfile(path):
        return f"ERROR: no such file: {path}"
    if not path.lower().endswith((".gcode", ".3mf")):
        return "ERROR: only .gcode (or packaged .3mf) files are supported."

    printers = _printers()
    name, cfg = _resolve(printer, printers)
    fname = Path(path).name
    uploaded = f"{name}: uploaded {fname}"

    if cfg["kind"] == "bambu":
        host, serial, code = _bambu_args(cfg)
        rep = _bambu_mod().upload_and_print(host, serial, code, path, print_after=auto)
        if not rep.get("uploaded"):
            return f"{name}: upload failed (size {rep.get('size', '?')})."
        if not auto:
            return uploaded + "."
        if rep.get("started"):
            return uploaded + " — print started."
        start = rep.get("start_report") or {}
        return (f"{name}: uploaded, but the start was rejected "
                f"(result {start.get('result')}: {start.get('msg', '?')}). "
                f"Use printer_print with filename={rep.get('filename')!r} to retry.")

    try:
        import anycubic
    except ImportError:
        from . import anycubic

    if cfg["kind"] == "anycubic":
        rep = anycubic.upload_and_print(cfg["host"], path, print_after=auto)
        if rep.get("code") == 200:
            return uploaded + (" — print started." if auto else ".")
        return (f"{name}: upload/start rejected "
                f"(code {rep.get('code')}: {rep.get('msg', '?')})")

    # moonraker
    with open(path, "rb") as fh:
        r = requests.post(f"{_m_base()}/server/files/upload",
                          files={"file": (fname, fh)},
                          headers=_m_headers(), timeout=120)
    r.raise_for_status()
    out = f"{name}: uploaded to gcodes root: {fname}"
    if auto:
        return out + "\n" + _m_print(fname)
    return out


# ================================================================ approval gate
#
# Tools that drive physical hardware (print start, upload+print, pause/resume,
# cancel) require a HUMAN approval inside Hermes on every call: this pre_tool_call
# hook returns {"action": "approve", "message": ...} and the runtime runs its own
# approval gate (once/session/always/deny; fails closed when no human is present,
# e.g. under cron). Read-only tools (status/files/snapshot) stay prompt-free.

_APPROVAL_TOOLS = frozenset(
    {"printer_print", "printer_upload", "printer_pause_resume", "printer_cancel"}
)


def _approval_printer_label(args: dict) -> str:
    name = (args.get("printer") or "").strip()
    if name:
        return name
    try:
        printers = _printers()
    except Exception:
        return "the printer"
    if len(printers) == 1:
        return next(iter(printers))
    return f"one of: {', '.join(sorted(printers))}"


def _on_pre_tool_call(tool_name: str = "", args: dict | None = None, **_) -> dict | None:
    """pre_tool_call hook — escalate hardware-moving tools to the human approval gate."""
    if tool_name not in _APPROVAL_TOOLS:
        return None
    a = args if isinstance(args, dict) else {}
    printer = _approval_printer_label(a)
    if tool_name == "printer_print":
        msg = f"Start print of {a.get('filename') or '<no filename>'} on {printer}?"
    elif tool_name == "printer_upload":
        fname = Path(str(a.get("path") or "")).name or "<no file>"
        auto = "" if a.get("auto_print", True) else " (upload only, no auto-print)"
        msg = f"Upload {fname} to {printer}{auto} and start the print?"
    elif tool_name == "printer_pause_resume":
        msg = f"{(a.get('action') or 'pause/resume').capitalize()} the print on {printer}?"
    else:  # printer_cancel
        msg = f"Cancel the current print on {printer}?"
    return {"action": "approve", "message": msg}


# ================================================================ register

def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_tool(name="printer_status", toolset="printwatch", schema=STATUS_SCHEMA, handler=_status)
    ctx.register_tool(name="printer_files", toolset="printwatch", schema=FILES_SCHEMA, handler=_files)
    ctx.register_tool(name="printer_print", toolset="printwatch", schema=PRINT_SCHEMA, handler=_print_cmd)
    ctx.register_tool(name="printer_upload", toolset="printwatch", schema=UPLOAD_SCHEMA, handler=_upload)
    ctx.register_tool(name="printer_cancel", toolset="printwatch", schema=CANCEL_SCHEMA, handler=_cancel)
    ctx.register_tool(name="printer_pause_resume", toolset="printwatch", schema=PAUSE_SCHEMA, handler=_pause_resume)
    ctx.register_tool(name="printer_snapshot", toolset="printwatch", schema=SNAPSHOT_SCHEMA, handler=_snapshot)
