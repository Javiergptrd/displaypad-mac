"""Macros: record keyboard and mouse input, then play it back from a pad key.

  {"type": "macro", "steps": [...],
   "play": "once" | "repeat" | "while_held" | "toggle",   (default "once")
   "times": 3,                                            (for "repeat": 1 to 1000)
   "delays": "recorded" | "fixed" | "none",               (default "recorded")
   "delay_ms": 50}                                        (for "fixed": the wait between steps)

  once        plays it one time
  repeat      plays it "times" times
  while_held  plays it at least once, then again and again while the pad key is held
  toggle      one press starts it looping, the next press stops it

Steps, played in order:
  {"do": "key_down", "key": "rcmd"}, {"do": "key_up", "key": "rcmd"}   (any key name, or "code:110")
  {"do": "keys", "keys": "cmd+c"}                a whole shortcut, pressed and let go
  {"do": "text", "text": "Hello"}
  {"do": "click", "x": 640, "y": 400, "button": "left", "count": 2}    (no x/y: where the pointer is)
  {"do": "mouse_down", "x": .., "y": .., "button": "right"}, {"do": "mouse_up", ...}
  {"do": "move", "x": .., "y": ..}               (with a button down it drags)
  {"do": "scroll", "dy": -120, "dx": 0, "x": .., "y": ..}   (pixels; positive dy scrolls up)
  {"do": "wait", "ms": 250}

Buttons: left, right, middle, back, forward. Positions are screen points from the
top-left corner of the main display, the way macOS counts them.
"""

import ctypes
import json
import logging
import math
import os
import threading
import time

from . import macos
from .i18n import t

log = logging.getLogger(__name__)

_cg, _cf, vp = macos._cg, macos._cf, macos.vp

MAX_STEPS = 5000
MAX_RECORD_SECONDS = 600
COUNTDOWN = 3          # seconds before a recording (or a test) starts: time to switch apps
MIN_WAIT_MS = 5        # shorter gaps between recorded events are left out
FAST_GAP = 0.004       # seconds between steps when delays are "none"
PLAY_MODES = ("once", "repeat", "while_held", "toggle")
DELAY_MODES = ("recorded", "fixed", "none")
STEP_KINDS = ("key_down", "key_up", "keys", "text", "click", "mouse_down", "mouse_up", "move",
              "scroll", "wait")


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_TAP_CALLBACK = ctypes.CFUNCTYPE(vp, vp, ctypes.c_uint32, vp, vp)

_cg.CGEventCreate.restype = vp
_cg.CGEventCreate.argtypes = [vp]
_cg.CGEventCreateMouseEvent.restype = vp
_cg.CGEventCreateMouseEvent.argtypes = [vp, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
_cg.CGEventCreateScrollWheelEvent2.restype = vp
_cg.CGEventCreateScrollWheelEvent2.argtypes = [vp, ctypes.c_uint32, ctypes.c_uint32,
                                               ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
_cg.CGEventSetIntegerValueField.argtypes = [vp, ctypes.c_uint32, ctypes.c_int64]
_cg.CGEventGetIntegerValueField.restype = ctypes.c_int64
_cg.CGEventGetIntegerValueField.argtypes = [vp, ctypes.c_uint32]
_cg.CGEventGetFlags.restype = ctypes.c_uint64
_cg.CGEventGetFlags.argtypes = [vp]
_cg.CGEventGetLocation.restype = CGPoint
_cg.CGEventGetLocation.argtypes = [vp]
_cg.CGEventTapCreate.restype = vp
_cg.CGEventTapCreate.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64,
                                 _TAP_CALLBACK, vp]
_cg.CGEventTapEnable.argtypes = [vp, ctypes.c_bool]
_cf.CFMachPortCreateRunLoopSource.restype = vp
_cf.CFMachPortCreateRunLoopSource.argtypes = [vp, vp, ctypes.c_long]
_cf.CFMachPortInvalidate.argtypes = [vp]
_cf.CFRunLoopGetCurrent.restype = vp
_cf.CFRunLoopAddSource.argtypes = [vp, vp, vp]
_cf.CFRunLoopRemoveSource.argtypes = [vp, vp, vp]
_cf.CFRunLoopRunInMode.restype = ctypes.c_int32
_cf.CFRunLoopRunInMode.argtypes = [vp, ctypes.c_double, ctypes.c_bool]
_COMMON_MODES = vp.in_dll(_cf, "kCFRunLoopCommonModes")
_DEFAULT_MODE = vp.in_dll(_cf, "kCFRunLoopDefaultMode")

# CGEventType values
_LEFT_DOWN, _LEFT_UP, _RIGHT_DOWN, _RIGHT_UP, _MOVED, _LEFT_DRAG, _RIGHT_DRAG = 1, 2, 3, 4, 5, 6, 7
_KEY_DOWN, _KEY_UP, _FLAGS = 10, 11, 12
_SCROLL = 22
_OTHER_DOWN, _OTHER_UP, _OTHER_DRAG = 25, 26, 27
_TAP_DISABLED = (0xFFFFFFFE, 0xFFFFFFFF)  # by timeout, by user input

# CGEventField values
_CLICK_STATE, _BUTTON_NUMBER, _AUTOREPEAT, _KEYCODE = 1, 3, 8, 9
_SCROLL_LINES_Y, _SCROLL_LINES_X = 11, 12
_SOURCE_PID = 41
_SCROLL_POINTS_Y, _SCROLL_POINTS_X = 96, 97

BUTTONS = {"left": 0, "right": 1, "middle": 2, "back": 3, "forward": 4}
_BUTTON_NAMES = {v: k for k, v in BUTTONS.items()}
_DOWN = {"left": _LEFT_DOWN, "right": _RIGHT_DOWN}
_UP = {"left": _LEFT_UP, "right": _RIGHT_UP}
_DRAG = {"left": _LEFT_DRAG, "right": _RIGHT_DRAG}
_SIDE_BITS = 0x207F  # device-dependent modifier bits: which side of a modifier is down

# ------------------------------------------------------------------ key names

_MOD_CODES = {code: name for name, (code, _, _) in macos.MOD_KEYS.items()}
_KEY_NAMES = {}
for _name, _code in macos.KEYCODES.items():
    _KEY_NAMES.setdefault(_code, _name)
_KEY_NAMES.update({36: "return", 51: "backspace", 53: "esc"})
_KEY_NAMES.update(_MOD_CODES)


def key_code(name):
    """'a', 'f19', 'rcmd', 'code:110' -> (virtual key code, modifier name or None). Raises ValueError."""
    n = str(name).strip().lower()
    n = macos.MOD_ALIASES.get(n, n)
    if n in macos.MOD_KEYS:
        return macos.MOD_KEYS[n][0], n
    if n in macos.KEYCODES:
        return macos.KEYCODES[n], None
    if n.startswith("code:") and n[5:].isdigit() and int(n[5:]) < 128:
        code = int(n[5:])
        return code, _MOD_CODES.get(code)
    raise ValueError(f"unknown key '{name}'")


def key_name(code: int) -> str:
    return _KEY_NAMES.get(code, f"code:{code}")


def pointer():
    """Where the mouse pointer is now, in screen points."""
    ev = _cg.CGEventCreate(None)
    p = _cg.CGEventGetLocation(ev)
    _cf.CFRelease(ev)
    return p.x, p.y


# ----------------------------------------------------------------- validation

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _whole(v, lo, hi) -> bool:
    return _num(v) and float(v).is_integer() and lo <= v <= hi


def _step_problem(step) -> str:
    if not isinstance(step, dict):
        return t("each step must be an object like {\"do\": \"click\"}")
    do = step.get("do")
    if do not in STEP_KINDS:
        return t("unknown step “{do}”", do=do)
    if do in ("key_down", "key_up"):
        try:
            key_code(step.get("key", ""))
        except ValueError:
            return t("unknown key “{key}”", key=step.get("key", ""))
    elif do == "keys":
        try:
            macos.parse_shortcut(step.get("keys", ""))
        except ValueError:
            return t("invalid shortcut “{keys}”", keys=step.get("keys", ""))
    elif do == "text":
        if not isinstance(step.get("text"), str) or not step["text"]:
            return t("the text is empty")
    elif do == "wait":
        if not _whole(step.get("ms"), 0, 600000):
            return t("the wait must be a number of milliseconds")
    elif do == "scroll":
        if not all(_whole(step.get(k, 0), -100000, 100000) for k in ("dx", "dy")):
            return t("dx and dy must be whole numbers")
    if do in ("click", "mouse_down", "mouse_up", "move", "scroll"):
        if do == "move" or "x" in step or "y" in step:
            if not (_num(step.get("x")) and _num(step.get("y"))):
                return t("x and y must be numbers")
    if do in ("click", "mouse_down", "mouse_up") and step.get("button", "left") not in BUTTONS:
        return t("unknown mouse button “{button}” (use left, right, middle, back or forward)",
                 button=step.get("button"))
    if do == "click" and not _whole(step.get("count", 1), 1, 3):
        return t("count must be 1, 2 or 3")
    return ""


def validate(action: dict) -> str:
    """Error message for a macro action, or '' if it is valid."""
    steps = action.get("steps")
    if not isinstance(steps, list) or not steps:
        return t("the macro has no steps yet: record it or add steps")
    if len(steps) > MAX_STEPS:
        return t("a macro can have up to {n} steps", n=MAX_STEPS)
    for i, step in enumerate(steps, 1):
        problem = _step_problem(step)
        if problem:
            return t("macro step {n}: {problem}", n=i, problem=problem)
    if all(s.get("do") == "wait" for s in steps):
        return t("the macro only waits: add a step that does something")
    if action.get("play", "once") not in PLAY_MODES:
        return t("play must be once, repeat, while_held or toggle")
    if action.get("play") == "repeat" and not _whole(action.get("times", 1), 1, 1000):
        return t("times must be a whole number from 1 to 1000")
    if action.get("delays", "recorded") not in DELAY_MODES:
        return t("delays must be recorded, fixed or none")
    if action.get("delays") == "fixed" and not _whole(action.get("delay_ms", 50), 0, 10000):
        return t("delay_ms must be a number of milliseconds from 0 to 10000")
    return ""


# ------------------------------------------------------------------- playback

class _Player:
    """Plays steps and remembers what it holds down, so a stop never leaves a key or button stuck."""

    def __init__(self, stop: threading.Event):
        self.stop = stop
        self.mods = []      # modifier names held, in the order they went down
        self.keys = set()   # other key codes held
        self.buttons = []   # mouse buttons held
        self.pos = None

    def flags(self) -> int:
        return macos._flags(self.mods)

    def sleep(self, seconds: float) -> bool:
        """False if the macro was stopped meanwhile."""
        return not self.stop.wait(seconds) if seconds > 0 else not self.stop.is_set()

    def key(self, name, down: bool):
        code, mod = key_code(name)
        if mod:
            if down and mod not in self.mods:
                self.mods.append(mod)
                macos._post(code, True, self.flags())
            elif not down and mod in self.mods:
                self.mods.remove(mod)
                macos._up(code, self.flags())
        elif down:
            self.keys.add(code)
            macos._post(code, True, self.flags())
        elif code in self.keys:
            self.keys.discard(code)
            macos._up(code, self.flags())

    def _mouse(self, kind: int, x: float, y: float, button: str = "left", clicks: int = 0):
        ev = _cg.CGEventCreateMouseEvent(None, kind, CGPoint(x, y), BUTTONS[button])
        if not ev:
            raise OSError("could not build a mouse event")
        if clicks:
            _cg.CGEventSetIntegerValueField(ev, _CLICK_STATE, clicks)
        if self.mods:
            _cg.CGEventSetFlags(ev, self.flags())
        _cg.CGEventPost(macos._kCGHIDEventTap, ev)
        _cf.CFRelease(ev)
        self.pos = (x, y)

    def _point(self, step):
        if _num(step.get("x")) and _num(step.get("y")):
            return float(step["x"]), float(step["y"])
        return self.pos or pointer()

    def move(self, x: float, y: float):
        if not self.buttons:
            self._mouse(_MOVED, x, y)
            return
        held = self.buttons[-1]  # glide there, so apps see a drag and not a jump
        sx, sy = self.pos or pointer()
        n = max(1, min(25, int(math.hypot(x - sx, y - sy) / 12)))
        for i in range(1, n + 1):
            self._mouse(_DRAG.get(held, _OTHER_DRAG), sx + (x - sx) * i / n, sy + (y - sy) * i / n, held)
            if i < n:
                time.sleep(0.006)

    def _go(self, x, y):
        if self.pos != (x, y):
            self.move(x, y)
            time.sleep(0.01)  # let the app under the pointer notice it (hover) before the click

    def button(self, step, down: bool):
        b = step.get("button", "left")
        x, y = self._point(step)
        if down:
            self._go(x, y)
            self._mouse(_DOWN.get(b, _OTHER_DOWN), x, y, b, 1)
            if b not in self.buttons:
                self.buttons.append(b)
        elif b in self.buttons:
            self._go(x, y)
            self._mouse(_UP.get(b, _OTHER_UP), x, y, b, 1)
            self.buttons.remove(b)

    def click(self, step):
        b = step.get("button", "left")
        x, y = self._point(step)
        self._go(x, y)
        count = int(step.get("count", 1))
        for n in range(1, count + 1):
            self._mouse(_DOWN.get(b, _OTHER_DOWN), x, y, b, n)
            time.sleep(0.012)
            self._mouse(_UP.get(b, _OTHER_UP), x, y, b, n)
            if n < count:
                time.sleep(0.04)

    def scroll(self, step):
        if _num(step.get("x")) and _num(step.get("y")):
            self._go(float(step["x"]), float(step["y"]))
        dy, dx = int(step.get("dy", 0)), int(step.get("dx", 0))
        chunks = max(1, math.ceil(max(abs(dy), abs(dx)) / 40))  # big scrolls in small, smooth steps
        sent_y = sent_x = 0
        for i in range(1, chunks + 1):
            cy, cx = round(dy * i / chunks) - sent_y, round(dx * i / chunks) - sent_x
            sent_y, sent_x = sent_y + cy, sent_x + cx
            ev = _cg.CGEventCreateScrollWheelEvent2(None, 0, 2, cy, cx, 0)  # 0: pixel units
            if self.mods:
                _cg.CGEventSetFlags(ev, self.flags())
            _cg.CGEventPost(macos._kCGHIDEventTap, ev)
            _cf.CFRelease(ev)
            if i < chunks and not self.sleep(0.008):
                return

    def run(self, step):
        do = step["do"]
        if do in ("key_down", "key_up"):
            self.key(step["key"], do == "key_down")
        elif do == "keys":
            macos.press_shortcut(step["keys"])
        elif do == "text":
            macos.type_text(step["text"])
        elif do == "click":
            self.click(step)
        elif do in ("mouse_down", "mouse_up"):
            self.button(step, do == "mouse_down")
        elif do == "move":
            self.move(float(step["x"]), float(step["y"]))
        elif do == "scroll":
            self.scroll(step)

    def release(self):
        for b in reversed(self.buttons):
            x, y = self.pos or pointer()
            self._mouse(_UP.get(b, _OTHER_UP), x, y, b, 1)
        self.buttons.clear()
        for code in list(self.keys):
            macos._up(code, self.flags())
        self.keys.clear()
        while self.mods:
            m = self.mods.pop()
            macos._up(macos.MOD_KEYS[m][0], self.flags())


def _play_pass(player: _Player, steps: list, delays: str, delay_ms: int, first: bool) -> int:
    """One pass through the steps. Returns how many steps did something; -1 if stopped."""
    gap = {"fixed": delay_ms / 1000, "none": FAST_GAP}.get(delays)  # None: the recorded waits
    done = 0
    for step in steps:
        if player.stop.is_set():
            return -1
        if step["do"] == "wait":
            if gap is None and not player.sleep(step["ms"] / 1000):
                return -1
            continue
        if gap is not None and not first and not player.sleep(gap):
            return -1
        first = False
        player.run(step)
        done += 1
    return done


class _Run:
    def __init__(self, mode: str):
        self.mode, self.started, self.stop = mode, time.monotonic(), threading.Event()


_running = {}
_running_lock = threading.Lock()


def playing() -> int:
    with _running_lock:
        return len(_running)


def stop_all() -> int:
    """Stop every macro that is playing (toggled on, held, or still going)."""
    with _running_lock:
        runs = list(_running.values())
    for run in runs:
        run.stop.set()
    return len(runs)


def keep_only(actions):
    """The config changed: stop playing macros that are no longer on any key (edited or removed),
    since their key could not stop them any more. The others keep going."""
    keep = {json.dumps(a, sort_keys=True) for a in actions if isinstance(a, dict) and a.get("type") == "macro"}
    with _running_lock:
        gone = [run for ident, run in _running.items() if not ident.startswith("test:") and ident not in keep]
    for run in gone:
        run.stop.set()
    if gone:
        log.info("stopped %d macro(s) that changed in the config", len(gone))


def play(action: dict, released: threading.Event = None, test: bool = False) -> dict:
    """Play a macro action. released: set when the pad key is let go (for "while_held").

    test: play it once, after a COUNTDOWN so the user can switch to the app it is for.
    A second press of a "toggle" macro stops it; of any other macro while it is still
    playing, it is ignored (two copies would mix their keys)."""
    mode = action.get("play", "once")
    ident = ("test:" if test else "") + json.dumps(action, sort_keys=True)
    with _running_lock:
        run = _running.get(ident)
        if run:
            if mode == "toggle" and not test:
                run.stop.set()
                log.info("macro stopped (its key was pressed again)")
            else:
                log.info("macro is still playing; press ignored")
            return {"steps": 0, "seconds": 0, "passes": 0, "stopped": False, "ignored": True}
        run = _running[ident] = _Run(mode)
    steps = action["steps"]
    delays = action.get("delays", "recorded")
    delay_ms = int(action.get("delay_ms", 50))
    times = int(action.get("times", 1)) if mode == "repeat" else 1
    player = _Player(run.stop)
    first_pass = threading.Event()
    if mode == "while_held" and released is not None and not test:
        def let_go():  # stop as soon as the key is up, but never before one whole pass
            released.wait(macos.MAX_HOLD)
            first_pass.wait()
            run.stop.set()
        threading.Thread(target=let_go, daemon=True).start()
    passes = done = 0
    began = time.monotonic()
    try:
        if test and not player.sleep(COUNTDOWN):
            return {"steps": 0, "seconds": 0, "passes": 0, "stopped": True}
        began = time.monotonic()
        while True:
            n = _play_pass(player, steps, delays, delay_ms, first=passes == 0)
            if n < 0:
                break
            done += n
            passes += 1
            first_pass.set()
            if test or mode == "once" or (mode == "repeat" and passes >= times):
                break
            if mode == "while_held" and (released is None or released.is_set()):
                break
            if not player.sleep(0.005):  # never spin: a loop of quick steps still yields
                break
    finally:
        first_pass.set()
        try:
            player.release()
        finally:
            with _running_lock:
                _running.pop(ident, None)
    return {"steps": done, "seconds": round(time.monotonic() - began, 2), "passes": passes,
            "stopped": run.stop.is_set()}


# ------------------------------------------------------------------ recording

class Recorder:
    """Records the keyboard and mouse through a listen-only event tap (Input Monitoring).

    States: idle -> countdown -> recording -> done (or error). Stopped by the configurator's
    Stop button, by any pad key, or by the limits."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state, self.error, self.stopped_by = "idle", "", ""
        self.steps, self.options = [], {}
        self._stop = threading.Event()
        self._tap = None
        self._callback = _TAP_CALLBACK(self._on_event)
        self._started = self._ended = self._go_at = 0.0

    def active(self) -> bool:
        return self.state in ("countdown", "recording")

    def start(self, options: dict = None):
        with self._lock:
            if self.active():
                raise RuntimeError(t("a recording is already running"))
            if macos.input_monitoring_status() != "granted":
                raise PermissionError(t("the Input Monitoring permission is needed to record"))
            o = options or {}
            self.options = {"keys": o.get("keys", True) is not False, "mouse": o.get("mouse", True) is not False,
                            "moves": bool(o.get("moves", False))}
            self.state, self.error, self.stopped_by, self.steps = "countdown", "", "", []
            self._stop.clear()
            self._steps, self._keys, self._buttons = [], set(), {}
            self._last_t = self._last_move = None
            self._scroll_start = 0.0
            self._go_at = time.monotonic() + COUNTDOWN
            threading.Thread(target=self._run, daemon=True, name="macro-recorder").start()

    def stop(self, by: str = "button"):
        if self.active():
            self.stopped_by = self.stopped_by or by
            self._stop.set()

    def cancel(self):
        self.stop("cancel")

    def status(self) -> dict:
        s = {"state": self.state, "options": self.options}
        if self.state == "countdown":
            s["seconds_left"] = max(0.0, round(self._go_at - time.monotonic(), 1))
        if self.state in ("recording", "done"):
            steps = self._steps if self.state == "recording" else self.steps
            s["count"] = sum(1 for st in list(steps) if st.get("do") != "wait")
            s["elapsed"] = round((time.monotonic() if self.state == "recording" else self._ended) - self._started, 1)
        if self.state == "done":
            s["steps"], s["stopped_by"] = self.steps, self.stopped_by
        if self.state == "error":
            s["error"] = self.error
        return s

    # -- the tap thread
    def _run(self):
        tap = src = loop = None
        try:
            if self._stop.wait(COUNTDOWN):
                self.state = "idle"
                return
            mask = 0
            for kind in (_LEFT_DOWN, _LEFT_UP, _RIGHT_DOWN, _RIGHT_UP, _MOVED, _LEFT_DRAG, _RIGHT_DRAG,
                         _KEY_DOWN, _KEY_UP, _FLAGS, _SCROLL, _OTHER_DOWN, _OTHER_UP, _OTHER_DRAG):
                mask |= 1 << kind
            # session tap, at the head, listen only: nothing is changed or blocked
            tap = self._tap = _cg.CGEventTapCreate(1, 0, 1, mask, self._callback, None)
            if not tap:
                raise PermissionError(t("could not start recording: allow Input Monitoring for DisplayPad, "
                                        "then restart it"))
            src = _cf.CFMachPortCreateRunLoopSource(None, tap, 0)
            loop = _cf.CFRunLoopGetCurrent()
            _cf.CFRunLoopAddSource(loop, src, _COMMON_MODES)
            _cg.CGEventTapEnable(tap, True)
            self._started = time.monotonic()
            self.state = "recording"
            log.info("macro recording started")
            while not self._stop.is_set():
                _cf.CFRunLoopRunInMode(_DEFAULT_MODE, 0.1, False)
                if time.monotonic() - self._started > MAX_RECORD_SECONDS:
                    self.stopped_by = self.stopped_by or "limit"
                    break
            self._ended = time.monotonic()
            if self.stopped_by == "cancel":  # nothing typed during it is kept
                self._steps, self.state = [], "idle"
                return
            self.steps = _finish(self._steps, trim_stop_click=self.stopped_by == "button")
            self.state = "done"
            log.info("macro recorded: %d steps, stopped by %s", len(self.steps), self.stopped_by)
        except Exception as e:
            log.warning("macro recording failed: %s", e)
            self.error, self.state = str(e), "error"
        finally:
            if tap:
                _cg.CGEventTapEnable(tap, False)
                _cf.CFMachPortInvalidate(tap)
            if src:
                if loop:
                    _cf.CFRunLoopRemoveSource(loop, src, _COMMON_MODES)
                _cf.CFRelease(src)
            if tap:
                _cf.CFRelease(tap)
                if self._tap == tap:
                    self._tap = None

    def _on_event(self, proxy, kind, ev, refcon):
        try:
            if kind in _TAP_DISABLED:  # macOS turns a slow tap off; turn it back on
                if self._tap:
                    _cg.CGEventTapEnable(self._tap, True)
            elif self.state == "recording" and not self._stop.is_set() \
                    and _cg.CGEventGetIntegerValueField(ev, _SOURCE_PID) != os.getpid():  # not our own macros
                self._event(kind, ev, time.monotonic())
        except Exception:
            log.exception("macro recorder")
        return ev

    def _emit(self, step: dict, now: float):
        if self._last_t is not None:
            gap = round((now - self._last_t) * 1000)
            if gap >= MIN_WAIT_MS:
                self._steps.append({"do": "wait", "ms": gap})
        self._steps.append(step)
        self._last_t = now
        if len(self._steps) >= MAX_STEPS - 1:
            self.stopped_by = self.stopped_by or "limit"
            self._stop.set()

    def _last_action(self, before: int = None):
        """Index of the last step that is not a wait (before an index), or None."""
        i = (len(self._steps) if before is None else before) - 1
        while i >= 0 and self._steps[i]["do"] == "wait":
            i -= 1
        return i if i >= 0 else None

    def _event(self, kind: int, ev, now: float):
        o = self.options
        if kind in (_KEY_DOWN, _KEY_UP, _FLAGS):
            if not o["keys"]:
                return
            code = _cg.CGEventGetIntegerValueField(ev, _KEYCODE)
            if kind == _FLAGS:
                mod = _MOD_CODES.get(code)
                if not mod:
                    return  # caps lock and other lock keys: not replayable as a press
                _, mask, side = macos.MOD_KEYS[mod]
                flags = _cg.CGEventGetFlags(ev)
                down = bool(flags & side) if side and flags & _SIDE_BITS else \
                    (bool(flags & mask) if mod == "fn" else code not in self._keys)
            else:
                down = kind == _KEY_DOWN
                if down and _cg.CGEventGetIntegerValueField(ev, _AUTOREPEAT):
                    return
            if down and code not in self._keys:
                self._keys.add(code)
                self._emit({"do": "key_down", "key": key_name(code)}, now)
            elif not down and code in self._keys:  # ups of keys held before the recording are left out
                self._keys.discard(code)
                self._emit({"do": "key_up", "key": key_name(code)}, now)
            return
        if not o["mouse"]:
            return
        p = _cg.CGEventGetLocation(ev)
        x, y = int(round(p.x)), int(round(p.y))
        if kind in (_LEFT_DOWN, _RIGHT_DOWN, _OTHER_DOWN, _LEFT_UP, _RIGHT_UP, _OTHER_UP,
                    _LEFT_DRAG, _RIGHT_DRAG, _OTHER_DRAG):
            number = {_LEFT_DOWN: 0, _LEFT_UP: 0, _LEFT_DRAG: 0, _RIGHT_DOWN: 1, _RIGHT_UP: 1,
                      _RIGHT_DRAG: 1}.get(kind)
            if number is None:
                number = _cg.CGEventGetIntegerValueField(ev, _BUTTON_NUMBER)
            b = _BUTTON_NAMES.get(number)
            if b is None:
                return
            if kind in (_LEFT_DOWN, _RIGHT_DOWN, _OTHER_DOWN):
                self._buttons[b] = _cg.CGEventGetIntegerValueField(ev, _CLICK_STATE) or 1
                self._last_move = (x, y, now)
                self._emit({"do": "mouse_down", "button": b, "x": x, "y": y}, now)
            elif kind in (_LEFT_UP, _RIGHT_UP, _OTHER_UP):
                clicks = self._buttons.pop(b, None)
                if clicks is None:
                    return  # pressed before the recording started
                self._button_up(b, x, y, clicks, now)
            elif b in self._buttons:
                self._moved(x, y, now)
        elif kind == _MOVED:
            if o["moves"]:
                self._moved(x, y, now)
        elif kind == _SCROLL:
            dy = _cg.CGEventGetIntegerValueField(ev, _SCROLL_POINTS_Y)
            dx = _cg.CGEventGetIntegerValueField(ev, _SCROLL_POINTS_X)
            if not dy and not dx:
                dy = _cg.CGEventGetIntegerValueField(ev, _SCROLL_LINES_Y) * 10
                dx = _cg.CGEventGetIntegerValueField(ev, _SCROLL_LINES_X) * 10
            if not dy and not dx:
                return
            last = self._steps[-1] if self._steps else None
            if last and last["do"] == "scroll" and now - self._last_t < 0.15 \
                    and now - self._scroll_start < 0.5 and (last.get("x"), last.get("y")) == (x, y):
                last["dy"] += dy
                last["dx"] += dx
                self._last_t = now
            else:
                self._scroll_start = now
                self._emit({"do": "scroll", "dy": dy, "dx": dx, "x": x, "y": y}, now)

    def _moved(self, x: int, y: int, now: float):
        if self._last_move:
            lx, ly, lt = self._last_move
            if math.hypot(x - lx, y - ly) < 4 or now - lt < 0.03:
                return
        self._last_move = (x, y, now)
        self._emit({"do": "move", "x": x, "y": y}, now)

    def _button_up(self, b: str, x: int, y: int, clicks: int, now: float):
        i = self._last_action()
        down = self._steps[i] if i is not None else None
        if not (down and down["do"] == "mouse_down" and down["button"] == b
                and abs(down["x"] - x) <= 3 and abs(down["y"] - y) <= 3):
            self._emit({"do": "mouse_up", "button": b, "x": x, "y": y}, now)
            return
        # a press and release in one spot is a click; the second click of a double click joins the first
        del self._steps[i:]
        j = self._last_action()
        before = self._steps[j] if j is not None else None
        if clicks > 1 and before and before["do"] == "click" and before.get("button") == b \
                and before.get("count", 1) == clicks - 1 and abs(before["x"] - x) <= 3 and abs(before["y"] - y) <= 3:
            del self._steps[j + 1:]
            before["count"] = clicks
        else:
            self._steps.append({"do": "click", "button": b, "x": down["x"], "y": down["y"]})
        self._last_t = now


def _finish(steps: list, trim_stop_click: bool) -> list:
    """Tidy a recording: drop the click on the configurator's Stop button, let go of keys still down."""
    steps = [dict(s) for s in steps]

    def strip_tail():
        while steps and steps[-1]["do"] in ("wait", "move"):
            steps.pop()

    if trim_stop_click:
        strip_tail()
        if steps and steps[-1]["do"] in ("click", "mouse_down"):
            steps.pop()
        elif steps and steps[-1]["do"] == "mouse_up":
            b = steps.pop()["button"]
            while steps and not (steps[-1]["do"] == "mouse_down" and steps[-1]["button"] == b):
                steps.pop()
            if steps:
                steps.pop()
        strip_tail()
    while steps and steps[-1]["do"] == "wait":
        steps.pop()
    down = []
    for s in steps:
        if s["do"] == "key_down" and s["key"] not in down:
            down.append(s["key"])
        elif s["do"] == "key_up" and s["key"] in down:
            down.remove(s["key"])
    steps.extend({"do": "key_up", "key": k} for k in reversed(down))
    return steps


recorder = Recorder()
