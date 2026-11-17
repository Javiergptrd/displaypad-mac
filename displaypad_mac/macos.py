"""macOS glue: IOKit access to the DisplayPad keyboard interface, privacy
permissions (TCC), and synthetic keyboard / media-key events.

Everything here uses ctypes against system frameworks, so no PyObjC is needed.
"""

import ctypes
import ctypes.util
import logging
import time

log = logging.getLogger(__name__)

_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
_io = ctypes.CDLL(ctypes.util.find_library("IOKit"))
_cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
_as = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
_objc = ctypes.CDLL(ctypes.util.find_library("objc"))
ctypes.CDLL(ctypes.util.find_library("AppKit"))  # registers NSEvent with the objc runtime

vp = ctypes.c_void_p

_cf.CFStringCreateWithCString.restype = vp
_cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
_cf.CFNumberCreate.restype = vp
_cf.CFNumberCreate.argtypes = [vp, ctypes.c_int, vp]
_cf.CFNumberGetValue.argtypes = [vp, ctypes.c_int, vp]
_cf.CFDictionaryCreateMutable.restype = vp
_cf.CFDictionaryCreateMutable.argtypes = [vp, ctypes.c_long, vp, vp]
_cf.CFDictionarySetValue.argtypes = [vp, vp, vp]
_cf.CFSetGetCount.restype = ctypes.c_long
_cf.CFSetGetCount.argtypes = [vp]
_cf.CFSetGetValues.argtypes = [vp, vp]
_cf.CFRelease.argtypes = [vp]

_io.IOHIDManagerCreate.restype = vp
_io.IOHIDManagerCreate.argtypes = [vp, ctypes.c_uint32]
_io.IOHIDManagerSetDeviceMatching.argtypes = [vp, vp]
_io.IOHIDManagerCopyDevices.restype = vp
_io.IOHIDManagerCopyDevices.argtypes = [vp]
_io.IOHIDDeviceGetProperty.restype = vp
_io.IOHIDDeviceGetProperty.argtypes = [vp, vp]
_io.IOHIDDeviceOpen.restype = ctypes.c_uint32
_io.IOHIDDeviceOpen.argtypes = [vp, ctypes.c_uint32]
_io.IOHIDDeviceClose.restype = ctypes.c_uint32
_io.IOHIDDeviceClose.argtypes = [vp, ctypes.c_uint32]
_io.IOHIDDeviceSetReport.restype = ctypes.c_uint32
_io.IOHIDDeviceSetReport.argtypes = [vp, ctypes.c_int, ctypes.c_long, ctypes.c_char_p, ctypes.c_long]
_io.IOHIDCheckAccess.restype = ctypes.c_uint32
_io.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
_io.IOHIDRequestAccess.restype = ctypes.c_bool
_io.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]

_kUTF8 = 0x08000100
_kSInt32 = 3

IORETURN_NAMES = {
    0xE00002E2: "kIOReturnNotPermitted",
    0xE00002C5: "kIOReturnExclusiveAccess",
    0xE00002C1: "kIOReturnNoPrivileges",
    0xE00002CD: "kIOReturnNotOpen",
    0xE00002D6: "kIOReturnTimeout",
}


def _ioret(code: int) -> str:
    return f"0x{code:08x} {IORETURN_NAMES.get(code, '')}".strip()


def _cfstr(s: str):
    return _cf.CFStringCreateWithCString(None, s.encode(), _kUTF8)


def _cfnum(n: int):
    v = ctypes.c_int32(n)
    return _cf.CFNumberCreate(None, _kSInt32, ctypes.byref(v))


def _get_int(dev, key: str):
    ref = _io.IOHIDDeviceGetProperty(dev, _cfstr(key))
    if not ref:
        return None
    v = ctypes.c_int32()
    _cf.CFNumberGetValue(ref, _kSInt32, ctypes.byref(v))
    return v.value


class KeyboardInterface:
    """IF0 of the DisplayPad (the HID keyboard interface macOS already drives).

    The firmware ignores everything on the command interface (IF3) until it
    receives output report 3 = [0x03, 0x01] on IF0 ("event reporting mode",
    the SET_REPORT BaseCamp sends on Windows). Linux drivers send it by
    detaching the kernel driver, which needs root on macOS. Opening the
    IOHIDDevice non-exclusively and calling IOHIDDeviceSetReport does the same
    without root, but macOS requires the Input Monitoring permission for it.
    """

    def __init__(self, vid: int, pid: int):
        self.vid, self.pid = vid, pid
        self._mgr = None
        self._set = None
        self._dev = None

    def open(self):
        self._mgr = _io.IOHIDManagerCreate(None, 0)
        match = _cf.CFDictionaryCreateMutable(None, 0, None, None)
        _cf.CFDictionarySetValue(match, _cfstr("VendorID"), _cfnum(self.vid))
        _cf.CFDictionarySetValue(match, _cfstr("ProductID"), _cfnum(self.pid))
        _cf.CFDictionarySetValue(match, _cfstr("PrimaryUsagePage"), _cfnum(0x01))
        _io.IOHIDManagerSetDeviceMatching(self._mgr, match)
        self._set = _io.IOHIDManagerCopyDevices(self._mgr)
        if not self._set or _cf.CFSetGetCount(self._set) < 1:
            raise OSError("DisplayPad keyboard interface (IF0) not found")
        n = _cf.CFSetGetCount(self._set)
        arr = (vp * n)()
        _cf.CFSetGetValues(self._set, arr)
        self._dev = arr[0]
        rc = _io.IOHIDDeviceOpen(self._dev, 0)  # 0 = shared, never seize
        if rc != 0:
            self._dev = None
            hint = ""
            if rc in (0xE00002E2, 0xE00002C1):
                hint = " (grant Input Monitoring to this program in System Settings > Privacy & Security)"
            raise PermissionError(f"Cannot open DisplayPad IF0: {_ioret(rc)}{hint}")

    def enable_event_mode(self):
        if not self._dev:
            raise OSError("IF0 not open")
        rc = _io.IOHIDDeviceSetReport(self._dev, 1, 3, bytes([0x03, 0x01]), 2)  # 1 = output report
        if rc != 0:
            raise OSError(f"SET_REPORT on IF0 failed: {_ioret(rc)}")

    def close(self):
        if self._dev:
            _io.IOHIDDeviceClose(self._dev, 0)
            self._dev = None
        for ref in (self._set, self._mgr):
            if ref:
                _cf.CFRelease(ref)
        self._set = self._mgr = None


# ---------------------------------------------------------------- permissions

_cg.CGPreflightPostEventAccess.restype = ctypes.c_bool
_cg.CGRequestPostEventAccess.restype = ctypes.c_bool
_as.AXIsProcessTrusted.restype = ctypes.c_bool

_ACCESS = {0: "granted", 1: "denied", 2: "not requested yet"}


def input_monitoring_status() -> str:
    return _ACCESS.get(_io.IOHIDCheckAccess(1), "unknown")


def request_input_monitoring() -> bool:
    return bool(_io.IOHIDRequestAccess(1))


def post_events_allowed() -> bool:
    return bool(_cg.CGPreflightPostEventAccess())


def request_post_events() -> bool:
    return bool(_cg.CGRequestPostEventAccess())


# ------------------------------------------------------------ keyboard events

_cg.CGEventCreateKeyboardEvent.restype = vp
_cg.CGEventCreateKeyboardEvent.argtypes = [vp, ctypes.c_uint16, ctypes.c_bool]
_cg.CGEventSetFlags.argtypes = [vp, ctypes.c_uint64]
_cg.CGEventPost.argtypes = [ctypes.c_uint32, vp]
_cg.CGEventKeyboardSetUnicodeString.argtypes = [vp, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]
_kCGHIDEventTap = 0

MODIFIERS = {
    "cmd": 0x100000, "command": 0x100000, "⌘": 0x100000,
    "shift": 0x20000, "⇧": 0x20000,
    "ctrl": 0x40000, "control": 0x40000, "⌃": 0x40000,
    "alt": 0x80000, "option": 0x80000, "opt": 0x80000, "⌥": 0x80000,
    "fn": 0x800000,
}

# US-ANSI virtual key codes (physical positions).
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36,
    "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43,
    "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49, "`": 50,
    "backspace": 51, "delete": 51, "esc": 53, "escape": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113,
    "f16": 106, "f17": 64, "f18": 79, "f19": 80, "f20": 90,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "forwarddelete": 117,
    "left": 123, "right": 124, "down": 125, "up": 126,
}


def parse_shortcut(spec: str):
    """'cmd+shift+4' -> (keycode, flags). Raises ValueError on unknown names."""
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty shortcut")
    flags = 0
    for mod in parts[:-1]:
        if mod not in MODIFIERS:
            raise ValueError(f"unknown modifier '{mod}' in '{spec}'")
        flags |= MODIFIERS[mod]
    key = parts[-1]
    if key not in KEYCODES:
        raise ValueError(f"unknown key '{key}' in '{spec}'")
    return KEYCODES[key], flags


def press_shortcut(spec: str):
    keycode, flags = parse_shortcut(spec)
    for down in (True, False):
        ev = _cg.CGEventCreateKeyboardEvent(None, keycode, down)
        _cg.CGEventSetFlags(ev, flags)
        _cg.CGEventPost(_kCGHIDEventTap, ev)
        _cf.CFRelease(ev)
        time.sleep(0.01)


def type_text(text: str, delay: float = 0.004):
    """Type arbitrary Unicode text, independent of the keyboard layout."""
    for ch in text:
        if ch == "\n":
            press_shortcut("return")
            continue
        units = ch.encode("utf-16-le")
        buf = (ctypes.c_uint16 * (len(units) // 2)).from_buffer_copy(units)
        for down in (True, False):
            ev = _cg.CGEventCreateKeyboardEvent(None, 0, down)
            _cg.CGEventKeyboardSetUnicodeString(ev, len(buf), buf)
            _cg.CGEventPost(_kCGHIDEventTap, ev)
            _cf.CFRelease(ev)
        time.sleep(delay)


# --------------------------------------------------------------- media keys
# Media keys are NSSystemDefined events (subtype 8), built through the objc
# runtime: [NSEvent otherEventWithType:...data1:...] then -CGEvent.

class _NSPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_objc.objc_getClass.restype = vp
_objc.objc_getClass.argtypes = [ctypes.c_char_p]
_objc.sel_registerName.restype = vp
_objc.sel_registerName.argtypes = [ctypes.c_char_p]
_msgsend_addr = ctypes.cast(_objc.objc_msgSend, vp).value
_make_event = ctypes.CFUNCTYPE(
    vp, vp, vp, ctypes.c_ulong, _NSPoint, ctypes.c_ulong, ctypes.c_double,
    ctypes.c_long, vp, ctypes.c_short, ctypes.c_long, ctypes.c_long)(_msgsend_addr)
_get_cgevent = ctypes.CFUNCTYPE(vp, vp, vp)(_msgsend_addr)

MEDIA_KEYS = {
    "volume_up": 0, "volume_down": 1, "brightness_up": 2, "brightness_down": 3,
    "mute": 7, "play": 16, "play_pause": 16, "next": 17, "previous": 18, "prev": 18,
}  # NX_KEYTYPE_* values from IOKit/hidsystem/ev_keymap.h


def press_media_key(name: str):
    if name not in MEDIA_KEYS:
        raise ValueError(f"unknown media key '{name}' (use: {', '.join(sorted(MEDIA_KEYS))})")
    code = MEDIA_KEYS[name]
    cls = _objc.objc_getClass(b"NSEvent")
    sel = _objc.sel_registerName(
        b"otherEventWithType:location:modifierFlags:timestamp:windowNumber:context:subtype:data1:data2:")
    sel_cg = _objc.sel_registerName(b"CGEvent")
    for down in (True, False):
        state = 0xA if down else 0xB
        ns_event = _make_event(cls, sel, 14, _NSPoint(0, 0), state << 8, 0.0, 0, None, 8,
                               (code << 16) | (state << 8), -1)
        if not ns_event:
            raise OSError("could not build media key event")
        _cg.CGEventPost(_kCGHIDEventTap, _get_cgevent(ns_event, sel_cg))
        time.sleep(0.01)
