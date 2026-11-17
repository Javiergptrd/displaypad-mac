"""Draws the launcher icon (a small DisplayPad) into a macOS .iconset folder."""

import os
import sys

from PIL import Image, ImageDraw, ImageFilter

COLORS = ["#0B63C5", "#5B5FC7", "#1D7A45", "#E8A13A", "#3B4A5C", "#C2553A",
          "#7A4FC1", "#C23B4B", "#2E8B6E", "#5A6B7D", "#2F80ED", "#8A8F99"]


def draw(size: int = 1024) -> Image.Image:
    s = size / 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # macOS icon grid: ~824px body centred in 1024 with a soft shadow
    body = (int(100 * s), int(250 * s), int(924 * s), int(774 * s))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((body[0], body[1] + 18 * s, body[2], body[3] + 18 * s), 90 * s, fill=(0, 0, 0, 110))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(24 * s)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(body, 90 * s, fill="#1E1E25", outline="#34343E", width=max(1, int(6 * s)))
    key, gap = 104 * s, 22 * s
    x0 = body[0] + (body[2] - body[0] - (6 * key + 5 * gap)) / 2
    y0 = body[1] + (body[3] - body[1] - (2 * key + gap)) / 2
    gloss = Image.new("RGBA", img.size, (0, 0, 0, 0))
    g = ImageDraw.Draw(gloss)
    for i, c in enumerate(COLORS):
        x = x0 + (i % 6) * (key + gap)
        y = y0 + (i // 6) * (key + gap)
        d.rounded_rectangle((x, y, x + key, y + key), 18 * s, fill=c)
        g.rounded_rectangle((x, y, x + key, y + key * 0.45), 18 * s, fill=(255, 255, 255, 34))
    img.alpha_composite(gloss)
    return img


def write_iconset(path: str):
    os.makedirs(path, exist_ok=True)
    big = draw(1024)
    for pt in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = pt * scale
            name = f"icon_{pt}x{pt}{'@2x' if scale == 2 else ''}.png"
            big.resize((px, px), Image.LANCZOS).save(os.path.join(path, name))


if __name__ == "__main__":
    write_iconset(sys.argv[1])
