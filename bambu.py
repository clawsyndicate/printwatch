"""Bambu Lab LAN adapter — status, files, upload, print control, snapshots.

Clean-room implementation from public community protocol documentation
(voska/bambu SPEC verified against X1C fw 01.12 2026-09; DMontgomery40/bambu-printer-mcp;
HA integration). Facts only — original Python code.

Wire format:
- MQTT mqtts://<host>:8883, user "bblp", password = LAN access code, self-signed cert.
  Subscribe device/<serial>/report, publish device/<serial>/request.
  Refresh: {"pushing":{"sequence_id":"0","command":"pushall"}}
- State: print.gcode_state (IDLE/PREPARE/RUNNING/PAUSE/FINISH/FAILED), mc_percent,
  mc_remaining_time, nozzle_temper, bed_temper, layer_num, total_layers...
- Commands: {"print":{"command":"pause"/"resume"/"stop","param":""}}
  Start: {"print":{"command":"project_file","param":"Metadata/plate_1.gcode",
  "url":"ftp:///<file>.gcode.3mf","subtask_name":...,"md5":...,"use_ams":false,...}}
- Files: implicit FTPS :990, user "bblp", password = access code. PBSZ 0 -> PROT P -> TYPE I.
- Camera: rtsps://bblp:<code>@<host>:322/stream
"""
from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
import uuid
import zipfile
from pathlib import Path

import requests  # noqa: F401  (kept for parity with anycubic adapter)
import paho.mqtt.client as mqtt


class BambuError(RuntimeError):
    pass


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # printer's self-signed cert; LAN-only by design
    return ctx


class BambuSession:
    """One MQTT session to a Bambu printer. Reports collect in .reports."""

    def __init__(self, host: str, serial: str, access_code: str, timeout: float = 8.0):
        self.host, self.serial, self.code = host, serial, access_code
        self.reports: list[dict] = []
        self.connected = threading.Event()
        self._c = mqtt.Client(
            client_id=f"printwatch-{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv311,
        )
        self._c.username_pw_set("bblp", access_code)
        self._c.tls_set_context(_tls_context())
        self._c.on_connect = self._on_connect
        self._c.on_message = self._on_message
        self._c.connect(host, 8883, keepalive=30)
        self._c.loop_start()
        if not self.connected.wait(timeout):
            self.stop()
            raise BambuError(f"{host}: MQTT connect failed")
        self._c.subscribe(f"device/{serial}/report", qos=0)
        self.push_all()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        self.connected.set()

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
            self.reports.append(data)
        except (ValueError, UnicodeDecodeError):
            pass

    def push_all(self) -> None:
        self._c.publish(f"device/{self.serial}/request",
                        json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))

    def request(self, payload: dict, timeout: float = 6.0) -> dict | None:
        """Publish a command; return the first matching report with the same sequence_id."""
        seq = payload.setdefault("print", {}).setdefault("sequence_id", str(int(time.time())))
        self._c.publish(f"device/{self.serial}/request", json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            for r in reversed(self.reports):
                p = r.get("print") or {}
                if str(p.get("sequence_id", "")) == str(seq) and p.get("command") != "pushall":
                    return r
            time.sleep(0.3)
        return None

    def latest_status(self, wait: float = 4.0) -> dict:
        """Wait for a report containing print state; return the print object."""
        deadline = time.time() + wait
        best: dict = {}
        while time.time() < deadline:
            for r in reversed(self.reports):
                p = r.get("print") or {}
                if "gcode_state" in p:
                    best = p
                    break
            if best:
                return best
            time.sleep(0.3)
        return best

    def stop(self) -> None:
        try:
            self._c.loop_stop()
            self._c.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------- status map

_GCODE_STATE_MAP = {
    "IDLE": "idle", "PREPARE": "preparing", "RUNNING": "printing",
    "PAUSE": "paused", "FINISH": "complete", "FAILED": "failed",
    "SLICING": "slicing", "SLICE_FAILED": "failed",
}


def status(host: str, serial: str, access_code: str) -> dict:
    """Normalized status dict for a Bambu printer."""
    s = BambuSession(host, serial, access_code)
    try:
        p = s.latest_status()
    finally:
        s.stop()
    state = _GCODE_STATE_MAP.get(str(p.get("gcode_state", "")).upper(), "unknown")
    remain = p.get("mc_remaining_time")
    if isinstance(remain, (int, float)):
        remain = f"{int(remain // 60)}m{int(remain % 60):02d}s"
    return {
        "kind": "bambu",
        "host": host,
        "serial": serial,
        "state": state,
        "progress": p.get("mc_percent"),
        "layer": p.get("layer_num"),
        "total_layers": p.get("total_layers"),
        "nozzle_temp": p.get("nozzle_temper"),
        "bed_temp": p.get("bed_temper"),
        "chamber_temp": p.get("chamber_temper"),
        "filename": p.get("gcode_file", ""),
        "remain_time": remain,
        "raw": {k: p.get(k) for k in
                ("gcode_state", "mc_percent", "mc_remaining_time", "print_error")},
    }


# ---------------------------------------------------------------- files (FTPS)

def _ftp_connect(host: str, access_code: str, timeout: float = 15.0):
    import ftplib

    ftp = ftplib.FTP_TLS(host=host, user="bblp", passwd=access_code, timeout=timeout)
    ftp.prot_p()          # PROT P after implicit TLS handshake
    ftp.voidcmd("TYPE I")
    return ftp


def list_files(host: str, serial: str, access_code: str, path: str = "/cache") -> list[dict]:
    ftp = _ftp_connect(host, access_code)
    try:
        entries: list[dict] = []

        def _line(line: str) -> None:
            parts = line.split(maxsplit=8)
            if len(parts) >= 9:
                entries.append({"filename": parts[8], "size": int(parts[4]),
                                "is_dir": parts[0].startswith("d")})
            else:
                entries.append({"filename": line, "size": 0, "is_dir": False})

        ftp.retrlines(f"LIST {path}", _line)
        return entries
    finally:
        ftp.quit()


def upload_file(host: str, serial: str, access_code: str, local_path: str,
                remote_name: str | None = None, remote_dir: str = "/cache") -> dict:
    """STOR a file to the printer SD card; verify size via LIST."""
    local = Path(local_path)
    remote_name = remote_name or local.name
    data = local.read_bytes()
    ftp = _ftp_connect(host, access_code)
    try:
        ftp.storbinary(f"STOR {remote_dir}/{remote_name}", local.open("rb"))
        sizes = {e["filename"]: e["size"] for e in
                 (list_files.__wrapped__(host, serial, access_code, remote_dir)
                  if hasattr(list_files, "__wrapped__") else list_files(host, serial, access_code, remote_dir))}
        remote_size = sizes.get(remote_name)
        return {"uploaded": remote_size == len(data), "filename": remote_name,
                "size": len(data), "remote_size": remote_size,
                "remote_path": f"{remote_dir}/{remote_name}"}
    finally:
        ftp.quit()


# ---------------------------------------------------------------- package (gcode -> 3mf)

def build_bambu_package(gcode_path: str, out_path: str, model_name: str = "print") -> str:
    """Wrap raw gcode into the minimal Bambu-shaped .gcode.3mf (plate_1.gcode + md5).
    Bambu Studio exports the same core structure; the printer's project_file command
    points at Metadata/plate_1.gcode inside it."""
    gcode = Path(gcode_path).read_text(errors="replace")
    md5 = hashlib.md5(gcode.encode()).hexdigest().upper()
    out = Path(out_path)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
            '<Default Extension="gcode" ContentType="text/plain"/>'
            '<Default Extension="md5" ContentType="text/plain"/>'
            '<Default Extension="config" ContentType="application/octet-stream"/>'
            '<Default Extension="png" ContentType="image/png"/></Types>')
        z.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xml:lang="en-US" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="1" type="model"><mesh>'
            '<vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>'
            '<vertex x="0" y="10" z="0"/></vertices>'
            '<triangles><triangle v1="0" v2="1" v3="2"/></triangles>'
            '</mesh></object></resources><build><item objectid="1"/></build></model>')
        z.writestr("Metadata/plate_1.gcode", gcode)
        z.writestr("Metadata/plate_1.gcode.md5", md5)
    return str(out)


# ---------------------------------------------------------------- print control

def print_control(host: str, serial: str, access_code: str,
                  action: str, filename: str | None = None) -> dict:
    """pause | resume | stop | start (start needs filename uploaded via upload_file)."""
    if action not in ("pause", "resume", "stop", "start"):
        raise BambuError(f"unknown action {action!r}")
    s = BambuSession(host, serial, access_code)
    try:
        if action == "start":
            if not filename:
                raise BambuError("start requires filename (uploaded via upload_file)")
            name = Path(filename).name
            payload = {"print": {
                "command": "project_file", "param": "Metadata/plate_1.gcode",
                "url": f"ftp:///{name}", "subtask_name": name, "file": name,
                "md5": "", "project_id": "0", "profile_id": "0",
                "task_id": "0", "subtask_id": "0",
                "bed_type": "auto", "timelapse": False,
                "bed_leveling": True, "flow_cali": True,
                "vibration_cali": True, "layer_inspect": True,
                "use_ams": False, "ams_mapping": [0, -1, -1, -1, -1],
            }}
        else:
            payload = {"print": {"command": action, "param": ""}}
        rep = s.request(payload)
        p = (rep or {}).get("print") or {}
        return {"code": 200 if p.get("result") in ("SUCCESS", None) else 500,
                "result": p.get("result"), "msg": p.get("message", ""),
                "state": _GCODE_STATE_MAP.get(str(p.get("gcode_state", "")).upper(),
                                              p.get("gcode_state", ""))}
    finally:
        s.stop()


def upload_and_print(host: str, serial: str, access_code: str, gcode_path: str,
                     print_after: bool = True) -> dict:
    """Raw gcode -> Bambu 3mf package -> FTPS upload -> project_file start."""
    src = Path(gcode_path)
    if src.suffix == ".gcode":
        pkg = src.parent / f"{src.stem}.gcode.3mf"
        build_bambu_package(str(src), str(pkg), model_name=src.stem)
        src = pkg
    up = upload_file(host, serial, access_code, str(src))
    result = {"uploaded": up["uploaded"], "filename": up["filename"], "size": up["size"]}
    if print_after:
        rep = print_control(host, serial, access_code, "start", filename=up["filename"])
        result["start_report"] = rep
        result["started"] = rep.get("code") == 200
    return result


# ---------------------------------------------------------------- camera

def camera_snapshot(host: str, serial: str, access_code: str, out_path: str,
                    settle: float = 2.0) -> str:
    """Grab a frame from the printer's RTSP stream via ffmpeg.

    Note: ffmpeg takes the RTSP url (which embeds the LAN access code) on its
    command line — it appears in the local process table for the duration of
    the capture only. Nothing logs the command; error output is scrubbed below.
    """
    import subprocess

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    url = f"rtsps://bblp:{access_code}@{host}:322/stream"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-rtsp_transport", "tcp", "-i", url,
           "-frames:v", "1", "-q:v", "3", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if not out.exists() or out.stat().st_size < 1000:
        err = (r.stderr or "").replace(access_code, "***")
        raise BambuError(f"camera capture failed: {err[-200:]}")
    return str(out)
