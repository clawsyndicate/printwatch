"""PrintWatch Anycubic LAN-Mode adapter (clean-room implementation).

Protocol facts were reverse-engineered by the community and re-derived here
from public documentation of the wire format:
  * chrisfore/anycubic_ha_local (AGPL-3.0) — handshake + monitoring reference
  * A-to-PC/Kobra-3-Max-Journey (MIT) — upload pipeline archaeology
No source code was copied; this module is PrintWatch's own implementation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import ssl
import string
import threading
import time
import uuid
from dataclasses import dataclass

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

INFO_PORT = 18910
TOPIC_ROOT = "anycubic/anycubicCloud/v1"

# Wire-spelled poll types ("tempature" is the firmware's own spelling — do not fix)
POLL_TYPES = ("info", "tempature", "fan", "light")
PUSH_ONLY_TYPES = ("print",)

JOB_PHASES = {
    "preheating", "auto_leveling", "vibrating", "flow_calibrating",
    "printing", "pausing", "paused", "resuming", "resumed",
}
PAUSE_STATE = {0: "printing", 1: "paused", 2: "pausing", 3: "resuming", 4: "stopping"}

REASON_TEXT = {
    10401: "Paused by user", 10601: "Stopped by user", 11518: "Filament is stuck",
    10107: "Filament broken", 10113: "Downloaded file failed its MD5 check",
    10115: "The device cannot parse the file", 10133: "The file is missing necessary commands",
    11801: "Spaghetti detected, print paused", 11519: "Filament tangle detected",
}


class AdapterError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Handshake: GET /info -> signed POST /ctrl -> AES-CBC decrypt -> MQTT creds
# --------------------------------------------------------------------------

def _sign(token: str, ts_ms: int, nonce: str) -> str:
    inner = hashlib.md5(token[:16].encode()).hexdigest()
    return hashlib.md5((inner + str(ts_ms) + nonce).encode()).hexdigest()


def _decrypt_ctrl(sealed_b64: str, token: str, session_token: str) -> dict:
    key = token[16:32].encode()
    iv = session_token.encode()[:16].ljust(16, b"\0")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(base64.b64decode(sealed_b64)) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return json.loads((unpadder.update(padded) + unpadder.finalize()).decode())


@dataclass(frozen=True)
class MqttSession:
    host: str            # printer LAN address we talked to
    broker_host: str
    broker_port: int
    username: str        # secret — never log
    password: str        # secret — never log
    device_id: str
    model_id: str
    serial: str
    model_name: str
    mac: str
    file_upload_url: str = ""
    rtsp_url: str = ""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise AdapterError(msg)


def lan_info(host: str, timeout: float = 6.0) -> dict:
    r = requests.get(f"http://{host}:{INFO_PORT}/info", timeout=timeout)
    r.raise_for_status()
    return r.json()


def handshake(host: str, timeout: float = 6.0) -> MqttSession:
    info = lan_info(host, timeout)
    _require(isinstance(info, dict) and info.get("ctrlType") != "cloud",
             f"{host}: not in LAN Mode (ctrlType=cloud) — enable LAN Mode on the printer")
    token = info.get("token")
    _require(isinstance(token, str) and len(token) >= 32 and info.get("ctrlInfoUrl")
             and info.get("modelId") and isinstance(info.get("cn"), str),
             f"{host}: /info lacks the signed-handshake fields (unsupported firmware?)")

    ts = int(time.time() * 1000)
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    did = "".join(random.choices(string.ascii_uppercase + string.digits, k=32))
    qs = f"ts={ts}&nonce={nonce}&sign={_sign(token, ts, nonce)}&did={did}"

    # POST to the printer's own /ctrl path on the SAME host:port — never follow
    # a foreign host from ctrlInfoUrl.
    parts = requests.utils.urlparse(info["ctrlInfoUrl"])
    path = parts.path if parts.path.startswith("/") else f"/{parts.path}"
    r = requests.post(f"http://{host}:{INFO_PORT}{path}?{qs}", timeout=timeout)
    r.raise_for_status()
    ctrl = r.json()
    _require(isinstance(ctrl, dict) and ctrl.get("code") == 200,
             f"{host}: /ctrl rejected the signed request")
    sealed = ctrl.get("data") or {}
    _require(isinstance(sealed.get("info"), str) and isinstance(sealed.get("token"), str),
             f"{host}: /ctrl response malformed")
    creds = _decrypt_ctrl(sealed["info"], token, sealed["token"])

    broker = creds.get("broker", "")
    m = re.match(r"mqtts?://([^:]+):(\d+)", broker if isinstance(broker, str) else "")
    _require(m is not None and all(k in creds for k in ("username", "password", "deviceId")),
             f"{host}: decrypted payload malformed")
    mac_m = re.search(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", str(info.get("usn", "")))
    return MqttSession(
        host=host, broker_host=m.group(1), broker_port=int(m.group(2)),
        username=creds["username"], password=creds["password"],
        device_id=str(creds["deviceId"]), model_id=str(info["modelId"]),
        serial=info["cn"], model_name=info.get("modelName", ""),
        mac=mac_m.group(0) if mac_m else "",
        file_upload_url=info.get("fileUploadurl", ""), rtsp_url=info.get("rtspUrl", ""),
    )


# --------------------------------------------------------------------------
# MQTT transport: subscribe reports, poll types, collect a status snapshot
# --------------------------------------------------------------------------

class PrinterSession:
    """Live MQTT session to one printer. Collects reports until stop()."""

    def __init__(self, session: MqttSession):
        import paho.mqtt.client as mqtt  # local import: only needed for live sessions

        self.s = session
        self.reports: list[dict] = []
        self.connected = threading.Event()
        self._closing = False
        self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"printwatch-{uuid.uuid4().hex[:8]}")
        self._c.username_pw_set(session.username, session.password)
        self._c.tls_set(cert_reqs=ssl.CERT_NONE)  # printer ships a self-signed cert
        self._c.tls_insecure_set(True)
        self._c.on_connect = self._on_connect
        self._c.on_message = self._on_message
        self._c.connect(session.broker_host, session.broker_port, keepalive=60)
        self._c.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if not reason_code.is_failure:
            root = f"{TOPIC_ROOT}/printer/public/{self.s.model_id}/{self.s.device_id}"
            client.subscribe(f"{root}/#")
            self.connected.set()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.topic and msg.payload.decode() or "{}")
        except Exception:
            return
        payload["_topic"] = msg.topic.rsplit("/", 1)[-1]
        self.reports.append(payload)

    def query(self, msg_type: str) -> None:
        action = "getInfo" if msg_type == "multiColorBox" else "query"
        body = json.dumps({"type": msg_type, "action": action,
                           "timestamp": int(time.time() * 1000),
                           "msgid": uuid.uuid4().hex, "data": None})
        self._c.publish(f"{TOPIC_ROOT}/web/printer/{self.s.model_id}/{self.s.device_id}/{msg_type}", body)

    def stop(self) -> None:
        self._closing = True
        try:
            self._c.loop_stop()
            self._c.disconnect()
        except Exception:
            pass

    def collect(self, seconds: float = 5.0) -> list[dict]:
        time.sleep(seconds)
        return self.reports


def snapshot_reports(host: str, wait: float = 5.0) -> tuple[MqttSession, list[dict]]:
    """Handshake + connect + poll all queryable types + collect reports."""
    s = handshake(host)
    ps = PrinterSession(s)
    try:
        if not ps.connected.wait(8):
            raise AdapterError(f"{host}: broker connection not established in 8s")
        for t in POLL_TYPES:
            ps.query(t)
        return s, ps.collect(wait)
    finally:
        ps.stop()
