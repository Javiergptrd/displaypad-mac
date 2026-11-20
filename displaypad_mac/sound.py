"""Sound keys (a soundboard): a key plays an audio file on this Mac when it is pressed.

  {"type": "sound", "file": "sounds/applause-1a2b3c.mp3", "volume": 80, "play": "restart"}
  {"type": "stop_sounds"}

Sounds play through the Mac's sound output (the one chosen in System Settings > Sound) with
afplay, so they open whatever Core Audio opens: MP3, AAC/M4A, WAV, AIFF, CAF and FLAC.
"play" says what pressing the key again does while its sound plays:
  restart (default): plays it again from the start;
  toggle: stops it;
  overlap: plays another copy over the ones playing.
"volume" is 0-100 (default 100) of the Mac's volume.

Files added in the configurator are copied into sounds/ in the configuration folder, so a
key keeps working when the original is moved or deleted. "file" can also be any file on
the Mac ("~/Music/intro.mp3"), and the Mac's own alert sounds are in /System/Library/Sounds.

While a key's sound plays the key shows how far it has got (render.py draws a ring).
"""

import logging
import os
import re
import secrets
import subprocess
import threading
import time
from typing import Iterable, Optional

from .i18n import t

log = logging.getLogger(__name__)

AFPLAY = "/usr/bin/afplay"
AFINFO = "/usr/bin/afinfo"
MAC_SOUNDS = "/System/Library/Sounds"
FOLDER = "sounds"
TYPES = (".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff", ".aifc", ".caf", ".flac", ".m4r")
MODES = ("restart", "toggle", "overlap")
MAX_PLAYING = 16          # the oldest one stops when one more starts
MAX_UPLOAD = 50 * 1024 * 1024
TAIL = 0.5                # afplay stays open up to ~1 s after the end: past its length + this, a sound counts as over

_lock = threading.Lock()
_playing: list = []       # {"file": real path, "proc": Popen, "t0": monotonic, "seconds": float or None}
_lengths: dict = {}       # (real path, mtime, size) -> seconds, or None if it is not a sound


def resolve(file: str, base_dir: str) -> str:
    """The real path of a sound: relative ones are in the configuration folder."""
    return os.path.realpath(os.path.join(base_dir, os.path.expanduser(str(file or "").strip())))


def name_of(file: str) -> str:
    """A sound's name for people: its file name without the type, or the code added on upload."""
    stem = os.path.splitext(os.path.basename(str(file or "")))[0]
    return re.sub(r"-[0-9a-f]{6}$", "", stem).replace("_", " ").replace("-", " ").strip() or stem


def validate(action: dict) -> str:
    file = str(action.get("file") or "").strip()
    if os.path.splitext(file)[1].lower() not in TYPES:
        return t("the sound must be an MP3, M4A, WAV, AIFF, CAF or FLAC file")
    vol = action.get("volume", 100)
    if isinstance(vol, bool) or not isinstance(vol, (int, float)) or not 0 <= vol <= 100:
        return t("the sound volume must be a number from 0 to 100")
    if action.get("play", "restart") not in MODES:
        return t("the sound's play must be one of: {values}", values=", ".join(MODES))
    return ""


def seconds(path: str) -> Optional[float]:
    """How long a sound lasts (from afinfo), or None if macOS cannot read it as a sound."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime, st.st_size)
    if key not in _lengths:
        try:
            r = subprocess.run([AFINFO, path], capture_output=True, text=True, timeout=10)
            m = re.search(r"estimated duration:\s*([\d.]+)", r.stdout)
            _lengths[key] = float(m.group(1)) if r.returncode == 0 and m else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            _lengths[key] = None
        if len(_lengths) > 500:
            _lengths.pop(next(iter(_lengths)))
    return _lengths[key]


def _reap():
    _playing[:] = [p for p in _playing if p["proc"].poll() is None]


def _over(p: dict) -> bool:
    """Heard to the end, though afplay may still be closing."""
    return bool(p["seconds"]) and time.monotonic() - p["t0"] > p["seconds"] + TAIL


def _kill(p: dict):
    if p in _playing:
        _playing.remove(p)
    if p["proc"].poll() is None:
        p["proc"].terminate()
        threading.Thread(target=p["proc"].wait, daemon=True).start()  # no zombie left behind


def play(path: str, volume=100, mode: str = "restart") -> bool:
    """Play a sound file. Returns False when it stopped one instead (play "toggle")."""
    if not os.path.isfile(path):
        raise FileNotFoundError(t("sound file not found: {path}", path=path))
    with _lock:
        _reap()
        mine = [p for p in _playing if p["file"] == path]
        if mine and mode in ("restart", "toggle"):
            heard = any(not _over(p) for p in mine)
            for p in mine:
                _kill(p)
            if mode == "toggle" and heard:
                log.info("sound stopped: %s", os.path.basename(path))
                return False
        while len(_playing) >= MAX_PLAYING:
            _kill(_playing[0])
        vol = max(0.0, min(100.0, float(volume))) / 100
        proc = subprocess.Popen([AFPLAY, "-v", f"{vol:.2f}", path], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        entry = {"file": path, "proc": proc, "t0": time.monotonic(), "seconds": None}
        _playing.append(entry)
    entry["seconds"] = seconds(path)  # after it starts, so the sound is not held up
    log.info("sound playing: %s", os.path.basename(path))
    return True


def stop_all() -> int:
    """Stop every sound playing; returns how many there were."""
    with _lock:
        _reap()
        n = len(_playing)
        for p in list(_playing):
            _kill(p)
    if n:
        log.info("stopped %d sound(s)", n)
    return n


def progress(paths: Iterable[str]) -> Optional[float]:
    """How far the newest sound playing from these files has got (0-1), or None if none plays."""
    paths = set(paths)
    with _lock:
        _reap()
        mine = [p for p in _playing if p["file"] in paths and not _over(p)]
        if not mine:
            return None
        p = max(mine, key=lambda p: p["t0"])
        return min(1.0, (time.monotonic() - p["t0"]) / p["seconds"]) if p["seconds"] else 0.0


def playing_count() -> int:
    with _lock:
        _reap()
        return sum(1 for p in _playing if not _over(p))


def save_upload(data: bytes, name: str, base_dir: str) -> dict:
    """Store a sound added in the configurator under sounds/, keeping its name."""
    ext = os.path.splitext(name or "")[1].lower()
    if ext not in TYPES:
        raise ValueError(t("the sound must be an MP3, M4A, WAV, AIFF, CAF or FLAC file"))
    if len(data) > MAX_UPLOAD:
        raise ValueError(t("the sound is too large (50 MB max)"))
    stem = re.sub(r"[^\w\- ]+", "", os.path.splitext(os.path.basename(name))[0]).strip()
    stem = re.sub(r"[\s-]+", "-", stem)[:40].strip("-") or "sound"
    folder = os.path.join(base_dir, FOLDER)
    os.makedirs(folder, exist_ok=True)
    fname = f"{stem}-{secrets.token_hex(3)}{ext}"
    path = os.path.join(folder, fname)
    with open(path, "wb") as f:
        f.write(data)
    secs = seconds(os.path.realpath(path))
    if secs is None:
        os.remove(path)
        raise ValueError(t("the file is not a sound macOS can play (use MP3, M4A, WAV, AIFF, CAF or FLAC)"))
    log.info("sound saved as %s/%s (%.1f s)", FOLDER, fname, secs)
    return {"file": f"{FOLDER}/{fname}", "name": name_of(fname), "seconds": round(secs, 2)}


def list_sounds(base_dir: str) -> dict:
    """The sounds added in the configurator (newest first) and the Mac's own."""
    mine = []
    folder = os.path.join(base_dir, FOLDER)
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.stat().st_mtime, reverse=True)
    except OSError:
        entries = []
    for e in entries:
        if e.is_file() and os.path.splitext(e.name)[1].lower() in TYPES:
            secs = seconds(os.path.realpath(e.path))
            mine.append({"file": f"{FOLDER}/{e.name}", "name": name_of(e.name),
                         "seconds": round(secs, 2) if secs else None})
    try:
        mac = sorted(n for n in os.listdir(MAC_SOUNDS) if os.path.splitext(n)[1].lower() in TYPES)
    except OSError:
        mac = []
    return {"mine": mine, "mac": [{"file": f"{MAC_SOUNDS}/{n}", "name": name_of(n),
                                   "seconds": round(seconds(f"{MAC_SOUNDS}/{n}") or 0, 2) or None} for n in mac]}
