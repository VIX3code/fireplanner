#!/usr/bin/env python3
"""Render fixed-length countdown GIFs for dropping onto a slide.

A GIF is the only countdown that survives inside PowerPoint with no network
and no add-in. The trade is that it is fixed: it starts when the slide
appears and it cannot be paused or extended. Use it for the segments you
control (a break you called, a build-up to a reveal); use the browser timer
for anything a client question might stretch.

    python3 tools/make_countdown_gifs.py                # 5, 10, 15 min
    python3 tools/make_countdown_gifs.py 3 7 20         # any minutes you like

Frames are emitted one per second and Pillow stores only the pixels that
changed between them, so a 15-minute countdown is a few hundred kilobytes
rather than a few hundred megabytes.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1000, 420
BG = (10, 10, 11)
INK = (244, 244, 245)          # counting down
WARN = (255, 176, 32)          # final minute
OVER = (255, 77, 77)           # 00:00
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

HOLD_LAST_SECONDS = 8          # leave 00:00 on the slide instead of a blank
WARN_FROM = 60                 # amber inside the last minute

# One shared 4-colour palette keeps every frame on the same colour table,
# which is what lets the delta encoder stay small.
# Exactly four entries, not a 256-entry table padded with black: a four-colour
# table lets the encoder use 2-bit codes, which is most of the file size.
PALETTE = [BG, INK, WARN, OVER]
FLAT_PALETTE = [c for rgb in PALETTE for c in rgb]


def fitted_font(sample="00:00", width_fraction=0.82):
    """Largest size at which `sample` still fits the canvas width."""
    size = 10
    while True:
        f = ImageFont.truetype(FONT, size + 6)
        box = f.getbbox(sample)
        if (box[2] - box[0]) > W * width_fraction or size > 600:
            return ImageFont.truetype(FONT, size)
        size += 6


def frame(text, colour_index, font):
    im = Image.new("P", (W, H))
    im.putpalette(FLAT_PALETTE)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, H], fill=0)
    box = d.textbbox((0, 0), text, font=font)
    d.text(((W - (box[2] - box[0])) / 2 - box[0],
            (H - (box[3] - box[1])) / 2 - box[1]),
           text, font=font, fill=colour_index)
    return im


def build(minutes, out_dir):
    font = fitted_font()
    frames = []
    for remaining in range(minutes * 60, -1, -1):
        if remaining == 0:
            colour = 3                      # OVER
        elif remaining <= WARN_FROM:
            colour = 2                      # WARN
        else:
            colour = 1                      # INK
        frames.append(frame("%02d:%02d" % divmod(remaining, 60), colour, font))

    durations = [1000] * (len(frames) - 1) + [HOLD_LAST_SECONDS * 1000]
    out = out_dir / ("countdown-%dmin.gif" % minutes)
    # No loop argument: without a Netscape extension the countdown plays once
    # and rests on 00:00, rather than restarting behind the presenter's back.
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        disposal=1,
        optimize=False,
    )
    return out


def main(argv):
    minutes = [int(a) for a in argv[1:]] or [5, 10, 15]
    out_dir = Path(__file__).resolve().parent / "countdown-gifs"
    out_dir.mkdir(exist_ok=True)
    for m in minutes:
        path = build(m, out_dir)
        print("%-28s %6.0f KB  %d frames" % (path.name, path.stat().st_size / 1024, m * 60 + 1))


if __name__ == "__main__":
    main(sys.argv)
