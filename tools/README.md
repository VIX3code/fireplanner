# tools

Standalone utilities that ride along with this repo but are not part of the
trading system.

## presentation-timer.html

A countdown timer for running workshop sessions — table discussions, breaks,
small-group activities, anything where the room needs to see how long is left.

Single file, no build step, no network: open it in any browser (double-click,
or drag it onto a browser window) and it works in a hall with no wifi, on a
borrowed laptop, on whatever machine happens to be plugged into the projector.
Nothing is fetched — the fonts are the ones already on the OS and the end
chime is synthesised in the browser rather than loaded from a file. Copy it to
a USB stick and it works from there.

**Two modes**

- *Duration* — 1 / 2 / 5 / 10 / 15 / 30 minute presets, or any minutes:seconds.
- *End at time* — you type the wall-clock time the room reconvenes ("11:15")
  and it counts down to it. Better than a duration for breaks, because the
  number on screen is the promise you actually made.

**While it runs**

- The screen shows the label you set ("Table discussion", "Break", "Prayer"),
  the digits, and the clock time it ends at, so nobody has to do arithmetic.
- Amber under a minute; red and counting *up* past zero, so an overrun is
  visible rather than silently ended.
- `+1 min` / `−1 min` extend or cut it mid-run without losing the count —
  which is most of the point, because a discussion that is finally going well
  is worth three more minutes.
- Controls fade out after four seconds and the digits take the whole screen;
  any mouse move brings them back.
- The clock is read from the system clock every tick, so it stays accurate
  even if the tab is in the background or the laptop screen sleeps.

**Keys** — `Space` start/pause · `R` reset · `F` fullscreen · `↑`/`↓` ±1 min ·
`M` mute · `H` hide controls.

Worth teaching whoever runs the laptop: `Space` and the arrow keys are the
whole job. They do not need to find the mouse.

**One projector, mirrored.** This is the normal church setup, and it is the
thing to plan around. Alt-tabbing to the timer puts your desktop on the wall
in front of everyone, so either commit to the switch (press `F` first, so what
lands on screen is only the timer) or keep the timer off the projector
entirely and use a GIF on the slide for the parts the room needs to see. If
the hall has a spare screen or a second HDMI, extending rather than mirroring
solves it outright and is worth the ten minutes with the AV volunteer.

**The chime will not carry.** A laptop speaker loses to sixty people talking
at round tables. Treat the sound as a cue for you, not for the room, and let
the red digits do the announcing — or run the laptop audio through the PA if
it is already patched in.

**With PowerPoint.** PowerPoint has no countdown of its own. Presenter View's
clock counts up, Rehearse Timings is a recording mode, and the only genuinely
offline in-slide options are an animated GIF or an inserted video — both
fixed-length and unpausable. So: this timer for anything that might stretch, a
GIF for the parts you control.

## countdown-gifs/

Six files: 5, 10 and 15 minutes, each in a dark and a light version, for the
case where the timer really does have to live on the slide — which on a single
mirrored projector is often the honest answer.

| | Slide template |
| --- | --- |
| `countdown-Nmin.gif` | dark, or a photo background |
| `countdown-Nmin-light.gif` | white or near-white |

Both are 1000x420 with digits that turn amber for the final minute and red at
zero. The light version's amber and red are darker than the dark version's:
a bright amber that reads well on black turns to pale nothing on white under
a tired projector lamp.

The panel is opaque rather than transparent, so on an off-white or textured
template you will see its edge — the dark version is usually the better
answer there. GIF has a single transparent index and no per-pixel alpha, and
a transparent pixel in a delta frame means "keep what was underneath", which
would leave every previous digit ghosting under the current one.

No loop block is written, so each one plays once and rests on 00:00 rather
than silently restarting behind you.

Insert with **Insert > Pictures > This Device**. The animation runs in Slide
Show view only (it sits still while you edit), and it starts the moment the
slide appears — there is no play, pause, or reset. Advancing to the next slide
is the only way to end one early.

That makes a GIF right for a fixed activity — a ten-minute table exercise you
called, a countdown into the start of a session — and wrong for anything you
might want to extend. Put the GIF on the activity's own slide, with the
question or instruction beside it, so the room has the task and the clock in
one place.

Two caveats before relying on one in front of a room. PowerPoint's GIF
playback is not frame-accurate and can drift a second or two over fifteen
minutes — close enough for a workshop, not for anything formally timed. And
the GIF is silent.

Regenerate, or make other lengths, with (both themes are written each time):

    pip install Pillow
    python3 tools/make_countdown_gifs.py 3 7 20
