"""Appearance and sound, kept out of the window code.

All of it is read from the same settings file the rest of the tool uses, and
applied both at startup and whenever a control changes, so what you see while
picking is what you get next launch. Nothing here is required for the logger to
work - it exists because a debugger you sit in front of for hours should be one
you can stand to look at, and because interface sounds are the first thing
anybody turns off.
"""

from dataclasses import replace

import vertexui as vui

# Presets, by the name stored in settings. The first is the default.
THEMES = [
    ("noir-blue", vui.theme.NOIR_BLUE),
    ("noir-red", vui.theme.NOIR_RED),
    ("slate-lime", vui.theme.SLATE_LIME),
]
THEME_NAMES = [name for name, _ in THEMES]

# Vertical space between log rows. Density is personal and it is the single
# biggest lever on how much of a session fits on screen.
SPACING = [("compact", 1.0), ("normal", 4.0), ("roomy", 8.0)]
SPACING_NAMES = [name for name, _ in SPACING]

FONT_MIN, FONT_MAX = 0.75, 1.75

DEFAULTS = {
    "theme": "noir-blue",
    "accent": "",            # empty means the preset's own accent
    "font_scale": 1.0,
    "log_mono": False,
    "log_spacing": "normal",
    "show_time": True,
    "show_tags": True,
    "sounds": True,
    "volume": 0.16,
}


def get(settings, key):
    return settings.get(key, DEFAULTS.get(key))


def hex_to_rgb(text):
    text = str(text).lstrip("#")
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16) / 255.0, int(text[2:4], 16) / 255.0,
                int(text[4:6], 16) / 255.0)
    except ValueError:
        return None


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c * 255)))) for c in rgb[:3])


def _lift(rgb, amount):
    """Toward white. The bright accent is a highlight of the accent, not a
    second colour to pick - one control is enough."""
    return tuple(c + (1.0 - c) * amount for c in rgb)


def _sink(rgb, amount):
    return tuple(c * (1.0 - amount) for c in rgb)


def theme_index(settings):
    name = str(get(settings, "theme"))
    return THEME_NAMES.index(name) if name in THEME_NAMES else 0


def spacing_index(settings):
    name = str(get(settings, "log_spacing"))
    return SPACING_NAMES.index(name) if name in SPACING_NAMES else 1


def spacing_px(settings):
    return SPACING[spacing_index(settings)][1]


def build_theme(settings):
    base = THEMES[theme_index(settings)][1]
    rgb = hex_to_rgb(get(settings, "accent"))
    if rgb is None:
        return base
    return replace(
        base,
        name="custom",
        accent=vui.theme.ImVec4(*rgb, 1.0),
        accent_bright=vui.theme.ImVec4(*_lift(rgb, 0.35), 1.0),
        accent_dim=vui.theme.ImVec4(*_sink(rgb, 0.60), 1.0),
    )


def accent_rgb(settings):
    """The colour the picker should show: the override, or the preset's own."""
    rgb = hex_to_rgb(get(settings, "accent"))
    if rgb is not None:
        return rgb
    col = THEMES[theme_index(settings)][1].accent
    return (col.x, col.y, col.z)


def apply(settings):
    """Push every appearance setting into the live UI.

    Safe to call every time a control moves; it is a handful of assignments,
    not a reload.
    """
    vui.theme.use(build_theme(settings))
    try:
        vui.theme.apply_style(vui.theme.current())
    except Exception:
        # Style application needs a live ImGui context. At import time there
        # is none, and the runner applies the style itself on first frame.
        pass
    apply_font(settings)
    apply_sound(settings)


def apply_font(settings):
    from imgui_bundle import imgui
    scale = float(get(settings, "font_scale") or 1.0)
    scale = max(FONT_MIN, min(FONT_MAX, scale))
    try:
        # ImGui 1.92 scales fonts through the style rather than io. Guarded
        # because this moved, and a missing knob should cost the setting, not
        # the window.
        imgui.get_style().font_scale_main = scale
    except Exception:
        pass


def apply_sound(settings):
    on = bool(get(settings, "sounds"))
    volume = float(get(settings, "volume") or 0.0)
    vui.sound.use(vui.sound.SoundSet(volume=volume, enabled=on and volume > 0.0))
