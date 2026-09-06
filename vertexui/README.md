# VertexUI

A small dark-UI toolkit for [Dear ImGui](https://github.com/pthom/imgui_bundle)
(imgui-bundle), pulled out of a tool that needed to sit next to a game all
night without being ugly or noisy.

Six pieces, each usable on its own:

| module | what it is |
|---|---|
| `theme` | colour, shape and spacing tokens you swap per project |
| `anim` | frame-rate-independent easing for immediate-mode UI |
| `sound` | synthesised interface cues — no audio files to ship |
| `fonts` | real system typefaces instead of ImGui's bitmap default |
| `widgets` | buttons, checkboxes, tabs that animate rather than snap |
| `toasts` | click-through notifications drawn *over another application* |

Try it: `python -m vertexui.demo`

## Minimal app

```python
from imgui_bundle import hello_imgui
import vertexui as vui

state = {"on": False, "tab": 0}

def gui():
    state["tab"] = vui.widgets.tabs("main", ["Home", "Settings"], state["tab"])
    if vui.widgets.button("Start", primary=True):
        state["on"] = True
    state["on"] = vui.widgets.checkbox("Enabled", state["on"])
    vui.end_frame()          # lets the app idle once motion has settled

params = hello_imgui.RunnerParams()
params.callbacks.show_gui = gui
vui.install(params, theme_=vui.theme.NOIR_RED)
hello_imgui.run(params)
```

`install()` wires the theme, fonts, sounds and the idling rule in one call.
That last one is easy to miss: an app that idles to save power renders
every ease at the idle frame rate, and every animation looks broken.

## Theming

Tokens are semantic (`accent`, `surface`, `danger`), never descriptive
(`red`, `dark_grey`) — a palette named after what colours *are* stops
making sense the moment you swap it.

```python
from dataclasses import replace
MINE = replace(vui.theme.NOIR_RED, accent=vui.theme.rgb("#7c3aed"))
vui.theme.use(MINE)
```

Presets: `NOIR_RED`, `NOIR_BLUE`, `SLATE_LIME`. `theme.apply_style()` also
pushes the palette into ImGui's own style, so stock widgets (inputs,
tables, scrollbars) match the drawn ones.

## Animation

```python
hot = vui.anim.to(f"btn:{label}", 1.0 if hovered else 0.0, speed=16)
```

The chase is exponential on the frame delta, so it takes the same
wall-clock time at 30fps and 144fps. `anim.settled()` reports whether
anything is mid-transition, which is what `end_frame()` uses to decide
when idling is safe.

Also: `ease_out`, `ease_in_out`, `pulse()` for "this is alive" indicators.

## Sound

Cues are generated as WAV bytes on first use — nothing to ship, nothing to
lose track of in a frozen exe.

```python
vui.sound.use(vui.sound.SoundSet(volume=0.16))
vui.sound.play("ok")
```

The default set is deliberately dull and low. Three things learned the
hard way while tuning it:

* **pitch, not volume, is what makes a cue feel sharp.** Quiet high beeps
  still cut through; the whole set sits low and is low-passed at 900 Hz.
* **a falling pitch reads as a physical knock.** The same tone held flat
  reads as an alert.
* **fade both ends.** A waveform starting or stopping at non-zero
  amplitude clicks, and that click is most of what "harsh" means.

Swap the set with `SoundSet(specs=sound.CRISP_SPECS)` or your own dict.

## Toasts

A separate always-on-top window that never takes focus and never receives
a click, so it can report over a fullscreen game.

```python
tray = vui.toasts.Toasts(anchor_titles=["My Game"], on_status=print)
tray.start()
tray.notify("ok", "Connected")
```

Kinds: `ok`, `warn`, `error`, `info`. Repeats within a couple of seconds
collapse into one with a counter. It has no ImGui dependency and runs
happily in a process with no GUI at all.

Two traps worth knowing, both of which fail *silently*:

* the window **must** pump its message queue. One that doesn't is a hung
  window as far as Windows is concerned — and `GetWindowText` sends
  `WM_GETTEXT` to windows in the same process, so the owning app can
  deadlock enumerating windows against its own overlay.
* `UpdateLayeredWindow` wants **premultiplied alpha**, or the boxes wash
  out pale instead of reading as translucent.

`exclude_from_capture=True` (the default) hides the window from screen
capture, so an app that reads the screen never photographs its own
notifications. On a few graphics configurations that flag stops the window
drawing entirely — hence the switch, and `on_status` to report which.

## Requirements

`imgui-bundle`, `numpy`, `opencv-python` (toasts only), Windows for
`toasts` and `sound`. The rest is cross-platform.
