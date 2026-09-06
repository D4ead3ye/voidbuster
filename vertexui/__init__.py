"""VertexUI - a small dark-UI toolkit for Dear ImGui (imgui-bundle).

Four things, each usable on its own:

    theme     colour, shape and spacing tokens you swap per project
    anim      frame-rate-independent easing for immediate-mode UI
    sound     synthesised interface cues, no audio files to ship
    widgets   buttons, checkboxes, tabs that animate rather than snap
    toasts    click-through notifications drawn over another application
    fonts     real system typefaces instead of ImGui's bitmap default

Minimal app:

    from imgui_bundle import hello_imgui
    import vertexui as vui

    def gui():
        if vui.widgets.button("Go", primary=True):
            print("clicked")

    params = hello_imgui.RunnerParams()
    params.callbacks.show_gui = gui
    vui.install(params, theme=vui.theme.NOIR_RED)
    hello_imgui.run(params)

`install` wires the theme, the fonts, the sound set and the idling rule in
one call - the last of which is easy to miss and makes every animation
look broken at 9fps if you do.
"""

from . import anim, fonts, sound, theme, widgets

__all__ = ["anim", "fonts", "sound", "theme", "widgets", "toasts", "install"]

__version__ = "1.0.0"


def install(params, theme_=None, faces=None, sounds=None, idle_fps=9.0,
            active_after_input=1.2):
    """Wire the toolkit into a hello_imgui RunnerParams.

    `active_after_input` is the one that matters: a panel that idles to
    save power renders every ease at the idle rate, which looks broken.
    Staying at full rate briefly after any input keeps motion smooth while
    still costing nothing when untouched.
    """
    if theme_ is not None:
        theme.use(theme_)
    if sounds is not None:
        sound.use(sounds)

    params.callbacks.load_additional_fonts = fonts.loader(faces)
    params.callbacks.setup_imgui_style = lambda: theme.apply_style(theme.current())

    params.fps_idling.enable_idling = True
    params.fps_idling.fps_idle = idle_fps
    params.fps_idling.time_active_after_last_event = active_after_input
    return params


def end_frame(params=None):
    """Call once at the end of your gui() to let the app idle again only
    when nothing is mid-transition."""
    from imgui_bundle import hello_imgui
    try:
        p = params or hello_imgui.get_runner_params()
        p.fps_idling.enable_idling = anim.settled()
    except Exception:
        pass
