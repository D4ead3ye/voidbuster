"""Frame-to-frame motion for an immediate-mode UI.

ImGui rebuilds every frame and keeps nothing, so anything that moves has
to store its own value somewhere. This is that store: one dict of named
values, each chasing a target.

    hot = anim.to(f"btn:{label}", 1.0 if hovered else 0.0)

Two rules that matter more than they look:

  * the chase is exponential on the frame delta, not a fixed step per
    frame, so it takes the same wall-clock time at 30fps and at 144fps
  * anything mid-transition is remembered, so an app that idles to save
    power can be told to keep rendering only while something is moving
"""

import math

from imgui_bundle import ImVec4, imgui

_values = {}
_busy = False


def to(key: str, target: float, speed: float = 14.0) -> float:
    """Ease the value named `key` toward `target` and return it.

    `speed` is roughly "how fast", not a duration: 8 is leisurely, 14 is
    the default for hover, 20+ is nearly instant but still not a jump.
    """
    global _busy
    dt = min(0.05, max(0.0001, imgui.get_io().delta_time))
    cur = _values.get(key, target)
    cur += (target - cur) * (1.0 - math.exp(-speed * dt))
    if abs(target - cur) < 0.001:
        cur = target
    else:
        _busy = True
    _values[key] = cur
    return cur


def mark_busy():
    """Declare motion that `to()` cannot see - anything driven by the clock
    rather than by a value approaching a target, such as a pulse."""
    global _busy
    _busy = True


def settled() -> bool:
    """True when nothing moved this frame. Reading it clears the flag, so
    call it once per frame:

        params.fps_idling.enable_idling = anim.settled()
    """
    global _busy
    was, _busy = _busy, False
    return not was


def forget(prefix: str = ""):
    """Drop stored values. Without a prefix, drops everything - useful when
    a panel is rebuilt and old keys would otherwise leak."""
    if not prefix:
        _values.clear()
        return
    for k in [k for k in _values if k.startswith(prefix)]:
        del _values[k]


# --- easing ------------------------------------------------------------
# For animations driven by an explicit 0..1 progress rather than by chase.

def ease_out(t: float) -> float:
    """Fast away, gentle at rest. The default for something arriving."""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def ease_in_out(t: float) -> float:
    """Gentle at both ends - for something that both arrives and leaves."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def pulse(period: float = 2.6, floor: float = 0.0) -> float:
    """A 0..1 breath on the wall clock, for 'this is alive' indicators.
    Marks the frame busy for you, since nothing else can tell it is
    moving."""
    import time
    mark_busy()
    v = 0.5 + 0.5 * math.sin(time.time() * (2.0 * math.pi / period))
    return floor + (1.0 - floor) * v


def lerp_col(a: ImVec4, b: ImVec4, t: float) -> ImVec4:
    return ImVec4(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t,
                  a.z + (b.z - a.z) * t, a.w + (b.w - a.w) * t)
