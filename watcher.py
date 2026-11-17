"""PrintWatch fleet watcher (v0.6.0 dev).

Long-running monitor for LAN-Mode Anycubic printers. Polls each printer's
telemetry on an interval, tracks job transitions, and emits events to a
configurable sink (default: webhook POSTs to the Hermes webhook endpoint,
which routes them to chat). Host-side only — NEVER issues printer commands;
the watcher observes, humans decide.

Event design (fleet lessons baked in):
- print_done    — project reached 100% or vanished with printer state "free"
                  (stopping/stoped residue is idle, not an event — verified
                  fleet behavior 2026-11-05)
- print_failed  — the pause:3 phantom-filament stall signature: progress
                  frozen AND pause==3 (known bad-dialect failure mode; the
                  printer self-pauses into endless "changing filament")
- print_paused  — pause==1 human pause (report once per job, not per poll)
- printer_offline — handshake fails N consecutive polls
- printer_back  — printer returned after being offline

State machine per printer: last project identity = (filename, start-epoch).
Job identity changes reset the notification-once latches.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

try:
    import anycubic  # dev context (plugin dir on sys.path)
except ImportError:
    from . import anycubic  # installed-package context

RESIDUE_STATES = {"stopping", "stoped"}  # cosmetic residue = idle (fleet-verified)
STALL_GRACE_POLLS = 3  # consecutive frozen polls with pause==3 before alerting


def _load_printers(config_path: Path) -> dict[str, dict]:
    """Config = JSON map name -> {"host": ...} (same shape as the plugin env)."""
    cfg = json.loads(Path(config_path).read_text())
    out = {}
    for name, entry in cfg.items():
        host = (entry or {}).get("host")
        if host:
            out[name] = {"host": host}
    if not out:
        raise ValueError(f"no printers with a host in {config_path}")
    return out


def _poll_one(host: str) -> dict:
    """One telemetry snapshot. Returns a normalized dict; raises on failure."""
    _, reports = anycubic.snapshot_reports(host, wait=4.0)
    info = next((r for r in reversed(reports) if r.get("type") == "info"), None)
    if not info:
        raise RuntimeError(f"{host}: connected but no info report arrived")
    d = info.get("data", {})
    state = d.get("state", "?")
    project = d.get("project") or {}
    residue = state == "free" and project.get("state") in RESIDUE_STATES
    return {
        "printer_state": state,
        "job_active": bool(project) and not residue,
        "filename": (project.get("filename") or "").rsplit("/", 1)[-1],
        "job_state": project.get("state", ""),
        "progress": int(project.get("progress", 0) or 0),
        "pause": int(project.get("pause", 0) or 0),
        "remain_min": int(project.get("remain_time", 0) or 0) // 60,
    }


class PrinterWatch:
    def __init__(self, name: str, host: str, alert_offline: bool = False):
        self.name = name
        self.host = host
        self.alert_offline = alert_offline
        self.offline_polls = 0
        self.was_offline = False
        # job identity / latches
        self.job_key: tuple = ()
        self.notified_done = False
        self.notified_failed = False
        self.notified_paused = False
        self.stall_count = 0

    def poll(self) -> list[dict]:
        """One poll cycle. Returns a list of event dicts (may be empty)."""
        try:
            snap = _poll_one(self.host)
        except Exception as e:
            self.offline_polls += 1
            if (self.offline_polls >= 3 and not self.was_offline
                    and self.alert_offline):
                self.was_offline = True
                return [{"event": "printer_offline", "printer": self.name,
                         "host": self.host, "detail": f"unreachable x{self.offline_polls} ({type(e).__name__})"}]
            if self.offline_polls >= 3 and not self.was_offline:
                self.was_offline = True  # stay quiet, but remember for printer_back
            return []
        events: list[dict] = []
        if self.was_offline:
            self.was_offline = False
            self.offline_polls = 0
            if self.alert_offline:
                events.append({"event": "printer_back", "printer": self.name, "host": self.host})
        self.offline_polls = 0

        key = (snap["filename"], snap["job_state"])
        if key != self.job_key:
            # new or changed job: reset latches
            self.job_key = key
            self.notified_done = self.notified_failed = self.notified_paused = False
            self.stall_count = 0

        if not snap["job_active"]:
            return events

        if snap["pause"] == 3:
            # phantom-filament stall: only alert after grace so normal
            # filament-change actions don't false-positive
            self.stall_count += 1
            if self.stall_count >= STALL_GRACE_POLLS and not self.notified_failed:
                self.notified_failed = True
                events.append({"event": "print_failed", "printer": self.name,
                               "file": snap["filename"],
                               "detail": (f"stalled at {snap['progress']}% — pause:3 "
                                          "(phantom filament-change signature; "
                                          "likely bad dialect or filament sensor)")})
        elif snap["pause"] == 1:
            if not self.notified_paused:
                self.notified_paused = True
                events.append({"event": "print_paused", "printer": self.name,
                               "file": snap["filename"],
                               "detail": f"paused at {snap['progress']}% by user/MCU"})
        else:
            self.stall_count = 0
            if snap["job_state"] in ("finished", "complete") or snap["progress"] >= 100:
                if not self.notified_done:
                    self.notified_done = True
                    events.append({"event": "print_done", "printer": self.name,
                                   "file": snap["filename"], "progress": 100})
        return events


def post_event(url: str, event: dict, timeout: float = 8.0) -> None:
    """Deliver one event to the Hermes webhook endpoint (fire-and-forget log on error)."""
    data = json.dumps(event).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"webhook sink returned {resp.status}")


def run_loop(config_path: str, webhook_url: str, interval: float = 60.0,
             once: bool = False) -> None:
    """Main loop. once=True = single sweep (for testing)."""
    printers = _load_printers(Path(config_path))
    cfg = json.loads(Path(config_path).read_text())
    alert_offline = bool(cfg.get("alert_offline", False))
    watches = [PrinterWatch(n, c["host"], alert_offline) for n, c in printers.items()]
    while True:
        for w in watches:
            for ev in w.poll():
                try:
                    post_event(webhook_url, ev)
                except Exception as e:
                    # never let a sink failure kill the watcher
                    print(f"sink error for {ev.get('event')}: {e}", flush=True)
        if once:
            return
        time.sleep(interval)
