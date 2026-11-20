"""Draw 102x102 key faces from a key's config: image, emoji, label, colours.
Animated images (GIF, WebP, APNG, converted Lottie) give one face per frame; live keys
(live.py) get their text, or are drawn as a meter. A key's background can be a gradient,
a moving one or waves, and a press effect (ripple, flash...) can play over it. While a
sound key's sound plays (sound.py), a ring around its edge shows how far it has got."""

import bisect
import functools
import itertools
import logging
import math
import os
import re
from typing import Optional

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

from . import anim
from .driver import ICON_SIZE
from .i18n import t

log = logging.getLogger(__name__)

S = ICON_SIZE
DEFAULT_BG = "#1C1C24"
DEFAULT_FG = "#FFFFFF"
# The pad's screens are square; key faces get rounded corners (pixels on the 102-px face,
# the "corner_radius" setting) so they look like the configurator. 0 = square, 51 = round.
DEFAULT_CORNER = 12
MAX_CORNER = S // 2
TEXT_FONTS = ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/HelveticaNeue.ttc",
              "/System/Library/Fonts/Helvetica.ttc")
EMOJI_FONT = "/System/Library/Fonts/Apple Color Emoji.ttc"


@functools.lru_cache(maxsize=64)
def _text_font(size: int):
    for path in TEXT_FONTS:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                try:
                    f.set_variation_by_name("Semibold")
                except Exception:
                    pass
                return f
            except OSError:
                continue
    return ImageFont.load_default(size)


@functools.lru_cache(maxsize=1)
def _emoji_font():
    return ImageFont.truetype(EMOJI_FONT, 160)  # Apple Color Emoji only has bitmap strikes, 160 is the largest


@functools.lru_cache(maxsize=128)
def _emoji_image(emoji: str) -> Optional[Image.Image]:
    try:
        font = _emoji_font()
    except OSError:
        return None
    canvas = Image.new("RGBA", (400, 220), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((10, 10), emoji, font=font, embedded_color=True)
    bbox = canvas.getbbox()
    return canvas.crop(bbox) if bbox else None


def _fit(img: Image.Image, box: int) -> Image.Image:
    """Scale up or down so the longer side is `box` pixels (keeps the aspect ratio)."""
    ratio = box / max(img.width, img.height)
    return img.resize((max(1, round(img.width * ratio)), max(1, round(img.height * ratio))), Image.LANCZOS)


ICON_SCALE_MIN, ICON_SCALE_MAX = 30, 200


def _icon_scale(spec: dict) -> float:
    """"icon_scale": percent of the normal icon size (30-200, default 100)."""
    try:
        pct = float(spec.get("icon_scale", 100))
    except (TypeError, ValueError):
        pct = 100
    return max(ICON_SCALE_MIN, min(ICON_SCALE_MAX, pct)) / 100


def _wrap(draw, text: str, font, max_w: int):
    lines = []
    for paragraph in text.split("\n"):
        words, cur = paragraph.split(" "), ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if draw.textlength(trial, font=font) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def _draw_text(img: Image.Image, text: str, box, color, max_size: int):
    """Largest font that fits `text` (word-wrapped) inside box=(x0,y0,x1,y1)."""
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    for size in range(max_size, 8, -1):
        font = _text_font(size)
        lines = _wrap(draw, text, font, w)
        line_h = size * 1.12
        too_wide = any(draw.textlength(ln, font=font) > w for ln in lines)
        if not too_wide and line_h * len(lines) <= h:
            break
    top = y0 + (h - line_h * len(lines)) / 2
    for i, ln in enumerate(lines):
        draw.text(((x0 + x1) / 2, top + i * line_h + line_h / 2), ln, font=font, fill=color, anchor="mm")


@functools.lru_cache(maxsize=256)
def _text_mask(text: str, box: tuple, max_size: int) -> Image.Image:
    """The label as an alpha mask, so every frame of an animation reuses it."""
    mask = Image.new("L", (S, S), 0)
    _draw_text(mask, text, box, 255, max_size)
    return mask


def _put_text(img: Image.Image, text: str, box: tuple, color, max_size: int):
    img.paste(color, (0, 0), _text_mask(text, box, max_size))


def crop_of(spec: dict) -> Optional[tuple]:
    """The key's "crop" as a hashable (left, top, right, bottom), or None for the default
    (a picture is centred: covering the key, or whole with a label)."""
    c = spec.get("crop")
    if isinstance(c, (list, tuple)) and len(c) == 4 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in c) and c[2] > c[0] and c[3] > c[1]:
        return tuple(float(v) for v in c)
    return None


def _image_path(spec: dict, base_dir: str) -> Optional[str]:
    if not spec.get("image"):
        return None
    path = os.path.expanduser(spec["image"])
    return path if os.path.isabs(path) else os.path.join(base_dir, path)


@functools.lru_cache(maxsize=256)
def _frame_count(path: str, mtime: float, size: int) -> int:
    with Image.open(path) as img:
        return getattr(img, "n_frames", 1)


@functools.lru_cache(maxsize=12)  # one page of animated keys
def _frames_cached(path: str, mtime: float, size: int, crop: Optional[tuple]) -> tuple:
    with Image.open(path) as img:
        return tuple(anim.read_frames(img, crop))


def _frames(path: str, crop: Optional[tuple] = None) -> tuple:
    """((RGBA, ms), ...) for an animated image, () for a still one or a missing file."""
    try:
        st = os.stat(path)
        key = (path, st.st_mtime, st.st_size)
        return _frames_cached(*key, crop) if _frame_count(*key) > 1 else ()
    except FileNotFoundError:
        return ()
    except Exception as e:
        log.warning("cannot read animation %s: %s", path, e)
        return ()


def _key_timeline(spec: dict, base_dir: str) -> tuple:
    """One loop of a key that moves (see _timeline), () for a still one."""
    path = _image_path(spec, base_dir)
    frames = _frames(path, crop_of(spec)) if path else ()
    period = _bg_period(spec)
    if not frames and not period:
        return ()
    return _timeline(tuple(ms for _, ms in frames), period)


def frame_durations(spec: Optional[dict], base_dir: str) -> Optional[list]:
    """Milliseconds of each frame when the key moves (an animated picture, a moving
    background or both), otherwise None."""
    tl = _key_timeline(spec or {}, base_dir)
    return [ms for ms, _, _ in tl] if tl else None


def frame_at(spec: Optional[dict], base_dir: str, ms: float) -> int:
    """The frame a moving key shows `ms` after its loop started (0 for a still key)."""
    tl = _key_timeline(spec or {}, base_dir)
    if not tl:
        return 0
    ends = list(itertools.accumulate(d for d, _, _ in tl))
    return min(bisect.bisect_right(ends, ms % ends[-1]), len(tl) - 1)


def _load_icon(spec: dict, base_dir: str, frame: int = 0, first_only: bool = False) -> Optional[Image.Image]:
    """The picture or emoji of a key. `first_only` reads only the first frame of an
    animation, without filling the frame cache (for quick previews while cropping)."""
    path = _image_path(spec, base_dir)
    if path:
        crop = crop_of(spec)
        frames = () if first_only else _frames(path, crop)
        if frames:  # `frame` is the key's frame; with a moving background it is not the picture's
            tl = _timeline(tuple(ms for _, ms in frames), _bg_period(spec))
            return frames[tl[frame % len(tl)][1] % len(frames)][0]
        try:
            with Image.open(path) as img:
                img.seek(0)
                icon = img.convert("RGBA")
            return anim.apply_crop(icon, crop) if crop else icon
        except Exception as e:
            log.warning("cannot load image %s: %s", path, e)
    if spec.get("emoji"):
        return _emoji_image(spec["emoji"])
    return None


@functools.lru_cache(maxsize=8)
def _corner_mask(radius: int) -> Image.Image:
    """Rounded-square mask, drawn 4x larger and scaled down so the curve is smooth."""
    big = Image.new("L", (S * 4, S * 4), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, S * 4 - 1, S * 4 - 1), radius * 4, fill=255)
    return big.resize((S, S), Image.LANCZOS)


def round_corners(img: Image.Image, radius: int = DEFAULT_CORNER) -> Image.Image:
    """The face with its corners cut to black (pixels off on the pad)."""
    radius = max(0, min(MAX_CORNER, int(radius)))
    if not radius:
        return img
    out = Image.new("RGB", (S, S), "#000000")
    out.paste(img, (0, 0), _corner_mask(radius))
    return out


# ---------------------------------------------------------------- live keys
ALERT_BG = {1: "#B45309", 2: "#B91C1C"}  # a live text key that needs attention (white text stays readable)
METER_COLORS = ("#3987E5", "#FAB219", "#D03B3B")  # normal, warning, critical
METER_DIM = "#807F79"
_K = 4  # meters are drawn 4x larger and scaled down, for smooth lines


@functools.lru_cache(maxsize=32)
def _weight_font(size: int, weight: str):
    for path in TEXT_FONTS:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                try:
                    f.set_variation_by_name(weight)
                except Exception:
                    pass
                return f
            except OSError:
                continue
    return ImageFont.load_default(size)


def _rgb(color: str, fallback: str) -> tuple:
    try:
        return ImageColor.getrgb(color)[:3]
    except (ValueError, TypeError, AttributeError):
        return ImageColor.getrgb(fallback)[:3]


def _mix(a: tuple, b: tuple, k: float) -> tuple:
    return tuple(round(x + (y - x) * k) for x, y in zip(a, b))


def _fit_text(draw, text: str, weight: str, size: int, max_w: float):
    """The font (down to 10 px) and text, cut with "…" when even that is too wide."""
    for px in range(size, 9, -1):
        font = _weight_font(px * _K, weight)
        if draw.textlength(text, font=font) <= max_w:
            return font, text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return font, (text + "…") if text else ""


def _meter(spec: dict, face: dict) -> Image.Image:
    """A gauge: title, the number, the last minute as a line, and a bar coloured by level."""
    W = S * _K
    bg = _rgb(spec.get("color", DEFAULT_BG), DEFAULT_BG)
    ink = _rgb(spec.get("text_color", DEFAULT_FG), DEFAULT_FG)
    ink2, muted = _mix(bg, ink, 0.78), _mix(bg, ink, 0.52)
    level = max(0, min(2, int(face.get("level") or 0)))
    color = _rgb(METER_DIM, METER_DIM) if face.get("dim") else _rgb(METER_COLORS[level], METER_COLORS[0])
    img = Image.new("RGB", (W, W), bg)
    d = ImageDraw.Draw(img)
    m = 10 * _K  # side margin, clear of the rounded corners

    note = face.get("note") or ""
    note_font, note = _fit_text(d, note, "Medium", 11, W * 0.45) if note else (None, "")
    note_w = d.textlength(note, font=note_font) + 4 * _K if note else 0
    if note:
        d.text((W - m, 9 * _K), note, font=note_font, fill=muted, anchor="ra")
    title_font, title = _fit_text(d, face.get("title") or "", "Semibold", 15, W - 2 * m - note_w)
    if title:
        d.text((m, 8 * _K), title, font=title_font, fill=ink2)

    value = face.get("value")
    big = _weight_font(36 * _K, "Bold")
    text = "–" if value is None else str(value)
    while d.textlength(text, font=big) > W - 2 * m - 20 * _K and big.size > 16 * _K:
        big = _weight_font(big.size - 2 * _K, "Bold")
    base = 58 * _K
    d.text((m, base), text, font=big, fill=ink, anchor="ls")
    if value is not None and face.get("unit"):
        x = m + d.textlength(text, font=big) + 2 * _K
        d.text((x, base), face["unit"], font=_weight_font(17 * _K, "Semibold"), fill=ink2, anchor="ls")

    # the last minute, on a fixed 0-100 scale so a flat line at the bottom really means idle
    top, bottom, left, right = 64 * _K, 82 * _K, m, W - m
    vals = list(face.get("history") or ())[-30:]
    if len(vals) >= 2:
        step = (right - left) / 29
        x0 = right - step * (len(vals) - 1)
        pts = [(x0 + i * step, bottom - (bottom - top) * max(0.0, min(100.0, v)) / 100) for i, v in enumerate(vals)]
        line = _mix(bg, ink, 0.45)
        d.polygon(pts + [(pts[-1][0], bottom), (pts[0][0], bottom)], fill=_mix(bg, line, 0.35))
        d.line(pts, fill=line, width=2 * _K, joint="curve")
        cx, cy = pts[-1]
        r = 3 * _K
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color, outline=bg, width=_K)

    # the bar, on a track in a dark step of its own colour
    my, mh = 88 * _K, 7 * _K
    d.rounded_rectangle((left, my, right, my + mh), radius=mh // 2, fill=_mix(bg, color, 0.28))
    fill_w = 0 if value is None else (right - left) * max(0.0, min(100.0, float(value))) / 100
    if fill_w >= mh:
        d.rounded_rectangle((left, my, left + fill_w, my + mh), radius=mh // 2, fill=color)
    elif fill_w > 0:
        d.ellipse((left, my, left + mh, my + mh), fill=color)
    return img.resize((S, S), Image.LANCZOS)


# ---------------------------------------------------------------- backgrounds
# "background": {"style": "gradient", "colors": ["#FF5F6D", "#FFC371"], "angle": 135, "speed": 1}
# gradient: still, from the first colour to the last along `angle` (degrees, as in CSS: 0 towards
# the top, 90 towards the right). animated: the colours keep flowing along the angle. waves: wavy
# bands of the colours that roll by. "color" stays the key's plain colour (meters and alerts use it).
BG_STYLES = ("solid", "gradient", "animated", "waves")
BG_ANGLE = {"gradient": 135, "animated": 135, "waves": 180}  # default angle of each style
BG_PERIOD = {"animated": 6000, "waves": 4800}  # ms per loop at speed 1
BG_STEP = 66          # ms per frame of a moving background (about 15 a second)
BG_MAX_FRAMES = 90    # per loop of the background
MAX_FRAMES = 360      # per loop of a key whose picture and background both move
SPEED_MIN, SPEED_MAX = 0.25, 4
_G = 34               # backgrounds are worked out on a 34x34 grid and smoothed up to the key
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}){1,2}")


def is_color(c) -> bool:
    return isinstance(c, str) and _HEX.fullmatch(c) is not None


def validate_background(b) -> str:
    """An error message for a key's "background", or "" when it is fine."""
    if not isinstance(b, dict):
        return t('background must be an object like {"style": "gradient", "colors": ["#FF5F6D", "#FFC371"]}')
    style = b.get("style", "gradient")
    if style not in BG_STYLES:
        return t("the background style must be one of: {values}", values=", ".join(BG_STYLES))
    if style == "solid":
        return ""
    colors = b.get("colors")
    if not isinstance(colors, list) or not 2 <= len(colors) <= 3 or not all(map(is_color, colors)):
        return t('the background needs 2 or 3 colors, like ["#FF5F6D", "#FFC371"]')
    angle = b.get("angle", 0)
    if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not 0 <= angle <= 360:
        return t("the background angle must be a number of degrees from 0 to 360")
    speed = b.get("speed", 1)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not SPEED_MIN <= speed <= SPEED_MAX:
        return t("the background speed must be a number from {min} to {max}", min=SPEED_MIN, max=SPEED_MAX)
    return ""


def background_of(spec: dict) -> Optional[tuple]:
    """(style, colors, angle, speed) of a key's gradient background, None for a plain colour."""
    b = spec.get("background")
    if not isinstance(b, dict) or b.get("style", "gradient") == "solid" or validate_background(b):
        return None
    style = b.get("style", "gradient")
    return (style, tuple(c.upper() for c in b["colors"]),
            float(b.get("angle", BG_ANGLE[style])) % 360, float(b.get("speed", 1)))


def _bg_period(spec: dict) -> Optional[int]:
    """ms per loop of a moving background, None for a still one or a plain colour."""
    bg = background_of(spec)
    return round(BG_PERIOD[bg[0]] / bg[3]) if bg and bg[0] in BG_PERIOD else None


@functools.lru_cache(maxsize=64)
def _timeline(gif: tuple, period: Optional[int]) -> tuple:
    """((ms, picture frame, background phase 0-1), ...): one loop of a key that moves. `gif`
    is the ms of each frame of its animated picture, `period` the ms per loop of its moving
    background. With both, the loop holds a whole number of each, and a frame starts wherever
    either of them changes. The background's speed bends to fit: a little, or more when a
    long picture would make the loop too many frames (MAX_FRAMES)."""
    if not period:
        return tuple((ms, i, 0.0) for i, ms in enumerate(gif))
    if not gif:
        n = max(2, min(BG_MAX_FRAMES, round(period / BG_STEP)))
        cuts = [round(k * period / n) for k in range(n + 1)]
        return tuple((cuts[k + 1] - cuts[k], 0, k / n) for k in range(n))
    total = sum(gif)
    loops = max(1, min(round(period / total), MAX_FRAMES // len(gif)))  # picture loops in one loop of both
    length = total * loops
    turns = max(1, round(length / period))  # background loops in the same time
    ends = list(itertools.accumulate(gif))
    starts = [loop * total + s for loop in range(loops) for s in [0] + ends[:-1]]
    steps = max(2, min(BG_MAX_FRAMES, round(length / turns / BG_STEP)))
    while True:
        bg_cuts = [round(j * length / (turns * steps)) for j in range(turns * steps)]
        cuts = set(starts)  # the picture changes on time; a background step that close waits for it
        for c in bg_cuts:
            k = bisect.bisect_left(starts, c)
            if all(abs(c - starts[j]) >= 10 for j in (k - 1, k) if 0 <= j < len(starts)):  # no slivers of a frame
                cuts.add(c)
        cuts = sorted(cuts)
        if len(cuts) <= MAX_FRAMES or steps <= 2:
            break
        steps = max(2, steps * 2 // 3)
    out = []
    for k, c in enumerate(cuts):
        nxt = cuts[k + 1] if k + 1 < len(cuts) else length
        pic = min(bisect.bisect_right(ends, c % total), len(gif) - 1)
        step = (bisect.bisect_right(bg_cuts, c) - 1) % steps
        out.append((nxt - c, pic, step / steps))
    return tuple(out)


@functools.lru_cache(maxsize=16)
def _ramp(colors: tuple, cyclic: bool, crisp: bool = False) -> tuple:
    """256 RGB steps (bytes) through the colours, back to the first one when cyclic, eased
    so the colour stops do not show as lines (`crisp`: each colour holds longer, for bands)."""
    stops = [_rgb(c, DEFAULT_BG) for c in colors] + ([_rgb(colors[0], DEFAULT_BG)] if cyclic else [])
    n = len(stops) - 1
    out = []
    for i in range(256):
        x = i / 256 * n if cyclic else i / 255 * n
        k = min(int(x), n - 1)
        f = x - k
        f = f * f * f * (f * (f * 6 - 15) + 10) if crisp else f * f * (3 - 2 * f)
        out.append(bytes(_mix(stops[k], stops[k + 1], f)))
    return tuple(out)


@functools.lru_cache(maxsize=192)
def _background(style: str, colors: tuple, angle: float, phase: float) -> Image.Image:
    """A gradient background at `phase` (0-1) of its loop."""
    ramp = _ramp(colors, style != "gradient", style == "waves")
    a = math.radians(angle)
    dx, dy = math.sin(a), -math.cos(a)
    span = abs(dx) + abs(dy)  # corner to corner along the angle, as in CSS
    tau = 2 * math.pi
    data = bytearray()
    for j in range(_G):
        y = (j + 0.5) / _G - 0.5
        for i in range(_G):
            x = (i + 0.5) / _G - 0.5
            u = (x * dx + y * dy) / span + 0.5  # 0 at the first corner, 1 at the last
            if style == "gradient":
                data += ramp[min(255, max(0, round(u * 255)))]
                continue
            if style == "animated":
                w = 0.5 * u - phase
            else:  # waves: bands across the angle, their edges rolling sideways
                v = (y * dx - x * dy) / span + 0.5
                w = (1.1 * u + 0.12 * math.sin(tau * (v - phase))
                     + 0.05 * math.sin(tau * (2 * v + 2 * phase) + 1.3) - phase)
            data += ramp[int((w % 1.0) * 256) % 256]
    return Image.frombytes("RGB", (_G, _G), bytes(data)).resize((S, S), Image.BICUBIC)


# ---------------------------------------------------------------- press effects
# "press_effect": {"style": "ripple", "color": "#FFFFFF"} plays over the key when it is pressed,
# in place of darkening it.
EFFECTS = {"ripple": 450, "flash": 380, "glow": 520, "sparkle": 560, "shine": 520, "bounce": 320}  # ms
EFFECT_STEP = 30  # ms per frame
DEFAULT_EFFECT_COLOR = "#FFFFFF"


def validate_effect(e) -> str:
    """An error message for a key's "press_effect", or "" when it is fine."""
    if not isinstance(e, dict):
        return t('press_effect must be an object like {"style": "ripple"}')
    if e.get("style") not in EFFECTS:
        return t("the press effect must be one of: {values}", values=", ".join(EFFECTS))
    if "color" in e and not is_color(e["color"]):
        return t('the press effect color must be a color like "#FFFFFF"')
    return ""


def effect_of(spec: Optional[dict]) -> Optional[tuple]:
    """(style, color) of a key's press effect, or None."""
    e = (spec or {}).get("press_effect")
    if not isinstance(e, dict) or validate_effect(e):
        return None
    return e["style"], e.get("color", DEFAULT_EFFECT_COLOR)


def _ease_out(p: float) -> float:
    return 1 - (1 - p) ** 3


@functools.lru_cache(maxsize=1)
def _diagonal() -> Image.Image:
    """0 at the top left corner up to 255 at the bottom right (for the shine)."""
    return Image.frombytes("L", (S, S), bytes(round(255 * (x + y) / (2 * (S - 1))) for y in range(S) for x in range(S)))


@functools.lru_cache(maxsize=8)
def _edge(radius: int) -> Image.Image:
    """A soft light along the inside of the key's edge (for the glow)."""
    line = Image.new("L", (S, S), 0)
    ImageDraw.Draw(line).rounded_rectangle((1, 1, S - 2, S - 2), max(0, radius - 1), outline=255, width=4)
    halo = line.filter(ImageFilter.GaussianBlur(7)).point(lambda v: min(255, v * 3))
    return ImageChops.lighter(halo, line.filter(ImageFilter.GaussianBlur(1)))


def _overlay(img: Image.Image, style: str, color: str, p: float, corner: int) -> Image.Image:
    """`img` with its press effect `p` (0-1) of the way through, before the corners are cut."""
    rgb = _rgb(color, DEFAULT_EFFECT_COLOR)
    q = 1 - p
    if style == "flash":
        return Image.blend(img, Image.new("RGB", (S, S), rgb), 0.85 * q * q)
    if style == "shine":  # a band of light across the key, top left to bottom right
        c = -0.3 + 1.6 * p * p * (3 - 2 * p)
        mask = _diagonal().point([round(235 * max(0.0, 1 - abs(v / 255 - c) / 0.17) ** 1.5) for v in range(256)])
    elif style == "glow":  # the edge lights up at once and fades
        e = math.sin(math.pi * p ** 0.5)
        img = Image.blend(img, Image.new("RGB", (S, S), rgb), 0.16 * e)
        mask = _edge(corner).point(lambda v: round(v * e))
    else:  # ripple and sparkle are drawn 4x larger and scaled down, for smooth edges
        k = 4
        big = Image.new("L", (S * k, S * k), 0)
        d = ImageDraw.Draw(big)
        c = S * k / 2
        if style == "ripple":  # a ring that spreads out from the middle, and a soft fill behind it
            r = (0.1 + 0.62 * _ease_out(p)) * S * k
            d.ellipse((c - r, c - r, c + r, c + r), fill=round(80 * q * q))
            w = max(1.5, 7 * q) * k
            d.ellipse((c - r - w / 2, c - r - w / 2, c + r + w / 2, c + r + w / 2),
                      outline=round(240 * q ** 1.3), width=round(w))
        else:  # sparkle: a burst in the middle and ten little stars flying out
            if p < 0.3:
                r = (0.08 + 0.3 * p / 0.3) * S * k
                d.ellipse((c - r, c - r, c + r, c + r), fill=round(190 * (1 - p / 0.3)))
            for n in range(10):
                ang = 2 * math.pi * n / 10 + 0.3
                dist = (0.12 + 0.42 * (1.0 if n % 2 == 0 else 0.7) * _ease_out(p)) * S * k
                size = (11 if n % 2 == 0 else 8) * (1 - 0.5 * p) * k
                cx, cy = c + dist * math.cos(ang), c + dist * math.sin(ang)
                d.polygon([(cx + rr * math.cos(aa), cy + rr * math.sin(aa))
                           for m in range(8) for rr, aa in [(size if m % 2 == 0 else size * 0.36,
                                                             math.pi / 4 * m + 1.5 * p)]],
                          fill=round(255 * q ** 1.2))
        mask = big.resize((S, S), Image.LANCZOS)
    img = img.copy()
    img.paste(rgb, (0, 0, S, S), mask)
    return img


def _bounce(img: Image.Image, p: float) -> Image.Image:
    """The face pushed in and springing back (after its corners are cut)."""
    if p < 0.4:
        s = 1 - 0.13 * _ease_out(p / 0.4)
    else:
        q = (p - 0.4) / 0.6
        s = 0.87 + 0.13 * (1 - (1 - q) ** 2) + 0.035 * math.sin(math.pi * q)
    side = round(S * s)
    if side == S:
        return img
    face = img.resize((side, side), Image.LANCZOS)
    off = (S - side) // 2
    if side > S:
        return face.crop((-off, -off, -off + S, -off + S))
    out = Image.new("RGB", (S, S), "#000000")
    out.paste(face, (off, off))
    return out


# ---------------------------------------------------------------- sound keys
RING_W = 4        # the playing ring's width, px
RING_STEPS = 60   # the ring moves in this many steps (the service sends a key again on each)


@functools.lru_cache(maxsize=8)
def _ring_path(corner: int) -> tuple:
    """Points along the key's edge (at 4x), clockwise from the top middle, following its
    rounded corners, and the length of the path up to each point."""
    d = RING_W / 2 + 1                                 # the line's middle, from the edge
    rc = max(0.5, max(0, min(MAX_CORNER, corner)) - d)  # its corners' radius, around the key's
    lo, hi = d + rc, S - d - rc
    pts = [(S / 2, d)]
    for cx, cy, a0, (ex, ey) in ((hi, lo, -90, (S - d, hi)), (hi, hi, 0, (lo, S - d)),
                                 (lo, hi, 90, (d, lo)), (lo, lo, 180, (S / 2, d))):
        pts += [(cx + rc * math.cos(math.radians(a0 + j * 10)), cy + rc * math.sin(math.radians(a0 + j * 10)))
                for j in range(10)]
        pts.append((ex, ey))
    pts = [(x * 4, y * 4) for x, y in pts]
    cum = [0.0]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    return tuple(pts), tuple(cum)


def _ring(img: Image.Image, p: float, corner: int, color) -> Image.Image:
    """A ring around the key: faint all the way round while its sound plays, solid as far
    as the sound has got (p, 0-1)."""
    pts, cum = _ring_path(corner)
    rgb = ImageColor.getrgb(color) if isinstance(color, str) else tuple(color)[:3]
    w = RING_W * 4
    layer = Image.new("RGBA", (S * 4, S * 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.line(pts, fill=rgb + (80,), width=w, joint="curve")
    want = max(0.0, min(1.0, p)) * cum[-1]
    if want > 0:
        i = bisect.bisect_left(cum, want)
        f = (want - cum[i - 1]) / ((cum[i] - cum[i - 1]) or 1)
        end = (pts[i - 1][0] + f * (pts[i][0] - pts[i - 1][0]), pts[i - 1][1] + f * (pts[i][1] - pts[i - 1][1]))
        part = list(pts[:i]) + [end]
        draw.line(part, fill=rgb + (255,), width=w, joint="curve")
        for x, y in (part[0], end):  # round ends
            draw.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=rgb + (255,))
    out = img.convert("RGBA")
    out.alpha_composite(layer.resize((S, S), Image.LANCZOS))
    return out.convert("RGB")


def _finish(img: Image.Image, corner: int, effect: Optional[tuple]) -> Image.Image:
    if effect and effect[0] != "bounce":
        img = _overlay(img, effect[0], effect[1], max(0.0, min(1.0, effect[2])), corner)
    img = round_corners(img, corner)
    if effect and effect[0] == "bounce":
        img = _bounce(img, max(0.0, min(1.0, effect[2])))
    return img


def effect_preview(spec: dict, base_dir: str = ".", corner: int = DEFAULT_CORNER, live: Optional[dict] = None):
    """(frames, ms): the key's press effect played once, then the key as it is (for the
    configurator's "Try it")."""
    style, color = effect_of(spec)
    total = EFFECTS[style]
    frames, durs = [], []
    for at in range(0, total, EFFECT_STEP):
        frames.append(render_key(spec, base_dir, frame=frame_at(spec, base_dir, at), corner=corner,
                                 live=live, effect=(style, color, at / total)))
        durs.append(EFFECT_STEP)
    frames.append(render_key(spec, base_dir, frame=frame_at(spec, base_dir, total), corner=corner, live=live))
    durs.append(600)
    return frames, durs


def render_key(spec: Optional[dict], base_dir: str = ".", pressed: bool = False, frame: int = 0,
               first_only: bool = False, corner: int = DEFAULT_CORNER, live: Optional[dict] = None,
               effect: Optional[tuple] = None, playing: Optional[float] = None) -> Image.Image:
    """The key face; `frame` picks the frame of a key that moves (its animated picture and
    moving background), `corner` rounds it. `live` is what a live key shows now
    (live.Engine.face): its text in place of the label, or the whole face for a meter.
    `effect` is (style, color, progress 0-1) of a press effect playing over it, `playing`
    how far the key's sound has got (0-1) while it plays."""
    spec = spec or {}
    if live and live.get("style") == "meter":
        img = _meter(spec, live)
        if playing is not None:
            img = _ring(img, playing, corner, spec.get("text_color", DEFAULT_FG))
        if pressed:
            img = Image.blend(img, Image.new("RGB", (S, S), "#000000"), 0.45)
        return _finish(img, corner, effect)
    bg = spec.get("color", DEFAULT_BG if spec else "#000000")
    grad = background_of(spec)
    if live and live.get("level"):  # needs attention: amber, or red
        bg, grad = ALERT_BG[min(2, live["level"])], None
    fg = spec.get("text_color", DEFAULT_FG)
    if grad:
        phase = 0.0
        if frame and grad[0] in BG_PERIOD:
            tl = _key_timeline(spec, base_dir)
            phase = tl[frame % len(tl)][2] if tl else 0.0
        img = _background(*grad[:3], phase).copy()
    else:
        img = Image.new("RGB", (S, S), bg)
    icon = _load_icon(spec, base_dir, frame, first_only)
    if live:
        label = live.get("text", "")
    else:
        label = "" if spec.get("hide_label") else spec.get("label", "")  # hidden labels stay in the config

    scale = _icon_scale(spec)
    if icon is not None and spec.get("image") and spec.get("fill", True) and not label:
        side = max(4, round(S * scale))  # photo-style: cover the key; >100% zooms in, <100% leaves a border
        face = ImageOps.fit(icon, (side, side), Image.LANCZOS)
        off = (S - side) // 2
        img.paste(face, (off, off), face)
    elif icon is not None:
        icon = _fit(icon, max(4, round((66 if label else 80) * scale)))
        cy = 41 if label else S / 2  # centre of the icon area (above the label when there is one)
        img.paste(icon, (round((S - icon.width) / 2), round(cy - icon.height / 2)), icon)
        if label:
            _put_text(img, label, (4, 76, S - 4, S - 3), fg, 18)
    elif label:
        _put_text(img, label, (6, 6, S - 6, S - 6), fg, 30)

    if playing is not None:
        img = _ring(img, playing, corner, fg)
    if pressed:
        img = Image.blend(img, Image.new("RGB", (S, S), "#000000"), 0.45)
    return _finish(img, corner, effect)


def to_bgr(img: Image.Image) -> bytes:
    img = img.convert("RGB")
    if img.size != (S, S):
        img = img.resize((S, S), Image.LANCZOS)
    r, g, b = img.split()
    return Image.merge("RGB", (b, g, r)).tobytes()


def preview_sheet(keys: list, base_dir: str = ".", corner: int = DEFAULT_CORNER, live=None) -> Image.Image:
    """Render 12 key specs as the physical 6x2 layout (for previews / docs). `live(spec)`
    gives what a live key shows now."""
    gap = 14
    sheet = Image.new("RGB", (6 * S + 7 * gap, 2 * S + 3 * gap), "#0B0B0E")
    for i, spec in enumerate(keys[:12]):
        x = gap + (i % 6) * (S + gap)
        y = gap + (i // 6) * (S + gap)
        sheet.paste(render_key(spec, base_dir, corner=corner, live=live(spec) if live else None), (x, y))
    return sheet
