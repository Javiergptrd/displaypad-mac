"""OBS Studio, controlled through the WebSocket server built into OBS 28 and later (in OBS:
Tools › WebSocket Server Settings). Keys can start and stop streaming and recording, switch
scenes, mute microphones and more, whichever app is in front, with no hotkeys to set up in OBS.

  {"type": "obs", "do": "stream"}                    start or stop streaming
  {"type": "obs", "do": "record"}                    start or stop recording
  {"type": "obs", "do": "pause"}                     pause or resume the recording
  {"type": "obs", "do": "scene", "scene": "Game"}    switch to a scene; "#2" is the second one in
                                                     OBS's Scenes list, counting from the top
  {"type": "obs", "do": "next_scene"}                the scene under the current one (wraps around)
  {"type": "obs", "do": "previous_scene"}            the scene above it
  {"type": "obs", "do": "mute", "source": ""}        mute or unmute an audio source by name;
                                                     empty: every microphone
  {"type": "obs", "do": "virtualcam"}                start or stop the virtual camera
  {"type": "obs", "do": "studio"}                    studio mode on or off
  {"type": "obs", "do": "transition"}                studio mode: send the preview to the program
  {"type": "obs", "do": "replay"}                    save the replay buffer

In studio mode, switching scenes changes the preview, as in OBS itself.

The connection (address, port, password) is kept in obs.json next to config.json, readable
only by this user: the password is not part of the configuration that the configurator page
or an AI assistant reads. The WebSocket client is a small one of our own (RFC 6455), so
nothing needs installing; OBS speaks obs-websocket protocol 5 over it.
"""

import base64
import hashlib
import itertools
import json
import logging
import os
import socket
import struct
import threading
from typing import Optional

from .i18n import t

log = logging.getLogger(__name__)

DO = ("stream", "record", "pause", "scene", "next_scene", "previous_scene", "mute",
      "virtualcam", "studio", "transition", "replay")
DEFAULTS = {"host": "127.0.0.1", "port": 4455, "password": ""}
FILE = "obs.json"
TIMEOUT = 3.0
MIC_KINDS = {"coreaudio_input_capture"}  # microphones and other inputs on a Mac
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ObsError(Exception):
    """A message for the user (in their language)."""


class _Closed(ConnectionError):
    def __init__(self, code: int, reason: str):
        super().__init__(f"closed {code} {reason}")
        self.code, self.reason = code, reason


# ------------------------------------------------------------------ settings
def load_settings(base_dir: str) -> dict:
    try:
        with open(os.path.join(base_dir, FILE), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: raw[k] for k in DEFAULTS if k in raw})
    return out


def save_settings(base_dir: str, patch: dict) -> dict:
    """host, port and password; a password of None keeps the one saved."""
    cur = load_settings(base_dir)
    host = str(patch.get("host", cur["host"])).strip() or DEFAULTS["host"]
    port = patch.get("port", cur["port"])
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise ObsError(t("the OBS port must be a number from 1 to 65535"))
    pw = cur["password"] if patch.get("password") is None else str(patch["password"])
    new = {"host": host, "port": port, "password": pw}
    path = os.path.join(base_dir, FILE)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(new, f, indent=2)
    os.replace(tmp, path)
    with _lock:  # the next call connects with the new settings
        _drop()
    return public(new)


def public(s: dict) -> dict:
    """What the configurator may see: no password, only whether there is one."""
    return {"host": s["host"], "port": s["port"], "has_password": bool(s["password"])}


# ------------------------------------------------------------------ WebSocket
class _Socket:
    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=TIMEOUT)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           f"Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk or len(head) > 65536:
                raise ConnectionError("no WebSocket answer")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
            raise ConnectionError(lines[0])
        headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
        want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != want:
            raise ConnectionError("bad WebSocket answer")

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, op: int, data: bytes):
        n = len(data)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                     bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                     bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(data)))

    def send(self, msg: dict):
        self._frame(0x1, json.dumps(msg).encode())

    def recv(self) -> dict:
        parts = []
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if op == 0x8:
                raise _Closed(struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1005,
                              data[2:].decode("utf-8", "replace"))
            if op == 0x9:
                self._frame(0xA, data)
            elif op in (0x0, 0x1, 0x2):
                parts.append(data)
                if b0 & 0x80:
                    return json.loads(b"".join(parts).decode("utf-8"))

    def close(self):
        try:
            self._frame(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        self.sock.close()


def _b64sha(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


class Client:
    """One identified connection to OBS."""

    def __init__(self, host: str, port: int, password: str):
        self.ws = _Socket(host, port)
        self._ids = itertools.count(1)
        hello = self.ws.recv()
        if hello.get("op") != 0:
            raise ConnectionError("not obs-websocket")
        d = hello.get("d") or {}
        ident = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = d.get("authentication")
        if auth:
            if not password:
                self.ws.close()
                raise ObsError(t("OBS asks for a password: copy it from OBS (Tools › WebSocket Server "
                                 "Settings › Show Connect Info) into DisplayPad's Settings › OBS"))
            ident["authentication"] = _b64sha(_b64sha(password + auth["salt"]) + auth["challenge"])
        self.ws.send({"op": 1, "d": ident})
        try:
            msg = self.ws.recv()
        except _Closed as e:
            if e.code == 4009:
                raise ObsError(t("OBS did not accept the password: copy it again from OBS (Tools › "
                                 "WebSocket Server Settings › Show Connect Info) into DisplayPad's Settings › OBS"))
            raise
        if msg.get("op") != 2:
            raise ConnectionError("OBS did not identify the connection")
        self.version = d.get("obsWebSocketVersion", "")

    def call(self, request: str, data: Optional[dict] = None) -> dict:
        rid = str(next(self._ids))
        self.ws.send({"op": 6, "d": {"requestType": request, "requestId": rid, "requestData": data or {}}})
        while True:
            msg = self.ws.recv()
            d = msg.get("d") or {}
            if msg.get("op") == 7 and d.get("requestId") == rid:
                break
        status = d.get("requestStatus") or {}
        if not status.get("result"):
            raise ObsError(t("OBS: {error}", error=status.get("comment") or t("the request failed (code {code})",
                                                                            code=status.get("code"))))
        return d.get("responseData") or {}

    def close(self):
        self.ws.close()


_lock = threading.Lock()
_client: Optional[Client] = None
_client_for: Optional[tuple] = None


def _drop():
    global _client, _client_for
    if _client:
        try:
            _client.close()
        except OSError:
            pass
    _client, _client_for = None, None


def _connect(s: dict) -> Client:
    global _client, _client_for
    key = (s["host"], s["port"], s["password"])
    if _client is None or _client_for != key:
        _drop()
        try:
            _client, _client_for = Client(*key), key
        except (OSError, ValueError) as e:
            log.info("OBS at %s:%s: %s", s["host"], s["port"], e)
            raise ObsError(t("OBS is not reachable at {host}:{port}. Is OBS open, with its WebSocket server on "
                             "(Tools › WebSocket Server Settings › Enable WebSocket server)?",
                             host=s["host"], port=s["port"]))
    return _client


def session(base_dir: str, work):
    """Run work(client) on the shared connection; a connection that went stale (OBS was
    closed and opened again) is made again once."""
    s = load_settings(base_dir)
    with _lock:
        for attempt in (1, 2):
            client = _connect(s)
            try:
                return work(client)
            except ObsError:
                raise
            except (OSError, ValueError) as e:
                _drop()
                if attempt == 2:
                    raise ObsError(t("lost the connection to OBS: {error}", error=e))


# ------------------------------------------------------------------ actions
def validate(action: dict) -> str:
    do = action.get("do")
    if do not in DO:
        return t("unknown OBS command '{do}'; use one of: {values}", do=do, values=", ".join(DO))
    if do == "scene" and not str(action.get("scene", "")).strip():
        return t("the OBS scene is missing: its name, or #1 for the first one in the list")
    for f in ("scene", "source"):
        if not isinstance(action.get(f, ""), str):
            return t("the OBS {field} must be text", field=f)
    return ""


def _scenes(c: Client) -> tuple:
    """(scene names from the top of OBS's list, the current one, studio mode)."""
    r = c.call("GetSceneList")
    names = [s["sceneName"] for s in reversed(r.get("scenes") or [])]  # OBS lists them bottom first
    studio = c.call("GetStudioModeEnabled").get("studioModeEnabled", False)
    now = (r.get("currentPreviewSceneName") if studio else None) or r.get("currentProgramSceneName")
    return names, now, studio


def _show(c: Client, name: str, studio: bool):
    c.call("SetCurrentPreviewScene" if studio else "SetCurrentProgramScene", {"sceneName": name})


def _do(c: Client, action: dict) -> str:
    do = action["do"]
    simple = {"stream": "ToggleStream", "record": "ToggleRecord", "pause": "ToggleRecordPause",
              "virtualcam": "ToggleVirtualCam", "transition": "TriggerStudioModeTransition",
              "replay": "SaveReplayBuffer"}
    if do in simple:
        r = c.call(simple[do])
        return {True: t("on"), False: t("off")}.get(r.get("outputActive", r.get("outputPaused")), t("done"))
    if do == "studio":
        on = not c.call("GetStudioModeEnabled").get("studioModeEnabled", False)
        c.call("SetStudioModeEnabled", {"studioModeEnabled": on})
        return t("on") if on else t("off")
    if do == "scene":
        want = str(action.get("scene", "")).strip()
        names, _, studio = _scenes(c)
        if want.startswith("#") and want[1:].isdigit():
            i = int(want[1:])
            if not 1 <= i <= len(names):
                raise ObsError(t("OBS has {n} scenes, not {i}", n=len(names), i=i))
            want = names[i - 1]
        elif want not in names:
            raise ObsError(t("OBS has no scene called “{name}”", name=want))
        _show(c, want, studio)
        return want
    if do in ("next_scene", "previous_scene"):
        names, now, studio = _scenes(c)
        if not names:
            raise ObsError(t("OBS has no scenes"))
        i = names.index(now) if now in names else -1
        name = names[(i + (1 if do == "next_scene" else -1)) % len(names)]
        _show(c, name, studio)
        return name
    if do == "mute":
        source = str(action.get("source", "")).strip()
        if source:
            muted = c.call("ToggleInputMute", {"inputName": source}).get("inputMuted")
            return t("muted") if muted else t("unmuted")
        mics = [i["inputName"] for i in c.call("GetInputList").get("inputs") or []
                if (i.get("unversionedInputKind") or i.get("inputKind")) in MIC_KINDS]
        if not mics:
            raise ObsError(t("OBS has no microphone source"))
        muted = not c.call("GetInputMute", {"inputName": mics[0]}).get("inputMuted", False)
        for name in mics:
            c.call("SetInputMute", {"inputName": name, "inputMuted": muted})
        return (t("muted") if muted else t("unmuted")) + ": " + ", ".join(mics)
    raise ObsError(validate(action))


def run(action: dict, base_dir: str) -> str:
    """Do it; returns what happened, for the configurator's Test button."""
    err = validate(action)
    if err:
        raise ObsError(err)
    return session(base_dir, lambda c: _do(c, action))


def info(base_dir: str) -> dict:
    """For the configurator: OBS's version, scenes (from the top) and sources."""
    def work(c: Client):
        version = c.call("GetVersion")
        names, now, studio = _scenes(c)
        inputs = c.call("GetInputList").get("inputs") or []
        return {"ok": True, "obs_version": version.get("obsVersion", ""), "websocket_version": c.version,
                "scenes": names, "current_scene": now, "studio_mode": studio,
                "sources": [i["inputName"] for i in inputs],
                "microphones": [i["inputName"] for i in inputs
                                if (i.get("unversionedInputKind") or i.get("inputKind")) in MIC_KINDS]}
    try:
        return session(base_dir, work)
    except ObsError as e:
        return {"ok": False, "error": str(e)}

