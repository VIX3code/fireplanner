#!/usr/bin/env python3
"""Render fixed-length countdown GIFs for dropping onto a slide.

A GIF is the only countdown that survives inside PowerPoint with no network
and no add-in. The trade is that it is fixed: it starts when the slide
appears and it cannot be paused or extended. Use it for the segments you
control (a table exercise you called, a build-up to the start of a session);
use the browser timer for anything a question might stretch.

    python3 tools/make_countdown_gifs.py                # 5, 10, 15 min
    python3 tools/make_countdown_gifs.py 3 7 20         # any minutes you like

Both a dark and a light version of each length are written, because a slide
template is either one or the other and a black box on a white slide reads as
a mistake.

Frames are emitted one per second and Pillow stores only the pixels that
changed between them, so a 15-minute countdown is a few hundred kilobytes
rather than a few hundred megabytes.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1000, 420
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

HOLD_LAST_SECONDS = 8          # leave 00:00 on the slide instead of a blank
WARN_FROM = 60                 # the last minute changes colour

# Four colours per theme: background, counting down, final minute, zero.
# The light amber and red are darker than their dark-theme counterparts —
# on white they have to survive a washed-out projector lamp, where a bright
# amber turns into pale nothing.
THEMES = {
    "":       [(10, 10, 11),    (244, 244, 245), (255, 176, 32), (255, 77, 77)],
    "-light": [(255, 255, 255), (20, 20, 22),    (186, 98, 5),   (192, 28, 28)],
}

# GIF has one transparent index and no per-pixel alpha, and a transparent
# pixel in a delta frame means "keep whatever was underneath" — which would
# leave every previous digit ghosting under the current one. So the panel is
# opaque, and the light theme is plain white to sit flush on a white slide.


def fitted_font(sample="00:00", width_fraction=0.82):
    """Largest size at which `sample` still fits the canvas width."""
    size = 10
    while True:
        f = ImageFont.truetype(FONT, size + 6)
        box = f.getbbox(sample)
        if (box[2] - box[0]) > W * width_fraction or size > 600:
            return ImageFont.truetype(FONT, size)
        size += 6


def frame(text, colour_index, font, flat_palette):
    im = Image.new("P", (W, H))
    im.putpalette(flat_palette)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, H], fill=0)
    box = d.textbbox((0, 0), text, font=font)
    d.text(((W - (box[2] - box[0])) / 2 - box[0],
            (H - (box[3] - box[1])) / 2 - box[1]),
           text, font=font, fill=colour_index)
    return im


def build(minutes, suffix, out_dir):
    # Exactly four entries, not a 256-entry table padded with black: a
    # four-colour table lets the encoder use 2-bit codes, which is most of
    # the file size.
    flat = [c for rgb in THEMES[suffix] for c in rgb]
    font = fitted_font()

    frames = []
    for remaining in range(minutes * 60, -1, -1):
        colour = 3 if remaining == 0 else 2 if remaining <= WARN_FROM else 1
        frames.append(frame("%02d:%02d" % divmod(remaining, 60), colour, font, flat))

    durations = [1000] * (len(frames) - 1) + [HOLD_LAST_SECONDS * 1000]
    out = out_dir / ("countdown-%dmin%s.gif" % (minutes, suffix))
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
        for suffix in THEMES:
            path = build(m, suffix, out_dir)
            print("%-30s %6.0f KB  %d frames" % (path.name, path.stat().st_size / 1024, m * 60 + 1))


if __name__ == "__main__":
    main(sys.argv)
