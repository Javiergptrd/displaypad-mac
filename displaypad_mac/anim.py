"""Animated key images: animated GIF / WebP / APNG and Lottie (.json, .lottie, .tgs).

The DisplayPad has no animation support of its own: it shows whatever 102x102
image it was sent last, so the service streams the frames (see daemon.py).
Uploads are normalised here to one animated WebP, with the shorter side at most
2x the key size (enough for a 200 % zoom, also after cropping a wide picture to a
square) and MAX_FRAMES frames, so the service only ever has to decode one format. Lottie is drawn with rlottie in a child process
(`python -m displaypad_mac.anim IN OUT`) so a malformed file cannot crash the
service.
"""

import base64
import gzip
import io
import json
import math
import os
import posixpath
import re
import subprocess
import sys
import zipfile

from PIL import Image

from . import i18n
from .i18n import t

MAX_SIDE = 204          # 2x the 102 px key
MAX_LONG = 2 * MAX_SIDE # longer side of a stored animation: a 2:1 picture keeps a full square to crop
MAX_FRAMES = 180        # 7 s at 25 fps; ~31 KB per frame once drawn for the pad
MAX_FPS = 25            # the pad shows ~50 key images per second in total: two Lottie keys fit
MIN_FRAME_MS = 20
MAX_JSON = 20 * 1024 * 1024
LOTTIE_TIMEOUT = 120
PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def frame_ms(raw) -> int:
    """GIF delay in ms, read the way browsers do: 0-10 ms means 100 ms."""
    try:
        ms = int(raw or 0)
    except (TypeError, ValueError):
        ms = 0
    return 100 if ms <= 10 else max(MIN_FRAME_MS, ms)


def limit_size(img: Image.Image, short: int, long: int) -> Image.Image:
    """Scale down (never up, never stretched) so the shorter side is at most `short`
    pixels and the longer side at most `long`."""
    k = min(1.0, short / min(img.size), long / max(img.size))
    if k >= 1:
        return img
    return img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)


def crop_box(size: tuple, crop) -> tuple:
    """A key's "crop" [left, top, right, bottom], fractions of the picture, in pixels.
    It may reach past the edges: that part stays transparent (the key's colour)."""
    w, h = size
    left, top, right, bottom = crop
    return left * w, top * h, right * w, bottom * h


def apply_crop(img: Image.Image, crop, limit: int = MAX_SIDE) -> Image.Image:
    """The cropped part of an RGBA picture, its longer side at most `limit` pixels.
    Only the visible part is resized, so zooming out never builds a huge canvas."""
    x0, y0, x1, y1 = crop_box(img.size, crop)
    bw, bh = x1 - x0, y1 - y0
    k = min(1.0, limit / max(bw, bh))
    out = Image.new("RGBA", (max(1, round(bw * k)), max(1, round(bh * k))), (0, 0, 0, 0))
    sx0, sy0, sx1, sy1 = max(0.0, x0), max(0.0, y0), min(float(img.width), x1), min(float(img.height), y1)
    dw, dh = round((sx1 - sx0) * k), round((sy1 - sy0) * k)
    if dw > 0 and dh > 0:
        part = img.resize((dw, dh), Image.LANCZOS, box=(sx0, sy0, sx1, sy1))
        out.paste(part, (round((sx0 - x0) * k), round((sy0 - y0) * k)))
    return out


def read_frames(img: Image.Image, crop=None, upload: bool = False) -> list:
    """[(RGBA image, duration ms), ...] for every frame of an open PIL image: cropped
    when `crop` is given, at most MAX_SIDE on the longer side for drawing, or (upload)
    at most MAX_SIDE on the shorter side and MAX_LONG on the longer one for storing.
    Long animations keep every k-th frame (and its time) so they stay under MAX_FRAMES."""
    n = getattr(img, "n_frames", 1)
    step = max(1, math.ceil(n / MAX_FRAMES))
    frames = []
    for i in range(n):
        img.seek(i)
        img.load()  # WebP only updates info["duration"] once the frame is loaded
        ms = frame_ms(img.info.get("duration"))
        if i % step == 0:
            f = img.convert("RGBA")
            if upload:
                f = limit_size(f, MAX_SIDE, MAX_LONG)
            elif crop:
                f = apply_crop(f, crop)
            else:
                f.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            frames.append([f, ms])
        else:
            frames[-1][1] += ms
    return [(f, ms) for f, ms in frames]


def save_webp(frames: list, path: str):
    images = [f for f, _ in frames]
    tmp = path + ".tmp"
    images[0].save(tmp, "WEBP", save_all=True, append_images=images[1:],
                   duration=[ms for _, ms in frames], loop=0, lossless=True, quality=60, method=4)
    os.replace(tmp, path)


# ---------------------------------------------------------------- Lottie
def _is_lottie(obj) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("layers"), list) and "op" in obj


def _read_member(z: zipfile.ZipFile, name: str) -> bytes:
    if z.getinfo(name).file_size > MAX_JSON:
        raise ValueError(t("the .lottie file is too large"))
    return z.read(name)


def _from_dotlottie(data: bytes) -> dict:
    """The first animation of a .lottie (zip) file, with its images embedded."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError(t("the .lottie file is damaged"))
    names = set(z.namelist())
    wanted = []
    if "manifest.json" in names:
        try:
            manifest = json.loads(_read_member(z, "manifest.json"))
            wanted = [f"{d}/{a['id']}.json" for a in manifest.get("animations", []) for d in ("animations", "a")]
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    wanted += sorted(n for n in names if re.fullmatch(r"(animations|a)/[^/]+\.json", n))
    member = next((n for n in wanted if n in names), None)
    if not member:
        raise ValueError(t("the .lottie file contains no animation"))
    obj = json.loads(_read_member(z, member))
    for asset in obj.get("assets", []) if isinstance(obj, dict) else []:
        p = asset.get("p") if isinstance(asset, dict) else None
        if not isinstance(p, str) or p.startswith("data:") or asset.get("e") == 1:
            continue
        for d in ("images", "i"):
            name = posixpath.join(d, posixpath.basename(p))
            if name in names:
                ext = posixpath.splitext(name)[1].lower().lstrip(".") or "png"
                mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                asset.update(u="", e=1, p=f"data:{mime};base64," + base64.b64encode(_read_member(z, name)).decode())
                break
    return obj


def lottie_json(data: bytes):
    """The Lottie animation in an upload (.json, .tgs = gzip, .lottie = zip) as a dict,
    or None when the file is not one of those (then it is treated as an image)."""
    if data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
                data = g.read(MAX_JSON + 1)
        except OSError:
            raise ValueError(t("the .tgs file is damaged"))
        if len(data) > MAX_JSON:
            raise ValueError(t("the animation is too large"))
    elif data[:4] == b"PK\x03\x04":
        obj = _from_dotlottie(data)
        if not _is_lottie(obj):
            raise ValueError(t("the .lottie file does not contain a valid Lottie animation"))
        return obj
    head = data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head.startswith(b"{"):
        return None
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(t("the JSON file cannot be read: {error}", error=e))
    if not _is_lottie(obj):
        raise ValueError(t("that JSON is not a Lottie animation (it has no “layers” and “op”)"))
    return obj


def render_lottie(text: str) -> list:
    """[(RGBA image, ms), ...] for a Lottie JSON string, at most MAX_FPS and MAX_FRAMES."""
    from rlottie_python import LottieAnimation

    anim = LottieAnimation.from_data(text)
    w, h = anim.lottie_animation_get_size()
    total = anim.lottie_animation_get_totalframe()
    seconds = anim.lottie_animation_get_duration()
    if w <= 0 or h <= 0 or total <= 0:
        raise ValueError(t("the Lottie animation is empty"))
    if seconds <= 0:
        seconds = total / 30
    scale = min(MAX_SIDE / min(w, h), MAX_LONG / max(w, h))  # same limits as other animations
    rw, rh = max(1, round(w * scale)), max(1, round(h * scale))
    fps = min(total / seconds, MAX_FPS)
    n = max(1, min(MAX_FRAMES, round(seconds * fps), total))
    frames = []
    for j in range(n):
        buf = anim.lottie_animation_render(frame_num=anim.lottie_animation_get_frame_at_pos(j / n),
                                           width=rw, height=rh)
        # rlottie draws premultiplied BGRA; "RGBa" un-premultiplies it so soft edges stay light
        img = Image.frombuffer("RGBa", (rw, rh), bytes(buf), "raw", "BGRa", 0, 1).convert("RGBA")
        t0, t1 = round(j * seconds * 1000 / n), round((j + 1) * seconds * 1000 / n)
        frames.append((img, max(MIN_FRAME_MS, t1 - t0)))
    if n > 1 and len({f.tobytes() for f, _ in frames}) == 1:
        raise ValueError(t("this Lottie animation was drawn without movement: it may use features the "
                           "Lottie reader does not support. Try exporting it as a GIF."))
    return frames


def convert_lottie(obj: dict, out_path: str) -> dict:
    """Draw a Lottie animation into an animated WebP, in a child process."""
    src = out_path + ".json"
    with open(src, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    env = dict(os.environ, PYTHONPATH=PKG_PARENT, DISPLAYPAD_LANG=i18n.language())
    try:
        r = subprocess.run([sys.executable, "-m", "displaypad_mac.anim", src, out_path],
                           capture_output=True, text=True, timeout=LOTTIE_TIMEOUT, env=env, cwd=PKG_PARENT)
    except subprocess.TimeoutExpired:
        raise ValueError(t("the animation took more than {seconds} s to prepare", seconds=LOTTIE_TIMEOUT))
    finally:
        try:
            os.remove(src)
        except OSError:
            pass
    if r.returncode != 0:
        lines = (r.stderr or "").strip().splitlines()
        raise ValueError(lines[-1] if lines and r.returncode == 2 else
                         t("the Lottie animation could not be drawn (it may use features the pad does not support)"))
    return json.loads(r.stdout.strip().splitlines()[-1])


def _main(argv):
    src, out = argv
    try:
        with open(src, encoding="utf-8") as f:
            frames = render_lottie(f.read())
        save_webp(frames, out)
    except ImportError:
        print(t("the Lottie component is missing: run install.sh again"), file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps({"frames": len(frames), "ms": sum(ms for _, ms in frames)}))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
