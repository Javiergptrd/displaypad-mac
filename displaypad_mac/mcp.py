"""MCP server: lets AI assistants (Claude, Cursor, VS Code, ...) read and change the
DisplayPad setup through the Model Context Protocol, so they can help the user
build pages, keys and automations.

Transport: "Streamable HTTP" at http://127.0.0.1:8765/mcp (web.py checks the
access key and routes each JSON-RPC message here). Answers are plain JSON; the
server never starts messages of its own. Apps that can only launch a local
command use `python -m displaypad_mac mcp` (stdio_bridge below), which forwards
every message to that address with the key from .mcp-token.

Every change goes through Service.update_config: it is validated like a save in
the configurator, written atomically (the previous file stays as
config.json.previous) and shows on the pad within a second. An open configurator
window notices and reloads.
"""

import base64
import io
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

from . import __version__, actions, daemon, render, web  # daemon/web: circular, used at call time only

log = logging.getLogger(__name__)

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # newest first
MAX_UPLOAD = 12 * 1024 * 1024

INSTRUCTIONS = """\
These tools set up the user's Mountain DisplayPad on this Mac: 12 keys with a small screen each \
(2 rows of 6; keys 1-6 top row, 7-12 bottom row), grouped in pages. Each key has a look (emoji, \
picture or animation, label, colors) and a list of actions run when it is pressed (open apps or \
websites, keyboard shortcuts, typed text, media keys, shell commands, AppleScript, page switches, \
brightness).

Call get_reference once for every key field and action type. Look before you change anything \
(list_pages, get_config) and ask before replacing keys the user already uses. Changes are saved at \
once and appear on the pad within a second; check the look with preview_page and switch the pad \
with show_page. test_actions runs actions right away and only works if the user allowed it in \
DisplayPad Settings > AI assistants."""

REFERENCE = """\
DisplayPad for Mac: configuration reference

THE DEVICE
- 12 keys, each a 102x102 pixel screen, in 2 rows of 6: keys 1-6 are the top row (left to right), 7-12 the bottom row.
- The configuration has one or more pages; the pad shows one at a time. start_page is shown when the service starts. The "page" action switches pages; page "back" returns to the previous one.

KEY FIELDS (all optional; a key with nothing is simply left out)
- emoji: drawn as the icon, e.g. "📧".
- image: a picture in the configuration folder, e.g. "icons/icono-1a2b3c4d.png". Add PNG/JPG pictures, animated GIF/WebP or Lottie animations with upload_image and use the path it returns. Animations play on the pad (about 50 frames per second in total for all animated keys of a page).
- label: text at the bottom of the key, under the icon (about 8-10 characters fit per line; longer text shrinks and wraps). A key with only a label shows it large in the middle.
- hide_label: true keeps the label in the configuration without drawing it.
- color: background "#RRGGBB" (default "#1C1C24"). text_color: label color (default "#FFFFFF").
- fill: images only. true (default) makes a picture cover the whole key when no label is drawn; false keeps a margin.
- icon_scale: 30-200, size of the emoji or picture in percent (default 100; over 100 zooms in).
- crop: [left, top, right, bottom] for pictures, as fractions of the picture (0-1), e.g. [0.25, 0, 0.75, 1] for the middle square of a 2:1 picture. Keys are square, so make the cropped area square in pixels or the picture is centred inside it (never stretched). Without crop the middle of the picture covers the key, or, on a key with visible text or fill false, the whole picture is shown smaller. Values past 0-1 zoom out: that part shows the key color (for a 2:1 picture, [0, -0.5, 1, 1.5] shows it whole). upload_image tells the picture's size. Setting a new image without crop drops the old crop.
- actions: list of actions run in order when the key is pressed. ("action": {...} with a single action is also accepted.)

ACTIONS
- {"type": "open", "target": "Safari"}: an app name (see list_apps), a URL ("https://...", "mailto:...", "zoommtg://...") or a file/folder path ("~/Documents").
- {"type": "shortcut", "keys": "cmd+shift+4"}: modifiers cmd, shift, ctrl, alt (or option), fn, then one key: a-z, 0-9, f1-f20, return, tab, space, delete, forwarddelete, esc, up, down, left, right, home, end, pageup, pagedown, - = [ ] \\ ; ' , . / `. Goes to the app in front.
- {"type": "text", "text": "Best regards,\\n"}: typed where the cursor is (any Unicode; \\n presses Return).
- {"type": "media", "key": "play_pause"}: play_pause, next, previous, volume_up, volume_down, mute, brightness_up, brightness_down.
- {"type": "shell", "command": "open -a Safari https://apple.com"}: runs with zsh -lc in the background; nobody sees its output.
- {"type": "applescript", "script": "tell application \\"Music\\" to playpause"}: runs with osascript in the background.
- {"type": "page", "page": "media"}: show another page; "back" returns to the previous one.
- {"type": "screen_brightness", "value": "+10", "display": "all"}: the Mac's displays. value "50" sets, "+10" / "-10" changes; display all, builtin or external (external monitors need DDC/CI on).
- {"type": "brightness", "value": "+10"}: the DisplayPad's own backlight, same values.
- {"type": "delay", "ms": 300}: wait between two actions (e.g. open an app, then send it a shortcut).
- {"type": "configurator"}: open the DisplayPad configurator in the browser.

PERMISSIONS
- shortcut, text and media need the macOS Accessibility permission for Python. If get_status shows "accessibility": false, tell the user to allow it in System Settings > Privacy & Security > Accessibility.

GOOD PRACTICE
- Short labels, one idea per key, related keys on the same page. Every extra page needs a way back (create_page puts a "back" key on key 1) and a key on another page that leads to it.
- Dark, saturated backgrounds keep the white label readable.
- For web apps, "open" with the URL is simpler than a chain of shortcuts.
- For multi-step automations prefer one AppleScript or shell action over many shortcuts and delays.
- After changing keys, check them with preview_page."""

PAGE = {"type": "string", "description": "Page name, as listed by list_pages."}
KEY = {"type": "integer", "minimum": 1, "maximum": 12,
       "description": "Key number: 1-6 top row, 7-12 bottom row, left to right."}
ACTION = {"type": "object", "required": ["type"], "description": "One action; see get_reference.",
          "properties": {"type": {"type": "string", "enum": ["open", "shortcut", "text", "media", "shell", "applescript",
                                                             "page", "screen_brightness", "brightness", "delay",
                                                             "configurator"]}}}
KEY_SPEC = {
    "type": "object", "description": "How the key looks and what it does; see get_reference.",
    "properties": {
        "emoji": {"type": "string"},
        "image": {"type": "string", "description": "icons/... path returned by upload_image."},
        "label": {"type": "string"},
        "hide_label": {"type": "boolean"},
        "color": {"type": "string", "description": "Background, #RRGGBB."},
        "text_color": {"type": "string", "description": "Label color, #RRGGBB."},
        "fill": {"type": "boolean"},
        "icon_scale": {"type": "number", "minimum": 30, "maximum": 200},
        "crop": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                 "description": "[left, top, right, bottom], fractions of the picture; see get_reference."},
        "actions": {"type": "array", "items": ACTION},
    },
}


class ToolError(Exception):
    """Shown to the assistant as a failed tool call (isError), not a protocol error."""


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- helpers
def _key(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise ToolError("key must be a number from 1 to 12")
    if not 1 <= n <= 12:
        raise ToolError("key must be a number from 1 to 12")
    return str(n)


def _page(raw: dict, name) -> dict:
    pages = raw.get("pages") or {}
    if name not in pages:
        raise ToolError(f"there is no page '{name}'. Pages: {', '.join(pages) or 'none'}")
    if not isinstance(pages[name], dict):
        pages[name] = {}
    return pages[name]


def _save(svc, change) -> str:
    try:
        return svc.update_config(change)
    except daemon.ConfigError as e:
        raise ToolError(f"not saved: {e}")


def _actions(spec: dict) -> list:
    return actions.actions_of(spec or {})


def _summary(spec: dict) -> str:
    look = " ".join(x for x in (spec.get("emoji"), spec.get("image") and f"[{os.path.basename(spec['image'])}]",
                                spec.get("label") and json.dumps(spec["label"], ensure_ascii=False)) if x)
    acts = "; ".join(f"{a.get('type')}: {next((str(v) for k, v in a.items() if k != 'type'), '')}".rstrip(": ")
                     for a in _actions(spec) if isinstance(a, dict))
    return (look or "(no icon)") + (f" -> {acts}" if acts else " -> does nothing")


def _check_image(svc, spec: dict):
    img = spec.get("image")
    if img and not os.path.isfile(os.path.join(svc.cfg["base_dir"], os.path.expanduser(img))):
        raise ToolError(f"image '{img}' not found: add it with upload_image and use the path it returns")


def _without_page_refs(raw: dict, name: str) -> list:
    """Drop "go to page <name>" actions from every key; returns the keys changed."""
    changed = []
    for pn, p in (raw.get("pages") or {}).items():
        for kn, spec in ((p or {}).get("keys") or {}).items():
            if not isinstance(spec, dict):
                continue
            acts = _actions(spec)
            keep = [a for a in acts if not (isinstance(a, dict) and a.get("type") == "page" and a.get("page") == name)]
            if len(keep) != len(acts):
                spec.pop("action", None)
                spec["actions"] = keep
                changed.append(f"{pn} key {kn}")
    return changed


# ---------------------------------------------------------------- tools
def get_status(svc, a):
    st = svc.status()
    st.pop("python_app", None)
    st["pages"] = list(svc.cfg["pages"])
    st["start_page"] = svc.cfg["start_page"]
    return st


def get_reference(svc, a):
    return REFERENCE


def get_config(svc, a):
    return {"rev": svc.config_rev(), "config": svc.read_config()}


def list_pages(svc, a):
    raw = svc.read_config()
    pages = raw.get("pages") or {}
    start = raw.get("start_page") or next(iter(pages), None)
    out = []
    for name, p in pages.items():
        keys = {k: _summary(spec) for k, spec in sorted(((p or {}).get("keys") or {}).items(),
                                                          key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 99)
                if isinstance(spec, dict) and spec}
        out.append({"name": name, "start_page": name == start, "on_the_pad_now": name == svc.page,
                    "empty_keys": [n for n in range(1, 13) if str(n) not in keys], "keys": keys})
    return out


def get_key(svc, a):
    k = _key(a["key"])
    spec = (_page(svc.read_config(), a["page"]).get("keys") or {}).get(k)
    return {"page": a["page"], "key": int(k), "spec": spec or None}


def set_key(svc, a):
    k, spec, merge = _key(a["key"]), a["spec"], a.get("merge", False)
    if not isinstance(spec, dict):
        raise ToolError("spec must be an object")
    result = {}

    def change(raw):
        keys = _page(raw, a["page"]).setdefault("keys", {})
        new = dict(keys.get(k) or {}) if merge else {}
        if merge and ("actions" in spec or "action" in spec):
            new.pop("action", None)
            new.pop("actions", None)
        if merge and spec.get("image") and spec["image"] != new.get("image") and "crop" not in spec:
            new.pop("crop", None)  # a crop belongs to the picture it was made for
        for f, v in spec.items():
            if v is None:
                new.pop(f, None)
            else:
                new[f] = v
        _check_image(svc, new)
        if new:
            keys[k] = new
        else:
            keys.pop(k, None)
        result["spec"] = new or None

    _save(svc, change)
    return {"saved": True, "page": a["page"], "key": int(k), "spec": result["spec"],
            "note": "On the pad within a second if this page is showing (show_page switches it)."}


def clear_key(svc, a):
    k = _key(a["key"])

    def change(raw):
        (_page(raw, a["page"]).get("keys") or {}).pop(k, None)

    _save(svc, change)
    return {"cleared": True, "page": a["page"], "key": int(k)}


def create_page(svc, a):
    name = str(a["name"]).strip()
    if not name or name == "back":
        raise ToolError("the page needs a name other than 'back'")
    keys = a.get("keys") or {}
    if not isinstance(keys, dict):
        raise ToolError("keys must be an object like {\"2\": {...}, \"3\": {...}}")
    back = a.get("back_key", True)

    def change(raw):
        pages = raw.setdefault("pages", {})
        if name in pages:
            raise ToolError(f"there is already a page '{name}'")
        new = {}
        if back:
            label = "Volver" if raw.get("language") == "es" else "Back"
            new["1"] = {"emoji": "⬅️", "label": label, "color": "#2B2B33",
                        "actions": [{"type": "page", "page": "back"}]}
        for kn, spec in keys.items():
            if isinstance(spec, dict):
                _check_image(svc, spec)
            new[_key(kn)] = spec
        pages[name] = {"keys": new}

    _save(svc, change)
    return {"created": name, "keys": sorted(int(k) for k in ([1] if back else []) + list(keys)),
            "next": f"add a key on another page with the action {{\"type\": \"page\", \"page\": \"{name}\"}} "
                    "so the user can reach it"}


def rename_page(svc, a):
    old, new = a["name"], str(a["new_name"]).strip()
    if not new or new == "back":
        raise ToolError("the page needs a name other than 'back'")
    result = {}

    def change(raw):
        _page(raw, old)
        if new in raw["pages"] and new != old:
            raise ToolError(f"there is already a page '{new}'")
        raw["pages"] = {(new if n == old else n): p for n, p in raw["pages"].items()}
        refs = []
        for pn, p in raw["pages"].items():
            for kn, spec in ((p or {}).get("keys") or {}).items():
                for act in _actions(spec) if isinstance(spec, dict) else []:
                    if isinstance(act, dict) and act.get("type") == "page" and act.get("page") == old:
                        act["page"] = new
                        refs.append(f"{pn} key {kn}")
        if raw.get("start_page") == old:
            raw["start_page"] = new
        result["links_updated"] = refs

    _save(svc, change)
    return {"renamed": old, "to": new, **result}


def delete_page(svc, a):
    name = a["name"]
    result = {}

    def change(raw):
        _page(raw, name)
        if len(raw["pages"]) == 1:
            raise ToolError("this is the only page; it cannot be deleted")
        del raw["pages"][name]
        result["links_removed"] = _without_page_refs(raw, name)
        if raw.get("start_page", name) == name:
            raw["start_page"] = next(iter(raw["pages"]))

    _save(svc, change)
    return {"deleted": name, **result}


def show_page(svc, a):
    name = a["page"]
    _page(svc.read_config(), name)
    end = time.monotonic() + 3
    while name not in svc.cfg["pages"] and time.monotonic() < end:  # just created: wait for the reload
        time.sleep(0.1)
    svc.request_page(name)
    return {"showing": name, "pad_connected": svc.pad.connected}


def update_settings(svc, a):
    patch = {f: a[f] for f in ("language", "theme", "brightness", "press_feedback") if a.get(f) is not None}
    if a.get("start_page") is not None:
        def change(raw):
            _page(raw, a["start_page"])
            raw["start_page"] = a["start_page"]
        _save(svc, change)
    if patch:
        try:
            svc.update_settings(patch)
        except daemon.ConfigError as e:
            raise ToolError(f"not saved: {e}")
    if not patch and a.get("start_page") is None:
        raise ToolError("give at least one setting to change")
    return {"saved": True, **patch, **({"start_page": a["start_page"]} if a.get("start_page") else {})}


def test_actions(svc, a):
    if not svc.cfg["mcp"]["allow_run"]:
        raise ToolError("Running actions from an assistant is turned off. Save the actions on a key and let the user "
                        "press it, or ask the user to turn on \"Let assistants run actions\" in the DisplayPad app, "
                        "Settings > AI assistants.")
    acts = a["actions"]
    if not isinstance(acts, list) or not acts:
        raise ToolError("actions must be a non-empty list")
    log.info("MCP: running %d action(s) on request of an assistant", len(acts))
    results = actions.test_actions({"actions": acts}, svc, svc.read_config().get("pages") or {})
    if len(results) < len(acts):
        results.append({"note": "the remaining actions were not run because one failed"})
    return results


def list_apps(svc, a):
    return web.list_apps()


def upload_image(svc, a):
    if a.get("file_path"):
        path = os.path.expanduser(a["file_path"])
        if not os.path.isfile(path):
            raise ToolError(f"file not found: {path}")
        if os.path.getsize(path) > MAX_UPLOAD:
            raise ToolError("file too large (12 MB max)")
        with open(path, "rb") as f:
            data = f.read()
    elif a.get("data_base64"):
        b64 = a["data_base64"]
        b64 = b64.split(",", 1)[1] if b64.startswith("data:") else b64
        try:
            data = base64.b64decode(b64, validate=False)
        except ValueError as e:
            raise ToolError(f"data_base64 is not valid base64: {e}")
        if len(data) > MAX_UPLOAD:
            raise ToolError("file too large (12 MB max)")
    else:
        raise ToolError("give file_path (a file on this Mac) or data_base64")
    try:
        rel = web.save_upload(data, svc.cfg["base_dir"])
    except ValueError as e:
        raise ToolError(str(e))
    from PIL import Image
    with Image.open(os.path.join(svc.cfg["base_dir"], rel)) as img:
        w, h = img.size
    out = {"image": rel, "width": w, "height": h, "next": "use it as the \"image\" field of a key with set_key"}
    if w != h:
        out["note"] = ("Not square: without \"crop\" the middle of the picture covers the key (on a key with visible text it is shown whole). "
                       "Set crop to choose the part to show, or to show it whole (see get_reference).")
    return out


def preview_page(svc, a):
    try:
        cfg = daemon.parse_config(svc.read_config(), svc.cfg["base_dir"])
    except daemon.ConfigError as e:
        raise ToolError(str(e))
    name = a.get("page") or svc.page
    if name not in cfg["pages"]:
        raise ToolError(f"there is no page '{name}'. Pages: {', '.join(cfg['pages'])}")
    buf = io.BytesIO()
    render.preview_sheet(cfg["pages"][name], cfg["base_dir"]).save(buf, "PNG")
    return [{"type": "text", "text": f"Page '{name}' as the DisplayPad draws it: keys 1-6 top row, 7-12 bottom row "
                                     "(animated keys show their first frame)."},
            {"type": "image", "data": base64.b64encode(buf.getvalue()).decode(), "mimeType": "image/png"}]


def _tool(fn, title, description, props=None, required=(), read_only=False, destructive=False, open_world=False):
    return {"fn": fn, "name": fn.__name__, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required),
                            "additionalProperties": False},
            "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": destructive,
                            "idempotentHint": read_only, "openWorldHint": open_world}}


TOOLS = [
    _tool(get_status, "DisplayPad status",
          "Whether the DisplayPad is connected, the page it shows, brightness, macOS permissions "
          "(input_monitoring, accessibility), the pages and the start page.", read_only=True),
    _tool(get_reference, "Configuration reference",
          "Every key field and action type with examples, plus layout tips. Read it before creating keys.",
          read_only=True),
    _tool(get_config, "Read the whole configuration", "The full config.json (all pages and keys) and its revision.",
          read_only=True),
    _tool(list_pages, "List pages and keys",
          "Each page with a one-line summary of every key (icon, label, actions), its empty keys and which "
          "page is the start page or on the pad now.", read_only=True),
    _tool(get_key, "Read one key", "The full settings of one key.", {"page": PAGE, "key": KEY}, ("page", "key"),
          read_only=True),
    _tool(set_key, "Set up a key",
          "Save how a key looks and what it does. Replaces the key unless merge is true (then only the given "
          "fields change; a field set to null is removed). The page must exist (create_page).",
          {"page": PAGE, "key": KEY, "spec": KEY_SPEC,
           "merge": {"type": "boolean", "description": "Change only the given fields. Default false."}},
          ("page", "key", "spec"), destructive=True),
    _tool(clear_key, "Clear a key", "Make a key empty (no icon, no actions).", {"page": PAGE, "key": KEY},
          ("page", "key"), destructive=True),
    _tool(create_page, "Create a page",
          "Add a page. By default key 1 becomes a \"back\" key. Optionally fill keys at once.",
          {"name": {"type": "string"},
           "keys": {"type": "object", "description": "Optional {\"2\": spec, \"3\": spec, ...}.",
                    "additionalProperties": KEY_SPEC},
           "back_key": {"type": "boolean", "description": "Put a back key on key 1. Default true."}},
          ("name",)),
    _tool(rename_page, "Rename a page", "Rename a page; keys that lead to it are updated.",
          {"name": PAGE, "new_name": {"type": "string"}}, ("name", "new_name")),
    _tool(delete_page, "Delete a page", "Delete a page and the \"go to this page\" actions that lead to it.",
          {"name": PAGE}, ("name",), destructive=True),
    _tool(show_page, "Show a page on the pad", "Switch the DisplayPad to a page now.", {"page": PAGE}, ("page",)),
    _tool(update_settings, "Change general settings",
          "Pad backlight brightness (0-100), darken-on-press, start page, and the configurator's language "
          "(en, es) and theme (system, light, dark).",
          {"brightness": {"type": "integer", "minimum": 0, "maximum": 100},
           "press_feedback": {"type": "boolean"},
           "start_page": PAGE,
           "language": {"type": "string", "enum": ["en", "es"]},
           "theme": {"type": "string", "enum": ["system", "light", "dark"]}}),
    _tool(test_actions, "Run actions now",
          "Run a list of actions immediately on this Mac and report each result (shell and AppleScript output "
          "included), to test an automation before saving it on a key. Only works if the user allowed it.",
          {"actions": {"type": "array", "items": ACTION, "minItems": 1}}, ("actions",),
          destructive=True, open_world=True),
    _tool(list_apps, "List installed apps", "Names of the apps installed on this Mac, for \"open\" actions.",
          read_only=True),
    _tool(upload_image, "Add a picture or animation",
          "Store a picture (PNG, JPG, WebP), an animated GIF/WebP or a Lottie animation (.json, .lottie, .tgs) "
          "for keys. Returns the path to use as a key's \"image\".",
          {"file_path": {"type": "string", "description": "A file on this Mac."},
           "data_base64": {"type": "string", "description": "The file's bytes in base64 (a data: URL works too)."}}),
    _tool(preview_page, "Preview a page",
          "A picture of a page exactly as the DisplayPad draws it. Without page, the page on the pad now.",
          {"page": PAGE}, read_only=True),
]
TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}
TOOL_LIST = [{k: v for k, v in tool.items() if k != "fn"} for tool in TOOLS]


# ---------------------------------------------------------------- JSON-RPC
def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _call(svc, params: dict) -> dict:
    name, args = params.get("name"), params.get("arguments") or {}
    tool = TOOLS_BY_NAME.get(name)
    if not tool:
        raise RpcError(-32602, f"Unknown tool: {name}")
    if not isinstance(args, dict):
        raise RpcError(-32602, "arguments must be an object")
    schema = tool["inputSchema"]
    unknown = [k for k in args if k not in schema["properties"]]
    missing = [k for k in schema["required"] if k not in args]
    if unknown or missing:
        msg = "; ".join(filter(None, [unknown and f"unknown argument(s): {', '.join(unknown)}",
                                      missing and f"missing argument(s): {', '.join(missing)}"]))
        return {"content": [{"type": "text", "text": msg}], "isError": True}
    log.info("MCP tool %s", name)
    try:
        out = tool["fn"](svc, args)
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:
        log.exception("MCP tool %s failed", name)
        return {"content": [{"type": "text", "text": f"{name} failed: {e}"}], "isError": True}
    if isinstance(out, list) and out and isinstance(out[0], dict) and "type" in out[0] and name == "preview_page":
        return {"content": out, "isError": False}
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(msg, svc):
    """Answer one JSON-RPC message; None for notifications and responses."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request")
    method = msg.get("method")
    if not isinstance(method, str) or "id" not in msg:
        return None  # notifications/initialized, notifications/cancelled, or a reply: nothing to answer
    mid, params = msg["id"], msg.get("params") or {}
    try:
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "displaypad-mac", "title": "DisplayPad for Mac", "version": __version__},
                      "instructions": INSTRUCTIONS}
            log.info("MCP: %s connected (protocol %s)",
                     (params.get("clientInfo") or {}).get("name", "an assistant"), result["protocolVersion"])
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOL_LIST}
        elif method == "tools/call":
            result = _call(svc, params)
        else:
            return _error(mid, -32601, f"Method not found: {method}")
    except RpcError as e:
        return _error(mid, e.code, str(e))
    return {"jsonrpc": "2.0", "id": mid, "result": result}


# ---------------------------------------------------------------- stdio bridge
def _out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def stdio_bridge(base_dir: str) -> int:
    """`python -m displaypad_mac mcp`: newline-delimited JSON-RPC on stdin/stdout, forwarded
    to the service's /mcp with the access key, so the key never goes in the app's settings."""
    token_path = os.path.join(base_dir, ".mcp-token")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as e:
            _out(_error(None, -32700, f"Parse error: {e}"))
            continue
        ids = [m["id"] for m in (msg if isinstance(msg, list) else [msg])
               if isinstance(m, dict) and "id" in m and "method" in m]
        try:
            with open(token_path) as f:
                token = f.read().strip()
        except OSError:
            token = ""
        req = urllib.request.Request(web.mcp_url(), data=line.encode(), method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            reply = json.loads(data) if data.strip() else None
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())["error"]
            except (ValueError, KeyError, TypeError):
                err = {"code": -32603, "message": f"DisplayPad service answered HTTP {e.code}"}
            reply = [{"jsonrpc": "2.0", "id": i, "error": err} for i in ids]
        except (urllib.error.URLError, OSError, ValueError) as e:
            err = {"code": -32603, "message": "the DisplayPad service is not running (open the DisplayPad app, "
                                              f"or run install.sh again): {e}"}
            reply = [{"jsonrpc": "2.0", "id": i, "error": err} for i in ids]
        if isinstance(reply, list) and not isinstance(msg, list):
            reply = reply[0] if reply else None
        if reply:
            _out(reply)
    return 0
