#!/usr/bin/env python3
"""Entry point for the printwatch-watch systemd service.

Reads config from the plugin data dir (~/.hermes/plugin-data/printwatch/
watcher.json: {"printers": {...}, "webhook_url": "..."}) and runs the
watcher loop. Observe-only: this process NEVER sends printer commands.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(
    os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
    "plugin-data", "printwatch")
CONFIG = os.path.join(DATA_DIR, "watcher.json")

import watcher  # noqa: E402 (sibling module, plugin dir on sys.path)


def main() -> None:
    cfg = json.load(open(CONFIG))
    printers = cfg.get("printers") or {}
    url = cfg.get("webhook_url") or ""
    interval = float(cfg.get("interval_s", 60))
    # HMAC secret lives in Hermes's own subscription store; the watcher reads
    # it via env injection (systemd EnvironmentFile) and NEVER logs it.
    secret = os.environ.get("PRINTWATCH_WEBHOOK_SECRET") or None
    if not printers or not url:
        print("watcher.json needs 'printers' (name->host map) and 'webhook_url'",
              flush=True)
        sys.exit(2)
    tmp = os.path.join(DATA_DIR, "watcher-printers.json")
    with open(tmp, "w") as fh:
        json.dump(printers, fh)
    watcher.run_loop(tmp, url, interval=interval, secret=secret)


if __name__ == "__main__":
    main()
