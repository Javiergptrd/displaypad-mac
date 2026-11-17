"""Key actions. Each key has "action": {...} or "actions": [{...}, ...].

  {"type": "open", "target": "Safari" | "https://..." | "~/Documents"}
  {"type": "shortcut", "keys": "cmd+shift+4"}
  {"type": "text", "text": "Hello\\n"}
  {"type": "media", "key": "play_pause|next|previous|volume_up|volume_down|mute"}
  {"type": "shell", "command": "say hello"}
  {"type": "applescript", "script": "display notification \\"hi\\""}
  {"type": "page", "page": "media" | "back"}
  {"type": "screen_brightness", "value": "+10", "display": "all|builtin|external"}
  {"type": "brightness", "value": 60 | "+10" | "-10"}      (DisplayPad backlight)
  {"type": "configurator"}                                 (open the settings page)
  {"type": "delay", "ms": 250}
"""

import logging
import os
import subprocess
import time

from . import macos, screens
from .i18n import t

log = logging.getLogger(__name__)

NEEDS_POST_EVENTS = {"shortcut", "text", "media"}


_REQUIRED = {"open": "target", "shortcut": "keys", "text": "text", "media": "key", "shell": "command",
             "applescript": "script", "page": "page", "brightness": "value",
             "screen_brightness": "value", "delay": "ms", "configurator": None}


def _is_level(value) -> bool:
    s = str(value).strip()
    return (s[1:] if s[:1] in "+-" else s).isdigit()


def validate_action(action: dict, pages: dict) -> str:
    """Error message for the configurator (in the user's language), or '' if the action is valid."""
    kind = (action or {}).get("type")
    if kind not in _REQUIRED:
        return t("unknown action type '{kind}'", kind=kind)
    field = _REQUIRED[kind]
    if field and (field not in action or str(action[field]).strip() == ""):
        return t("the '{kind}' action is missing '{field}'", kind=kind, field=field)
    if kind == "shortcut":
        try:
            macos.parse_shortcut(action["keys"])
        except ValueError:
            return t("invalid shortcut “{keys}”: use modifiers (cmd, shift, ctrl, alt) and one key, "
                     "e.g. cmd+shift+4, ctrl+up, f5, return", keys=action["keys"])
    if kind == "media" and action["key"] not in macos.MEDIA_KEYS:
        return t("unknown media key '{key}'", key=action["key"])
    if kind == "page" and action["page"] != "back" and action["page"] not in pages:
        return t("the page '{page}' does not exist", page=action["page"])
    if kind in ("brightness", "screen_brightness") and not _is_level(action["value"]):
        return t("brightness must be a number (50) or a change (+10 / -10)")
    if kind == "screen_brightness" and action.get("display", "all") not in ("all", "builtin", "external"):
        return t("display must be all, builtin or external")
    if kind == "delay" and not str(action["ms"]).isdigit():
        return t("the wait must be a number of milliseconds")
    return ""


def actions_of(spec: dict) -> list:
    if not spec:
        return []
    if "actions" in spec:
        return list(spec["actions"])
    if "action" in spec:
        return [spec["action"]]
    return []


def _open(target: str):
    expanded = os.path.expanduser(target)
    if "://" in target or ":" in target.split("/")[0] or os.path.exists(expanded):
        cmd = ["open", expanded]
    else:
        cmd = ["open", "-a", target]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        log.warning("open %s failed: %s", target, r.stderr.strip())


def _background(cmd: list):
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def run_action(action: dict, ctx):
    kind = action.get("type")
    if kind == "open":
        _open(action["target"])
    elif kind == "shortcut":
        macos.press_shortcut(action["keys"])
    elif kind == "text":
        macos.type_text(action["text"])
    elif kind == "media":
        macos.press_media_key(action["key"])
    elif kind == "shell":
        _background(["/bin/zsh", "-lc", action["command"]])
    elif kind == "applescript":
        _background(["/usr/bin/osascript", "-e", action["script"]])
    elif kind == "page":
        ctx.request_page(action["page"])
    elif kind == "brightness":
        ctx.change_brightness(action["value"])
    elif kind == "screen_brightness":
        for line in screens.change_brightness(str(action.get("value", "+10")), action.get("display", "all")):
            log.info("screen brightness: %s", line)
    elif kind == "configurator":
        ctx.open_configurator()
    elif kind == "delay":
        time.sleep(float(action.get("ms", 100)) / 1000)
    else:
        raise ValueError(f"unknown action type '{kind}'")


def run_actions(spec: dict, ctx):
    for action in actions_of(spec):
        try:
            run_action(action, ctx)
        except Exception as e:
            log.error("action %s failed: %s", action, e)
            break


def _captured(cmd: list) -> dict:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": t("stopped: it took more than 30 seconds")}
    out = (r.stdout + ("\n" if r.stdout and r.stderr else "") + r.stderr).strip()
    return {"ok": r.returncode == 0, "output": out[-4000:] or t("ended with code {code}", code=r.returncode)}


def test_actions(spec: dict, ctx, pages: dict) -> list:
    """Run actions for the configurator's "Test" button and report each result.
    Shell and AppleScript run in the foreground so their output and errors are shown."""
    results = []
    for action in actions_of(spec):
        err = validate_action(action, pages)
        if err:
            results.append({"type": action.get("type"), "ok": False, "output": err})
            break
        kind = action["type"]
        if kind in NEEDS_POST_EVENTS and not macos.post_events_allowed():
            results.append({"type": kind, "ok": False, "output": t("the Accessibility permission is needed to control the keyboard")})
            break
        try:
            if kind == "shell":
                res = _captured(["/bin/zsh", "-lc", action["command"]])
            elif kind == "applescript":
                res = _captured(["/usr/bin/osascript", "-e", action["script"]])
            elif kind == "screen_brightness":
                lines = screens.change_brightness(str(action["value"]), action.get("display", "all"))
                res = {"ok": bool(lines), "output": "\n".join(lines) or t("no compatible display was found")}
            else:
                run_action(action, ctx)
                res = {"ok": True, "output": ""}
        except Exception as e:
            res = {"ok": False, "output": str(e)}
        results.append({"type": kind, **res})
        if not res["ok"]:
            break
    return results
