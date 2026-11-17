"""Brightness of the Mac's screens (not the DisplayPad backlight).

Built-in display: the private DisplayServices framework (what the F1/F2 keys use).
External monitors: DDC/CI over the display cable through IOAVService (Apple
Silicon), VCP code 0x10 = luminance, the same approach as MonitorControl/m1ddc.
"""

import ctypes
import ctypes.util
import logging
import threading
import time
from typing import Dict, List, Optional

log = logging.getLogger(__name__)
vp = ctypes.c_void_p

_cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
_io = ctypes.CDLL(ctypes.util.find_library("IOKit"))
try:
    _ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
    _ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
    _ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
    _ds.DisplayServicesCanChangeBrightness.restype = ctypes.c_bool
    _ds.DisplayServicesCanChangeBrightness.argtypes = [ctypes.c_uint32]
except OSError:
    _ds = None

_cg.CGGetOnlineDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
_cg.CGDisplayIsBuiltin.restype = ctypes.c_bool
_cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]

_cf.CFStringCreateWithCString.restype = vp
_cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
_cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFRelease.argtypes = [vp]
_io.IOServiceMatching.restype = vp
_io.IOServiceMatching.argtypes = [ctypes.c_char_p]
_io.IOServiceGetMatchingServices.argtypes = [ctypes.c_uint32, vp, ctypes.POINTER(ctypes.c_uint32)]
_io.IOIteratorNext.restype = ctypes.c_uint32
_io.IOIteratorNext.argtypes = [ctypes.c_uint32]
_io.IOObjectRelease.argtypes = [ctypes.c_uint32]
_io.IORegistryEntryCreateCFProperty.restype = vp
_io.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint32, vp, vp, ctypes.c_uint32]
try:
    _io.IOAVServiceCreateWithService.restype = vp
    _io.IOAVServiceCreateWithService.argtypes = [vp, ctypes.c_uint32]
    _io.IOAVServiceWriteI2C.restype = ctypes.c_uint32
    _io.IOAVServiceWriteI2C.argtypes = [vp, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32]
    _io.IOAVServiceReadI2C.restype = ctypes.c_uint32
    _io.IOAVServiceReadI2C.argtypes = [vp, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32]
    _HAS_AV = True
except AttributeError:  # Intel Macs
    _HAS_AV = False

_lock = threading.Lock()
_ddc_cache: Dict[int, int] = {}  # external screen index -> last known brightness, if a DDC read fails


# ---------------------------------------------------------------- built-in

def _builtin_ids() -> List[int]:
    ids = (ctypes.c_uint32 * 16)()
    n = ctypes.c_uint32()
    _cg.CGGetOnlineDisplayList(16, ids, ctypes.byref(n))
    return [ids[i] for i in range(n.value)
            if _cg.CGDisplayIsBuiltin(ids[i]) and _ds and _ds.DisplayServicesCanChangeBrightness(ids[i])]


def _builtin_get(did: int) -> Optional[float]:
    b = ctypes.c_float(-1)
    if _ds.DisplayServicesGetBrightness(did, ctypes.byref(b)) != 0:
        return None
    return b.value * 100


def _builtin_set(did: int, percent: float):
    _ds.DisplayServicesSetBrightness(did, ctypes.c_float(max(0.0, min(100.0, percent)) / 100))


# ---------------------------------------------------------------- DDC

def _external_services() -> List[int]:
    if not _HAS_AV:
        return []
    it = ctypes.c_uint32()
    _io.IOServiceGetMatchingServices(0, _io.IOServiceMatching(b"DCPAVServiceProxy"), ctypes.byref(it))
    key = _cf.CFStringCreateWithCString(None, b"Location", 0x08000100)
    found = []
    while True:
        svc = _io.IOIteratorNext(it.value)
        if not svc:
            break
        prop = _io.IORegistryEntryCreateCFProperty(svc, key, None, 0)
        loc = ctypes.create_string_buffer(32)
        if prop:
            _cf.CFStringGetCString(prop, loc, 32, 0x08000100)
            _cf.CFRelease(prop)
        if loc.value == b"External":
            found.append(svc)
        else:
            _io.IOObjectRelease(svc)
    _io.IOObjectRelease(it.value)
    _cf.CFRelease(key)
    return found


def _checksum(payload: bytes) -> int:
    c = 0x6E ^ 0x51
    for b in payload:
        c ^= b
    return c


def _ddc_get(av) -> Optional[tuple]:
    req = bytes([0x82, 0x01, 0x10])
    req += bytes([_checksum(req)])
    for _ in range(3):
        for _ in range(2):
            time.sleep(0.01)
            _io.IOAVServiceWriteI2C(av, 0x37, 0x51, req, len(req))
        time.sleep(0.04)
        buf = ctypes.create_string_buffer(12)
        if _io.IOAVServiceReadI2C(av, 0x37, 0x51, buf, 12) == 0:
            r = buf.raw
            if r[2] == 0x02 and r[3] == 0x00 and r[4] == 0x10:
                return (r[8] << 8 | r[9], max(1, r[6] << 8 | r[7]))
    return None


def _ddc_set(av, value: int):
    payload = bytes([0x84, 0x03, 0x10, (value >> 8) & 0xFF, value & 0xFF])
    msg = payload + bytes([_checksum(payload)])
    for _ in range(2):
        time.sleep(0.01)
        _io.IOAVServiceWriteI2C(av, 0x37, 0x51, msg, len(msg))


# ---------------------------------------------------------------- public

def change_brightness(value, display: str = "all") -> List[str]:
    """value: absolute percent (60) or relative ("+10" / "-10").
    display: "all", "builtin" or "external". Returns a log line per screen."""
    relative = isinstance(value, str) and value[:1] in "+-"
    amount = int(value)
    done = []
    with _lock:
        if display in ("all", "builtin"):
            for did in _builtin_ids():
                cur = _builtin_get(did)
                if cur is None:
                    continue
                new = cur + amount if relative else amount
                _builtin_set(did, new)
                done.append(f"built-in {cur:.0f}% -> {max(0, min(100, new)):.0f}%")
        if display in ("all", "external"):
            for idx, svc in enumerate(_external_services()):
                av = _io.IOAVServiceCreateWithService(None, svc)
                _io.IOObjectRelease(svc)
                if not av:
                    continue
                got = _ddc_get(av)
                if got:
                    cur, mx = got
                    _ddc_cache[idx] = cur
                else:
                    cur, mx = _ddc_cache.get(idx, 50), 100
                pct = cur * 100 / mx
                new_pct = max(0, min(100, pct + amount if relative else amount))
                _ddc_set(av, round(new_pct * mx / 100))
                _ddc_cache[idx] = round(new_pct * mx / 100)
                done.append(f"external {pct:.0f}% -> {new_pct:.0f}%" + ("" if got else " (DDC read failed)"))
                _cf.CFRelease(av)
    return done
