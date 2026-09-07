# tools

Standalone utilities that ride along with this repo but are not part of the
trading system.

## presentation-timer.html

A countdown timer for use while presenting. Single file, no build step, no
network: open it in any browser (double-click, or drag it onto a browser
window) and it works on a plane, in a client boardroom, or on a locked-down
machine. Nothing is fetched — the fonts are the ones already on the OS and
the end chime is synthesised in the browser rather than loaded from a file.

**Two modes**

- *Duration* — 1 / 2 / 5 / 10 / 15 / 30 minute presets, or any minutes:seconds.
- *End at time* — you type the wall-clock time the room reconvenes ("14:45")
  and it counts down to it. Better than a duration for breaks, because the
  number on screen is the promise you actually made.

**While it runs**

- The screen shows the label you set ("Coffee break", "Q&A"), the digits, and
  the clock time it ends at, so nobody has to do arithmetic.
- Amber under a minute; red and counting *up* past zero, so an overrun is
  visible rather than silently ended.
- `+1 min` / `−1 min` extend or cut it mid-run without losing the count.
- Controls fade out after four seconds and the digits take the whole screen;
  any mouse move brings them back.
- The clock is read from the system clock every tick, so it stays accurate
  even if the tab is in the background or the laptop screen sleeps.

**Keys** — `Space` start/pause · `R` reset · `F` fullscreen · `↑`/`↓` ±1 min ·
`M` mute · `H` hide controls.

**With PowerPoint.** Run it on the second monitor or on alt-tab rather than
inside the deck. PowerPoint has no native countdown — Presenter View's clock
counts up, and the only genuinely offline in-slide options are an animated GIF
or an inserted video, both of which are fixed-length and cannot be paused or
extended once a client question runs long.
