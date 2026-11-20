"""Live keys: keys that show something about the Mac that keeps changing (the time, CPU,
GPU and memory use, the battery, the volume, what is playing, OBS, a command's output) and
redraw themselves when it changes.

    {"label": "CPU {value}%", "live": {"source": "cpu"}}
    {"label": "CPU", "live": {"source": "cpu", "style": "meter"}}
    {"label": "{time}", "live": {"source": "clock"}}
    {"label": "{output}", "live": {"source": "command", "command": "curl -s 'wttr.in?format=%t'", "every": 600}}

The label is the text to show, with the reading's values in braces (FIELDS lists them). With
"style": "meter" (numbers only) the key is a small gauge instead: the label as its title, the
number, a line of the last minute and a bar that turns amber and red. A text key turns amber
or red when the reading needs attention (OBS live, the mic at 0, CPU high...), unless
"alert" is false.

Readings are taken only for the keys someone looks at: the page on the pad, the pages open in
the configurator, a page an assistant previews. Each source keeps its own pace (EVERY) and is
read in a small pool of threads, so a slow one (a command, AppleScript) never holds up the
others or the pad. The main loop of the service asks face() what a key should show, and sends
the key to the pad when that changes.
"""

import collections
import concurrent.futures
import ctypes
import hashlib
import json
import logging
import re
import subprocess
import threading
import time
from typing import Callable, Dict, Iterable, Optional

from . import appwatch, i18n, obs
from .i18n import t

log = logging.getLogger(__name__)

SOURCES = ("clock", "cpu", "gpu", "ram", "battery", "volume", "mic", "music", "obs", "command")
NUMERIC = ("cpu", "gpu", "ram", "battery", "volume", "mic", "command")  # can be a meter
STYLES = ("text", "meter")
# What a label can show, per source: "{time}", "{value}"... (also in the configurator and get_reference)
FIELDS = {
    "clock": ("time", "time12", "ampm", "seconds", "weekday", "day", "month", "year", "date"),
    "cpu": ("value",), "gpu": ("value",), "ram": ("value", "used", "total"),
    "battery": ("value", "state"), "volume": ("value", "state"), "mic": ("value", "state"),
    "music": ("title", "artist", "app", "state"),
    "obs": ("status", "scene", "stream_time", "record_time"),
    "command": ("output", "value"),
}
# seconds between two readings; the clock is read when a key is drawn
EVERY = {"cpu": 2, "gpu": 2, "ram": 2, "battery": 30, "audio": 2, "music": 3, "obs": 2}
COMMAND_EVERY, COMMAND_MIN, COMMAND_MAX = 10, 2, 3600
COMMAND_TIMEOUT = 10
COMMAND_MAX_LEN = 2000
HISTORY = 30          # readings in a meter's line: one minute at 2 s
LOOKER_TTL = 15       # the configurator asks every second; it is forgotten 15 s after it stops
WORKERS = 4
_FIELD = re.compile(r"\{(\w+)\}")


def default_text(source: str) -> str:
    """The label a live key shows when it has none."""
    return {"clock": "{time}", "cpu": "CPU {value}%", "gpu": "GPU {value}%", "ram": "RAM {value}%",
            "battery": "{value}%", "volume": "{value}%", "mic": "{value}%", "music": "{title}",
            "obs": "{status}", "command": "{output}"}.get(source, "")


def meter_title(source: str) -> str:
    return {"cpu": "CPU", "gpu": "GPU", "ram": "RAM", "battery": t("Battery"), "volume": t("Volume"),
            "mic": t("Mic")}.get(source, "")


def validate(lv) -> str:
    """An error message for a key's "live", or "" when it is fine."""
    if not isinstance(lv, dict):
        return t('live must be an object like {"source": "cpu"}')
    source = lv.get("source")
    if source not in SOURCES:
        return t("unknown live source '{source}'; use one of: {values}", source=source, values=", ".join(SOURCES))
    style = lv.get("style", "text")
    if style not in STYLES:
        return t("the live style must be \"text\" or \"meter\"")
    if style == "meter" and source not in NUMERIC:
        return t("a meter needs a number: use it with {values}", values=", ".join(NUMERIC))
    if not isinstance(lv.get("alert", True), bool):
        return t("alert must be true or false")
    if source == "command":
        cmd = lv.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return t("a live command key needs the command to run")
        if len(cmd) > COMMAND_MAX_LEN:
            return t("the command is too long (at most {n} characters)", n=COMMAND_MAX_LEN)
        every = lv.get("every", COMMAND_EVERY)
        if isinstance(every, bool) or not isinstance(every, (int, float)) or not COMMAND_MIN <= every <= COMMAND_MAX:
            return t("every must be a number of seconds from {min} to {max}", min=COMMAND_MIN, max=COMMAND_MAX)
    return ""


def is_meter(spec: Optional[dict]) -> bool:
    lv = (spec or {}).get("live")
    return isinstance(lv, dict) and lv.get("style") == "meter" and not validate(lv)


def commands_in(pages: dict) -> set:
    """The commands of the live keys in a parsed configuration (the only ones that may run)."""
    return {spec["live"]["command"].strip() for keys in pages.values() for spec in keys
            if spec and isinstance(spec.get("live"), dict) and spec["live"].get("source") == "command"
            and isinstance(spec["live"].get("command"), str)}


def _reader_key(lv: dict) -> tuple:
    """(reader, source key): what to read, and under which name its reading is kept.
    Volume and mic come from one reading of the sound settings."""
    source = lv["source"]
    if source == "command":
        every = float(lv.get("every", COMMAND_EVERY))
        key = f"command|{every:g}|{lv['command'].strip()}"
        return key, key
    if source in ("volume", "mic"):
        return "audio", source
    return source, source


def _every(reader: str) -> float:
    if reader.startswith("command|"):
        return float(reader.split("|", 2)[1])
    return EVERY.get(reader, 2)


# ------------------------------------------------------------------ readings
_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_libc.mach_host_self.restype = ctypes.c_uint32
_libc.host_statistics.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                               ctypes.c_void_p, ctypes.c_size_t]
_HOST_CPU_LOAD_INFO = 3


def _sysctl(name: str, ctype):
    value, size = ctype(0), ctypes.c_size_t(ctypes.sizeof(ctype))
    if _libc.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
        raise OSError(f"sysctl {name} failed")
    return value.value


def cpu_ticks() -> tuple:
    """(busy, total) ticks of all cores since the Mac started."""
    info = (ctypes.c_uint32 * 4)()  # user, system, idle, nice
    count = ctypes.c_uint32(4)
    if _libc.host_statistics(_libc.mach_host_self(), _HOST_CPU_LOAD_INFO, info, ctypes.byref(count)) != 0:
        raise OSError("host_statistics failed")
    user, system, idle, nice = info
    return user + system + nice, user + system + idle + nice


def _run(cmd: list, timeout: float = 5) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def _level(pct: Optional[float]) -> int:
    return 0 if pct is None else 2 if pct >= 90 else 1 if pct >= 70 else 0


def read_gpu() -> dict:
    """The "Device Utilization %" the graphics driver reports (Apple silicon and most Intel Macs)."""
    found = [int(n) for n in re.findall(r'"Device Utilization %"=(\d+)', _run(["/usr/sbin/ioreg", "-r", "-d", "1",
                                                                               "-c", "IOAccelerator"]))]
    if not found:
        return {"error": t("this Mac does not report GPU use")}
    pct = float(max(found))
    return {"value": pct, "level": _level(pct), "fields": {"value": round(pct)}}


def read_ram() -> dict:
    """Memory in use as Activity Monitor counts it (app memory + wired + compressed); the
    colour follows macOS's memory pressure, since macOS keeps memory fairly full on purpose."""
    out = _run(["/usr/bin/vm_stat"])
    page = int(re.search(r"page size of (\d+) bytes", out).group(1))
    v = {k.strip().strip('"'): int(n) for k, n in re.findall(r"^([^:\n]+):\s+(\d+)\.", out, re.M)}
    if "Anonymous pages" in v:
        pages = v["Anonymous pages"] - v.get("Pages purgeable", 0) + v["Pages wired down"] + v["Pages occupied by compressor"]
    else:
        pages = (v["Pages active"] + v["Pages inactive"] + v.get("Pages speculative", 0) + v["Pages wired down"]
                 + v["Pages occupied by compressor"] - v.get("Pages purgeable", 0) - v.get("File-backed pages", 0))
    total = _sysctl("hw.memsize", ctypes.c_uint64)
    used = pages * page
    pct = 100.0 * used / total
    try:
        pressure = _sysctl("kern.memorystatus_vm_pressure_level", ctypes.c_int)  # 1 normal, 2 warning, 4 critical
    except OSError:
        pressure = 1
    gb = 1024 ** 3
    return {"value": pct, "level": {2: 1, 4: 2}.get(pressure, 0), "note": f"{used / gb:.1f} GB",
            "fields": {"value": round(pct), "used": f"{used / gb:.1f}", "total": f"{total / gb:.0f}"}}


def read_battery() -> dict:
    out = _run(["/usr/bin/pmset", "-g", "batt"])
    m = re.search(r"(\d+)%;\s*([^;]+);", out)
    if not m:
        return {"error": t("this Mac has no battery")}
    pct, raw = float(m.group(1)), m.group(2).strip().lower()
    charging = raw in ("charging", "finishing charge") or raw.startswith("ac")
    state = t("charged") if raw == "charged" else t("charging") if charging else t("on battery")
    level = 0 if charging or raw == "charged" else 2 if pct <= 10 else 1 if pct <= 20 else 0
    return {"value": pct, "level": level, "note": "⚡" if charging else "",
            "fields": {"value": round(pct), "state": state}}


def read_audio() -> Dict[str, dict]:
    """Output volume and input (microphone) level, from one look at the sound settings."""
    out = _run(["/usr/bin/osascript", "-e", "get volume settings"])
    vals = dict(re.findall(r"(output volume|input volume|output muted):\s*([\w.]+)", out))
    res = {}
    vol = vals.get("output volume", "")
    muted = vals.get("output muted") == "true"
    if vol.isdigit():
        res["volume"] = {"value": float(vol), "level": 1 if muted else 0, "dim": muted, "note": t("muted") if muted else "",
                         "fields": {"value": int(vol), "state": t("muted") if muted else ""}}
    else:
        res["volume"] = {"error": t("this sound output has no volume control")}
    mic = vals.get("input volume", "")
    if mic.isdigit():
        off = int(mic) == 0
        res["mic"] = {"value": float(mic), "level": 2 if off else 0, "dim": off, "note": t("muted") if off else "",
                      "fields": {"value": int(mic), "state": t("muted") if off else ""}}
    else:
        res["mic"] = {"error": t("no microphone")}
    return res


_MUSIC_SCRIPT = """\
tell application "{app}"
  try
    set s to player state as text
    if s is "stopped" then return ""
    return s & linefeed & (name of current track) & linefeed & (artist of current track)
  on error
    return ""
  end try
end tell"""


def read_music(running: set) -> dict:
    """What Music or Spotify is playing. Only an app that is already open is asked (macOS asks
    once whether DisplayPad may control it), so nothing is ever opened by this."""
    best = None
    for app in ("Spotify", "Music"):
        if appwatch.norm(app) not in running:
            continue
        r = subprocess.run(["/usr/bin/osascript", "-e", _MUSIC_SCRIPT.format(app=app)],
                           capture_output=True, text=True, timeout=5)
        if r.returncode and "-1743" in r.stderr:
            log.info("live music: allow DisplayPad to control %s in System Settings > Privacy & Security > Automation", app)
            return {"error": t("allow it in Settings › Privacy › Automation")}
        parts = r.stdout.rstrip("\n").split("\n")
        if len(parts) >= 3 and parts[1]:
            playing = parts[0] == "playing"
            now = {"fields": {"title": parts[1], "artist": parts[2], "app": app,
                              "state": t("playing") if playing else t("paused")}}
            if playing:
                return now
            best = best or now
    return best or {"fields": {"title": "", "artist": "", "app": "", "state": ""}}


def _hms(code: str) -> str:
    """OBS's "01:02:03.456" as "1:02:03", or "02:03" under an hour."""
    h, m, s = (code or "00:00:00").split(".")[0].split(":")
    return f"{int(h)}:{m}:{s}" if int(h) else f"{m}:{s}"


def read_obs(base_dir: str, running: set) -> dict:
    """Streaming and recording state and the scene on air. OBS is only asked while it is open."""
    if "obs" not in running:
        return {"level": 0, "fields": {"status": t("OBS closed"), "scene": "", "stream_time": "", "record_time": ""}}

    def work(c):
        return c.call("GetStreamStatus"), c.call("GetRecordStatus"), c.call("GetCurrentProgramScene")
    try:
        stream, record, scene = obs.session(base_dir, work)
    except obs.ObsError:
        return {"level": 0, "fields": {"status": t("No connection"), "scene": "", "stream_time": "", "record_time": ""}}
    live, rec, paused = stream.get("outputActive"), record.get("outputActive"), record.get("outputPaused")
    words = ([t("LIVE")] if live else []) + ([t("PAUSED") if paused else t("REC")] if rec else [])
    return {"level": 2 if live or (rec and not paused) else 1 if rec else 0,
            "fields": {"status": " · ".join(words) or t("Off air"),
                       "scene": scene.get("currentProgramSceneName") or scene.get("sceneName") or "",
                       "stream_time": _hms(stream.get("outputTimecode")) if live else "",
                       "record_time": _hms(record.get("outputTimecode")) if rec else ""}}


def read_command(command: str, timeout: float) -> dict:
    """The first line the command prints (and the first number in it, for a meter)."""
    try:
        r = subprocess.run(["/bin/zsh", "-lc", command], capture_output=True, text=True,
                           timeout=timeout, start_new_session=True)
    except subprocess.TimeoutExpired:
        return {"error": t("took more than {seconds} s", seconds=round(timeout)), "level": 1}
    line = next((ln.strip() for ln in r.stdout.splitlines() if ln.strip()), "")[:80]
    m = re.search(r"-?\d+(?:[.,]\d+)?", line)
    value = float(m.group(0).replace(",", ".")) if m else None
    level = 1 if r.returncode else _level(value) if value is not None else 0
    fields = {"output": line or (r.stderr.strip().splitlines() or [""])[0][:80]}
    if value is not None:
        fields["value"] = f"{value:g}"
    return {"value": value, "level": level, "fields": fields}


_DAYS = {"en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
         "es": ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")}
_MONTHS = {"en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
           "es": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic")}


def read_clock(now: Optional[float] = None) -> dict:
    lt = time.localtime(now)
    lang = i18n.language() if i18n.language() in _DAYS else "en"
    day, month = _DAYS[lang][lt.tm_wday], _MONTHS[lang][lt.tm_mon - 1]
    h12 = lt.tm_hour % 12 or 12
    return {"fields": {"time": f"{lt.tm_hour:02d}:{lt.tm_min:02d}", "time12": f"{h12}:{lt.tm_min:02d}",
                       "ampm": "AM" if lt.tm_hour < 12 else "PM", "seconds": f"{lt.tm_sec:02d}",
                       "weekday": day, "day": str(lt.tm_mday), "month": month, "year": str(lt.tm_year),
                       "date": f"{day} {lt.tm_mday} {month}"}}


def fill(template: str, fields: dict) -> str:
    """The label with the reading's values in place of {name}; unknown names stay as written."""
    return _FIELD.sub(lambda m: str(fields[m.group(1)]) if m.group(1) in fields else m.group(0), template)


# ------------------------------------------------------------------ engine
class Engine:
    """Takes the readings the live keys on view need, at each source's pace."""

    def __init__(self, base_dir: Callable[[], str]):
        self._base_dir = base_dir
        self._lock = threading.Lock()
        self._readings: Dict[str, dict] = {}                 # source key -> latest reading
        self._history: Dict[str, collections.deque] = {}    # source key -> recent values (meters)
        self._lookers: Dict[str, tuple] = {}                 # who -> (readers, expires or None)
        self._due: Dict[str, float] = {}
        self._last: Dict[str, float] = {}                    # reader -> when it was last read
        self._busy: set = set()
        self._errors: Dict[str, str] = {}
        self._commands: set = set()   # commands of saved live keys: the only ones run
        self._cpu: Optional[tuple] = None
        self._apps = appwatch.Apps()
        self._apps_lock = threading.Lock()
        self._pool = concurrent.futures.ThreadPoolExecutor(WORKERS, thread_name_prefix="live")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.version = 0  # goes up with every new reading

    # -------------------------------------------------- who looks at what
    def allow_commands(self, commands: Iterable[str]):
        with self._lock:
            self._commands = {c.strip() for c in commands}
            for name, reading in self._readings.items():  # just saved: run it now, not at its next turn
                if reading.get("unsaved") and name.split("|", 2)[2] in self._commands:
                    self._due[name] = 0
        self._wake.set()

    def watch(self, who: str, lives: Iterable, ttl: Optional[float] = None):
        """The live keys `who` shows now ("pad", "portal", "preview"); with ttl they are
        dropped that many seconds after the last call."""
        readers = {_reader_key(lv)[0] for lv in lives if not validate(lv)}
        with self._lock:
            if readers:
                self._lookers[who] = (readers, time.monotonic() + ttl if ttl else None)
            else:
                self._lookers.pop(who, None)
        self._wake.set()

    def _wanted(self, now: float) -> set:
        for who in [w for w, (_, exp) in self._lookers.items() if exp is not None and exp < now]:
            del self._lookers[who]
        return set().union(*(r for r, _ in self._lookers.values())) if self._lookers else set()

    def ready(self, lives: Iterable) -> bool:
        """True when every one of these has a reading (the clock always has)."""
        with self._lock:
            return all(lv["source"] == "clock" or _reader_key(lv)[1] in self._readings
                       for lv in lives if not validate(lv))

    # -------------------------------------------------- the reading thread
    def start(self):
        threading.Thread(target=self._loop, name="live", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self):
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                due = [r for r in self._wanted(now) if r != "clock" and r not in self._busy and self._due.get(r, 0) <= now]
                self._busy.update(due)
            for r in due:
                self._pool.submit(self._read, r)
            self._wake.wait(0.2)
            self._wake.clear()

    def _running(self) -> set:
        with self._apps_lock:
            return self._apps.running()

    def _read(self, reader: str):
        started = time.monotonic()
        stale = started - self._last.get(reader, 0) > 3 * _every(reader)  # not looked at for a while
        try:
            if reader == "cpu":
                if stale or self._cpu is None:
                    self._cpu = cpu_ticks()
                    time.sleep(0.5)
                busy, total = cpu_ticks()
                db, dt = busy - self._cpu[0], total - self._cpu[1]
                self._cpu = (busy, total)
                pct = 100.0 * db / dt if dt > 0 else 0.0
                out = {"cpu": {"value": pct, "level": _level(pct), "fields": {"value": round(pct)}}}
            elif reader == "gpu":
                out = {"gpu": read_gpu()}
            elif reader == "ram":
                out = {"ram": read_ram()}
            elif reader == "battery":
                out = {"battery": read_battery()}
            elif reader == "audio":
                out = read_audio()
            elif reader == "music":
                out = {"music": read_music(self._running())}
            elif reader == "obs":
                out = {"obs": read_obs(self._base_dir(), self._running())}
            elif reader.startswith("command|"):
                command = reader.split("|", 2)[2]
                with self._lock:
                    allowed = command in self._commands
                out = {reader: read_command(command, min(COMMAND_TIMEOUT, max(2.0, _every(reader))))
                       if allowed else {"error": t("save the key to run its command"), "unsaved": True}}
            else:
                out = {}
            msg = ""
        except Exception as e:  # a reading that fails shows a dash; the key keeps working
            msg = f"{type(e).__name__}: {e}"
            names = ("volume", "mic") if reader == "audio" else (reader,)
            out = {n: {"error": t("cannot read it now")} for n in names}
        if msg and self._errors.get(reader) != msg:
            log.warning("live %s: %s", reader, msg)
        self._errors[reader] = msg
        now = time.monotonic()
        with self._lock:
            for name, reading in out.items():
                hist = self._history.setdefault(name, collections.deque(maxlen=HISTORY))
                if stale:
                    hist.clear()
                if reading.get("value") is not None:
                    hist.append(reading["value"])
                self._readings[name] = reading
            self._last[reader] = now
            self._due[reader] = started + _every(reader)
            self._busy.discard(reader)
            self.version += 1

    # -------------------------------------------------- what a key shows
    def face(self, spec: Optional[dict]) -> Optional[dict]:
        """What render.render_key needs to draw a live key now, with a "sig" that changes
        when the picture would; None for a key that is not live."""
        lv = (spec or {}).get("live")
        if lv is None:
            return None
        if validate(lv):  # half set up in the configurator (no command yet): a dash, not "{output}"
            return {"style": "text", "text": "–", "level": 0, "sig": "unset"} if isinstance(lv, dict) else None
        source = lv["source"]
        if source == "clock":
            reading, hist = read_clock(), ()
        else:
            key = _reader_key(lv)[1]
            with self._lock:
                reading = self._readings.get(key)
                hist = tuple(self._history.get(key, ()))
        waiting = reading is None
        reading = reading or {}
        fields = {f: "…" if waiting else "–" for f in FIELDS[source]}
        fields.update(reading.get("fields") or {})
        value = reading.get("value")
        if lv.get("style") == "meter":
            label = (spec or {}).get("label", "")
            face = {"style": "meter", "title": "" if (spec or {}).get("hide_label") else
                    (label if label else meter_title(source)),
                    "value": None if value is None else round(value), "unit": "" if source == "command" else "%",
                    "note": reading.get("note", ""),
                    "level": reading.get("level", 0), "dim": bool(reading.get("dim")),
                    "history": tuple(round(v, 1) for v in hist)}
        else:
            label = (spec or {}).get("label") or default_text(source)
            text = fill(label, fields).strip()
            if not text or text == "–" and reading.get("error"):
                text = reading.get("error") or "–"
            face = {"style": "text", "text": "" if (spec or {}).get("hide_label") else text,
                    "level": reading.get("level", 0) if lv.get("alert", True) else 0}
        face["sig"] = hashlib.sha1(json.dumps(face, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        return face

    def status(self) -> dict:
        with self._lock:
            wanted = sorted(self._wanted(time.monotonic()))
            return {"sources": [w.split("|", 1)[0] for w in wanted],
                    "errors": {k: v for k, v in self._errors.items() if v and k in wanted}}
