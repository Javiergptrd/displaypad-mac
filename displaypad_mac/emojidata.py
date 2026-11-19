"""The Mac's own emoji, for the configurator's emoji picker: every emoji macOS draws, with
the names its Character Viewer shows, in the configurator's language and in English (the
search looks at both). They are read from the system when asked for; nothing is copied
into this project. The browser puts them in order, in groups and in skin tones.

The list covers the emoji as macOS knows them, so whatever the picker offers, the pad can
draw: keys are drawn with the same Apple Color Emoji font.
"""

import functools
import logging
import os
import plistlib
from typing import Dict, List

log = logging.getLogger(__name__)

CORE_EMOJI = ("/System/Library/PrivateFrameworks/CoreEmoji.framework/Resources",
              "/System/Library/PrivateFrameworks/CoreEmoji.framework/Versions/A/Resources")
# skin tones and hair styles: parts that go inside other emoji, not emoji to pick
_PARTS = {chr(c) for c in (*range(0x1F3FB, 0x1F400), *range(0x1F9B0, 0x1F9B4))}


def _region() -> str:
    """The Mac's region ("DO" for es_DO or en_DO), to pick Latin American Spanish names."""
    try:
        with open(os.path.expanduser("~/Library/Preferences/.GlobalPreferences.plist"), "rb") as f:
            loc = str(plistlib.load(f).get("AppleLocale", ""))
    except (OSError, plistlib.InvalidFileException, ValueError):
        return ""
    parts = loc.split("@")[0].replace("-", "_").split("_")
    return parts[1].upper() if len(parts) > 1 else ""


def _folders(lang: str, region: str) -> List[str]:
    out = [f"{lang}_{region}"] if region else []
    if lang == "es" and region and region != "ES":
        out.append("es_419")  # Latin America and the US
    return out + [lang]


@functools.lru_cache(maxsize=8)
def _read(folder: str) -> Dict[str, str]:
    for base in CORE_EMOJI:
        path = os.path.join(base, folder + ".lproj", "AppleName.strings")
        try:
            with open(path, "rb") as f:
                names = plistlib.load(f)
        except FileNotFoundError:
            continue
        except (OSError, plistlib.InvalidFileException, ValueError) as e:
            log.warning("emoji names in %s: %s", path, e)
            return {}
        return {e: str(n) for e, n in names.items() if isinstance(e, str) and e and e not in _PARTS}
    return {}


def names(lang: str) -> Dict[str, str]:
    """emoji -> name in lang ("en" or "es"); empty if this Mac has no list."""
    for folder in _folders(lang, _region()):
        found = _read(folder)
        if found:
            return found
    return {}


def catalog(lang: str) -> List[list]:
    """[emoji, name, English name or "" when the same] for every emoji, in no order."""
    en = names("en")
    local = en if lang == "en" else names(lang) or en
    out = []
    for e in {**en, **local}:
        name, english = local.get(e) or en.get(e, ""), en.get(e, "")
        out.append([e, name, "" if english == name else english])
    return out
