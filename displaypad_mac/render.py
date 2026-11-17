"""Draw 102x102 key faces from a key's config: image, emoji, label, colours.
Animated images (GIF, WebP, APNG, converted Lottie) give one face per frame."""

import functools
import logging
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import anim
from .driver import ICON_SIZE

log = logging.getLogger(__name__)

S = ICON_SIZE
DEFAULT_BG = "#1C1C24"
DEFAULT_FG = "#FFFFFF"
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


def render_key(spec: Optional[dict], base_dir: str = ".", pressed: bool = False, frame: int = 0,
               first_only: bool = False) -> Image.Image:
    """The key face; `frame` picks the frame of an animated image."""
    spec = spec or {}
    bg = spec.get("color", DEFAULT_BG if spec else "#000000")
    fg = spec.get("text_color", DEFAULT_FG)
    img = Image.new("RGB", (S, S), bg)
    icon = _load_icon(spec, base_dir, frame, first_only)
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
    return img


def to_bgr(img: Image.Image) -> bytes:
    img = img.convert("RGB")
    if img.size != (S, S):
        img = img.resize((S, S), Image.LANCZOS)
    r, g, b = img.split()
    return Image.merge("RGB", (b, g, r)).tobytes()


def preview_sheet(keys: list, base_dir: str = ".") -> Image.Image:
    """Render 12 key specs as the physical 6x2 layout (for previews / docs)."""
    gap = 14
    sheet = Image.new("RGB", (6 * S + 7 * gap, 2 * S + 3 * gap), "#0B0B0E")
    for i, spec in enumerate(keys[:12]):
        x = gap + (i % 6) * (S + gap)
        y = gap + (i // 6) * (S + gap)
        sheet.paste(render_key(spec, base_dir), (x, y))
    return sheet
