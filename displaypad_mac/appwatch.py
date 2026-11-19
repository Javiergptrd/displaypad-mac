"""Pages that come up by themselves, like Base Camp's profiles: a page linked to apps is shown
when one of them opens (and goes away when the last one quits), or while one of them is in
front (and goes away when you switch to another app). When it goes away the pad returns to
what it showed before, if you have not moved it meanwhile.

Apps are known by the name of their .app bundle, the same names as the "open" action and
the configurator's list ("Safari", "Adobe Photoshop 2025"), compared without case and
without a year at the end, so a page linked to "Adobe Photoshop" (or to last year's
version) keeps working when Adobe installs "Adobe Photoshop 2026".
- Open apps: the process table (libproc), only each app's main program, so helpers that
  stay behind don't count.
- The app in front: the owner of the front-most normal window (CGWindowList). Neither needs
  a permission, and both stay current without an AppKit run loop.
"""

import ctypes
import ctypes.util
import logging
import re
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

WHEN = ("open", "front")
FRONT_SECONDS = 0.4    # how often the app in front is checked
RUNNING_SECONDS = 1.0  # how often the open apps are listed

vp = ctypes.c_void_p
_proc = ctypes.CDLL("/usr/lib/libproc.dylib")
_proc.proc_listallpids.argtypes = [vp, ctypes.c_int]
_proc.proc_listallpids.restype = ctypes.c_int
_proc.proc_pidpath.argtypes = [ctypes.c_int, vp, ctypes.c_uint32]
_proc.proc_pidpath.restype = ctypes.c_int

_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
_cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
_cf.CFStringCreateWithCString.restype = vp
_cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
_cf.CFArrayGetCount.restype = ctypes.c_long
_cf.CFArrayGetCount.argtypes = [vp]
_cf.CFArrayGetValueAtIndex.restype = vp
_cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
_cf.CFDictionaryGetValue.restype = vp
_cf.CFDictionaryGetValue.argtypes = [vp, vp]
_cf.CFNumberGetValue.restype = ctypes.c_bool
_cf.CFNumberGetValue.argtypes = [vp, ctypes.c_int, vp]
_cf.CFRelease.argtypes = [vp]
_cg.CGWindowListCopyWindowInfo.restype = vp
_cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]

_UTF8 = 0x08000100
_SINT64 = 4
_ON_SCREEN, _NO_DESKTOP = 1, 16
_LAYER = _cf.CFStringCreateWithCString(None, b"kCGWindowLayer", _UTF8)
_OWNER = _cf.CFStringCreateWithCString(None, b"kCGWindowOwnerPID", _UTF8)


_YEAR = re.compile(r"\s+(19|20)\d\d$")


def norm(name: str) -> str:
    """How app names are compared: 'Adobe Photoshop 2026.app' -> 'adobe photoshop'."""
    n = str(name).strip()
    if n.lower().endswith(".app"):
        n = n[:-4].rstrip()
    return _YEAR.sub("", n).lower()


def app_of(path: str) -> str:
    """'Safari' for /Applications/Safari.app/Contents/MacOS/Safari; '' for anything else
    (helpers inside an app, command-line programs)."""
    parts = path.split("/")
    for i, part in enumerate(parts):
        if part.endswith(".app"):
            return part[:-4] if len(parts) == i + 4 and parts[i + 1:i + 3] == ["Contents", "MacOS"] else ""
    return ""


def _path(pid: int) -> str:
    buf = ctypes.create_string_buffer(4096)
    n = _proc.proc_pidpath(pid, buf, 4096)
    return buf.value.decode("utf-8", "replace") if n > 0 else ""


def _number(d, key) -> Optional[int]:
    v = _cf.CFDictionaryGetValue(d, key)
    out = ctypes.c_int64(0)
    return out.value if v and _cf.CFNumberGetValue(v, _SINT64, ctypes.byref(out)) else None


class Apps:
    """What the Mac is running. Names are as norm() makes them."""

    def __init__(self):
        self._names: Dict[int, str] = {}  # pid -> app name ("" when not an app)

    def _name(self, pid: int) -> str:
        if pid not in self._names:
            self._names[pid] = norm(app_of(_path(pid)))
        return self._names[pid]

    def running(self) -> Set[str]:
        n = _proc.proc_listallpids(None, 0)
        buf = (ctypes.c_int * (max(n, 0) + 128))()
        n = _proc.proc_listallpids(buf, ctypes.sizeof(buf))
        pids = set(buf[:max(n, 0)])
        for gone in set(self._names) - pids:  # a pid can come back as another program
            del self._names[gone]
        return {name for name in map(self._name, pids) if name}

    def front(self) -> str:
        windows = _cg.CGWindowListCopyWindowInfo(_ON_SCREEN | _NO_DESKTOP, 0)
        if not windows:
            return ""
        try:
            for i in range(_cf.CFArrayGetCount(windows)):
                w = _cf.CFArrayGetValueAtIndex(windows, i)
                if _number(w, _LAYER) == 0:  # normal windows; menus, the Dock, panels are above
                    pid = _number(w, _OWNER)
                    return self._name(pid) if pid else ""
            return ""
        finally:
            _cf.CFRelease(windows)


Link = Tuple[str, frozenset, str]  # (page, app names as norm() makes them, "open" | "front")


class AutoPages:
    """Decides which page the pad shows for pages linked to apps. It only gets told what the
    Mac is doing, so it can be tested without one.

    `stack` holds the pages shown automatically, oldest first, each with where to return
    when it goes away (None: nowhere known, stay)."""

    def __init__(self):
        self.links: List[Link] = []
        self.stack: List[list] = []        # [page, return to]
        self._last: Dict[str, str] = {}    # page -> the folder inside it the pad was in when it went away
        self._running: Optional[Set[str]] = None
        self._front: Optional[str] = None
        self._fresh: Set[Link] = set()     # links just added: checked against what is open now
        self.reason = ""

    def configure(self, links: List[Link]):
        old = set(self.links)
        self.links = list(links)
        self._fresh = {link for link in self.links if link not in old}
        kept = {link[0] for link in self.links}
        for entry in [e for e in self.stack if e[0] not in kept]:
            self._drop(entry)

    def _drop(self, entry):
        i = self.stack.index(entry)
        if i + 1 < len(self.stack):  # the one above now returns where this one would have
            self.stack[i + 1][1] = entry[1]
        del self.stack[i]

    def update(self, running: Set[str], front: str, shown: str) -> Optional[str]:
        """running: the open apps; front: the app in front; shown: the page or folder on the
        pad. Returns the page or folder to show, or None to leave the pad alone."""
        first = self._running is None
        opened = running if first else running - self._running
        came_front = front != self._front
        self._running, self._front = set(running), front
        fresh, self._fresh = self._fresh, set()
        now = shown
        reasons = []

        def top(where):
            return (where or "").split("/")[0]

        # pages whose app is gone
        for entry in list(self.stack):
            link = next((l for l in self.links if l[0] == entry[0]), None)
            if link and (link[1] & running if link[2] == "open" else front in link[1]):
                continue
            self._drop(entry)
            if top(now) == entry[0] and entry[1]:
                self._last[entry[0]] = now
                now = entry[1]
                reasons.append(f"left '{entry[0]}'")

        # pages whose app has just opened, then the one whose app has come to the front
        for when in WHEN:
            for link in self.links:
                page, apps, how = link
                if how != when:
                    continue
                if how == "open":
                    hit = sorted(apps & (running if first or link in fresh else opened))
                else:
                    hit = [front] if front in apps and (came_front or link in fresh) else []
                if not hit:
                    continue
                entry = next((e for e in self.stack if e[0] == page), None)
                if entry and self.stack[-1] is entry and top(now) == page:
                    continue
                ret = now if top(now) != page else (entry[1] if entry else None)
                if entry:
                    self._drop(entry)
                self.stack.append([page, ret])
                if top(now) != page:
                    now = self._last.pop(page, page)
                reasons.append(f"{hit[0]} {'opened' if how == 'open' else 'in front'}: '{page}'")

        self.reason = "; ".join(reasons)
        return now if now != shown else None
