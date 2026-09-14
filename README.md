# VoidBuster

A log viewer for **any** Wii U title.

Most console log tools are a wall of scrolling text, or a dashboard built
around one game's output - a handful of regexes that match the title they were
written for and nothing else. Point one at a different game and the log still
scrolls, but the counters stay empty and nothing is tagged.

VoidBuster inverts that. Structure is derived from the text itself, so a title
nobody has ever profiled still gets tags, severity, colours and live counters
the moment it says anything. Profiles add precision on top, and are never
required.

**[Download VoidBuster.exe](../../releases/latest)** - one file, no Python, no
install. Or run it from source:

```
python voidbuster.py                      the window
python voidbuster.py --sweep              which port is this game logging on?
python voidbuster.py --quiet --stats 2    counters only, every 2s
python voidbuster.py --replay old.log     read a session back
python voidbuster.py --host 10.0.0.5 --only-host
python voidbuster.py --help
```

![live tab](docs/tab0.png)

## What it captures

| source | what it is |
|---|---|
| UDP | `WHBLogUdp` from anything built with wut, **and Aroma's logging module**, which redirects `OSReport`/`OSConsoleWrite` — that is what makes retail titles log without being rebuilt. Default 4405, broadcast. |
| TCP | the same module's stream mode, and a few standalone loggers. `--tcp PORT` |
| file | an SD capture or a previous session. `--file PATH`, or `--replay PATH` to read one from the start and exit. |
| FTP | crash dumps out of `sd:/wiiu/crash_logs`. |

Several UDP ports are bound at once, so a title logging somewhere unexpected is
caught without a restart. Datagrams that split or batch lines are reassembled;
a fragment with no newline is held briefly and then emitted, so the last line
before a freeze is not lost.

## What it derives, with no configuration

- **Tags** from `[gfx]`, `[net][sync]`, `AXFX::Init:` and `main.c:88:`
  prefixes. Each gets a stable colour hashed from its name, so the same
  subsystem is the same colour in every session.
- **Severity** from what the line says — `assertion failed` is fatal, `packet
  dropped` is a warning — not from a level field the game never wrote.
- **Counters** from every numeric shape logs actually use: `draws=340`,
  `340 draws`, and `frame 12`. Hex addresses are excluded, and a stoplist keeps
  a line of English prose from filling the table with `the` and `of`.
- **Rates and stalls.** Every tag is metered over a five second window. A tag
  that was talking and goes quiet is called out — on this console that is what
  a freeze looks like from the outside, and it is visible before anything
  crashes.

![counters](docs/tab1.png)

## Recording

Every session is written to `logs/` as lines arrive, always, with no button to
press. A hard freeze does not give you the chance to save, and the session you
forgot to save is the one you needed. A fatal line also writes the 400 lines
around it to `crash_logs/` on its own.

**Save .txt** on the Live tab writes what is on screen to `logs/saved-*.txt` —
the filtered view, not the whole buffer, with a header recording which filter
produced it. Narrowing to the one subsystem that misbehaves and saving that is
usually what you want to send to someone else.

## Reading a live log

The view eases toward the bottom rather than snapping to it, and the **speed**
picker caps how fast text is allowed to cross the screen:

| | px/s | roughly |
|---|---|---|
| calm (default) | 140 | 7 lines a second |
| steady | 260 | 13 lines a second |
| quick | 520 | 26 lines a second |
| snap | — | straight to the bottom, no glide |

The cap is the part that makes a live log readable. Easing on its own moves
faster the further behind it is, which is exactly the case where you want it
slow — a burst of two hundred lines arrives as a blur no matter how smooth the
interpolation. A ceiling in pixels per second means the text never outruns you.

The glide runs at a fixed rate per second, so it looks the same at 9fps or 60,
and it hands over the moment you touch the wheel or the scrollbar — scroll back
to the bottom and it takes hold again.

When the console is louder than the cap, the view says **behind by ~N lines**
rather than silently trailing. That is the signal to filter, pause, or raise
the speed; past a couple of screens it stops pretending and jumps.

**Pause** freezes the view at the line it was pressed on and shows how far
behind you are. Lines keep arriving, keep counting and keep being recorded;
pausing is about reading, not about dropping evidence.

**Clear log** empties the view; counters, tags and the recording carry on.
**Reset** clears everything — lines, counters, tags, alerts — and starts a new
recording file, which is what you want when you swap games rather than
restarting the tool.

## Your console

Set the console's IP once in **Setup > console**; the Crashes tab and the FTP
fetch both read it, and any address the logger has already heard from is
offered as a one-click fill.

**only accept lines from this address** is off by default — most people have
one console, and pinning the address only costs them a confusing empty window
when it changes. Turn it on when two consoles are on the network, or when
something else is broadcasting on the same port. The logger's own notices are
never filtered out, since they are what explains an otherwise empty window.

```bash
python voidbuster.py --host 192.168.1.110 --only-host
python voidbuster.py --host 192.168.1.110 --crashes
```

## Appearance and sound

**Setup > appearance**, applied as you change them and saved with everything
else — a theme you cannot see until next launch is no way to pick a theme.

- **theme** — `noir-blue`, `noir-red`, `slate-lime`, and an **accent** swatch
  that overrides the preset's own. The bright and dim variants are derived, so
  there is one colour to pick rather than three to keep consistent.
- **text size** — 0.75x to 1.75x.
- **row spacing** — compact, normal or roomy. The biggest lever on how much of
  a session fits on screen.
- **monospaced log** — timestamps and numbers line up into columns.
- **timestamps** and **tag column** — turn either off to widen the message.

**interface sounds** is a plain on/off with a volume slider next to it. Off is
off: nothing is synthesised and no audio thread starts.

## Profiles

Optional JSON in `profiles/`. `_base.json` is always applied — `OSFatal`, DSI
and ISI exceptions, allocation failures, RPL loads, Cafe result codes — and a
game profile stacks on top of it. The right one is chosen automatically by
scoring its `match` patterns against the first few hundred lines, so swapping
games generally needs no input.

```json
{
  "name": "mygame",
  "match": ["\\[mygame\\]"],
  "rules": [
    { "re": "\\[mygame\\] frame (?P<frame>\\d+) (?P<ms>\\d+)ms", "label": "frame" },
    { "re": "OUT OF ARENA", "level": "fatal", "count": "arena_oom", "alert": true }
  ],
  "tags":  { "mygame": "#5ad0ff" },
  "watch": ["frame", "ms", "arena_oom"]
}
```

Named capture groups become counters. `count` bumps a running total, `level`
overrides the guessed severity, `alert` surfaces the line in the Signals tab,
`watch` pins counters to the top of the table.

`profiles/example.json` is a working one to copy. Drop your own into the
`profiles/` folder beside the exe: a file there shadows a bundled one of the
same name, so updating VoidBuster never overwrites your edits.

## Crashes

On Aroma a crash dump is **not a file**. It is a directory named for the moment
it was written, holding 100 ring chunks of 32KB plus a 4-byte `meta.bin`. Three
things defeat a naive reader, and all three are handled here:

- **It is a directory, and the fetch has to recurse.** A flat listing that keeps
  `*.txt` finds nothing at all.
- **The ring has wrapped.** Read in filename order the log jumps backwards in
  the middle. `meta.bin` is a big-endian count of chunks written since the ring
  was created, so the newest chunk is `meta % 100` and the order follows:

  ```
  meta = 4264   →  newest = 4264 % 100 = 64,  wrapped 42 times
  order = [(meta + 1 + i) % 100 for i in range(100)]   # 65 … 99, 0 … 64
  ```

  That is preferred over reading timestamps because it does not depend on the
  content being readable: a chunk that is binary or simply has no date in it is
  silently misplaced by any content-based ordering. When `meta.bin` is missing
  VoidBuster falls back to finding the seam — the one point where the clock
  goes backwards — and says which method it used, so a guess never passes for
  a reading.
- **Records are separated by CR, not LF**, and each is prefixed with its own
  `HH;MM;SS;mmm:` clock. Splitting on newlines yields one enormous line per
  chunk and every downstream parser gives up.

```bash
python voidbuster.py --crashes 192.168.1.110          # pull dumps, recursively
python voidbuster.py --read-crash crash_logs/2026-09-13_13-13-14
```

```
Core1  Instruction  at 0x0c9c1040  safe|_localeconv_r+0x10  touching 0x00000030
  ring: 100 chunks, 39357 records, newest 64 (meta.bin), wrapped 42 times
  #0  0x0c9c1040  safe|_localeconv_r+0x10
  #1  0x0c9c31d4  safe|_svfprintf_r+0x2c
```

Dumps are parsed by shape rather than by format — registers look like
registers, stack frames look like stack frames — because crash logger output
has changed between Aroma releases and homebrew writes its own variants. Frames
the console resolved itself keep their `module|symbol+offset`; the rest can be
named from an ELF with `--elf`, which turns partial frames into `file:line` and
makes the ones the module resolver missed readable at all.

Addresses inside the RPX window (`0x02000000`–`0x10000000`) are the ones worth
handing to addr2line. Anything in the `0x1nnnnnnn` or `0xe`/`0xf` ranges is an
OS library and will not resolve — that is expected, not a failure.

## Runs

A capture left going all evening is not one log, it is eight or ten separate
experiments. The **Runs** tab splits it and gives each a verdict:

| run | start | build | lines | replayed | errors | lasted | ended |
|---|---|---|---|---|---|---|---|
| 12 | 20:41:07 | 3a2eef4 | 679 | 0 | 37 | 4m12s | crashed |
| 13 | 20:45:28 | 3a2eef4 | 998 | 34 | 48 | 6m03s | clean exit |

A run ends where a launch banner appears, or where the console goes quiet for
20s — the second rule needs no cooperation from the program being debugged.
Click a run number to filter the Live tab to it. `--runs` prints the same table
headless.

**Replayed lines are marked, not hidden.** Ports commonly dump the previous
session's breadcrumbs at startup, and by content those are indistinguishable
from live ones — same tags, same format. The tell is that their embedded clock
runs behind the run they appear in; those lines are dimmed and labelled `hist`,
and **hide replays** removes them.

**Build under test** is a field in Setup, stamped into the session header.
Automatic detection reads `Commit:` and friends when they are on screen, but a
capture started after boot has already missed them — and a log that cannot be
tied to a build is evidence about nothing in particular.

## Two clocks

Capture time and the time the game stamps on its own messages are not the same
clock, and in real logs they can be the better part of an hour apart.
VoidBuster measures the offset and shows it — `console clock +52m27s ahead of
the game's` — and the **timestamps** setting switches the column between
capture, console, or both.

## Console died, or network died?

An abruptly-ending UDP log looks identical either way, and that ambiguity is
expensive: it is the difference between reading a crash and chasing one that
never happened. When the stream goes quiet for 8s, VoidBuster asks the console
directly and writes the answer into the log next to the silence:

```
-- stream quiet 9s: console still responding - the logging stopped, not the
   console (ping ok, tcp/21 open) --
!! stream quiet 9s: console unreachable - it went down with the log
   (ping timeout, no tcp answer) !!
```

`--probe HOST` runs the same check once and exits.

## Nothing is arriving

In order, cheapest first:

0. **The Windows firewall prompt.** The first time you run it, Windows asks
   whether to allow network access. Say yes — VoidBuster only ever listens, it
   never sends to the console. Deny it and nothing arrives, with no other
   symptom. If you missed the prompt, it is under Windows Defender Firewall >
   Allow an app.
1. **Self-test** (Setup tab, or `--self-test`) sends a datagram to our own
   listener. If that line does not appear, the problem is this end — almost
   always the firewall above. If it does appear, the problem is on the
   console.
2. **Port sweep** (`--sweep`) binds every likely port for eight seconds. Start
   the game while it runs. A title logging on a port nobody expected shows up
   here.
3. **Retail titles log nothing on their own.** They need Aroma's logging module
   in `sd:/wiiu/environments/aroma/modules/` — that is the piece that redirects
   `OSReport`. Without it there is no traffic to receive, and no logger can fix
   that from this side.
4. **Homebrew you build yourself** needs `WHBLogUdpInit()` called early, before
   the first `WHBLogPrintf`.
5. Console and PC on the same subnet.
6. **After a hard freeze there is often nothing at all** — the console stopped
   before it could send. Fetch the crash dump instead.

## Building the exe

```bash
pip install pyinstaller
pyinstaller VoidBuster.spec
```

One file in `dist/`. It is a console application on purpose — the headless
viewer is half the tool — and the window hides that console itself when the exe
is double-clicked, while leaving your terminal alone when you run it from one.

Sessions, dumps, settings and any profiles you add live **beside the exe**, not
inside it. A one-file build unpacks to a temporary folder that Windows deletes
on exit, so anything written relative to the code would vanish with it — which
for a logger means losing the session you were recording. See
[voidbuster/paths.py](voidbuster/paths.py).

## Layout

```
voidbuster.py          entry point: no arguments opens the window
voidbuster/parse.py    tags, severity, counters, colours - all configuration-free
voidbuster/sources.py  UDP/TCP/file capture, line reassembly, the port sweep
voidbuster/rules.py    profiles: load, score, combine, hot-reload
voidbuster/session.py  buffer, counters, rates, stalls, recording, snapshots
voidbuster/crash.py    FTP fetch, dump parsing, addr2line
voidbuster/look.py     themes, text size, density, sound - all persisted
voidbuster/paths.py    beside-the-exe vs inside-the-build, which a frozen app must split
voidbuster/syslog.py   Aroma ring dumps: find, order, split into records
voidbuster/launches.py runs, verdicts, replay detection, clock skew
voidbuster/liveness.py console-died vs network-died
voidbuster/cli.py      headless
voidbuster/gui.py      the window (VertexUI + Dear ImGui)
profiles/           _base.json is always on; the rest are per game
```

Needs `imgui-bundle` for the window:

```bash
pip install imgui-bundle
```

The headless viewer needs nothing beyond the standard library, which is
deliberate: it has to work on a machine where you have not installed anything.

`vertexui/` is vendored rather than depended on - it is a small dark-UI toolkit
of mine that is not published separately, and a debugger you reach for at 2am
should not need a second install to open.

## Licence

MIT. See [LICENSE](LICENSE).
