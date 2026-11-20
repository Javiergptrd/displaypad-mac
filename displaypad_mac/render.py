"""Draw 102x102 key faces from a key's config: image, emoji, label, colours.
Animated images (GIF, WebP, APNG, converted Lottie) give one face per frame; live keys
(live.py) get their text, or are drawn as a meter."""

import functools
import logging
import os
from typing import Optional

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from . import anim
from .driver import ICON_SIZE

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


def frame_durations(spec: Optional[dict], base_dir: str) -> Optional[list]:
    """Milliseconds of each frame when the key shows an animation, otherwise None."""
    path = _image_path(spec or {}, base_dir)
    frames = _frames(path, crop_of(spec or {})) if path else ()
    return [ms for _, ms in frames] if frames else None


def _load_icon(spec: dict, base_dir: str, frame: int = 0, first_only: bool = False) -> Optional[Image.Image]:
    """The picture or emoji of a key. `first_only` reads only the first frame of an
    animation, without filling the frame cache (for quick previews while cropping)."""
    path = _image_path(spec, base_dir)
    if path:
        crop = crop_of(spec)
        frames = () if first_only else _frames(path, crop)
        if frames:
            return frames[frame % len(frames)][0]
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


def render_key(spec: Optional[dict], base_dir: str = ".", pressed: bool = False, frame: int = 0,
               first_only: bool = False, corner: int = DEFAULT_CORNER, live: Optional[dict] = None) -> Image.Image:
    """The key face; `frame` picks the frame of an animated image, `corner` rounds it.
    `live` is what a live key shows now (live.Engine.face): its text in place of the label,
    or the whole face for a meter."""
    spec = spec or {}
    if live and live.get("style") == "meter":
        img = _meter(spec, live)
        if pressed:
            img = Image.blend(img, Image.new("RGB", (S, S), "#000000"), 0.45)
        return round_corners(img, corner)
    bg = spec.get("color", DEFAULT_BG if spec else "#000000")
    if live and live.get("level"):  # needs attention: amber, or red
        bg = ALERT_BG[min(2, live["level"])]
    fg = spec.get("text_color", DEFAULT_FG)
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

    if pressed:
        img = Image.blend(img, Image.new("RGB", (S, S), "#000000"), 0.45)
    return round_corners(img, corner)


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
