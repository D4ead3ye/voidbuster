"""A runnable tour of the toolkit.

    python -m vertexui.demo

Every widget, all three presets, a live theme swap, and the notification
tray - so the package can be checked in isolation before being wired into
anything.
"""

from dataclasses import replace

from imgui_bundle import ImVec2, hello_imgui, imgui

import vertexui as vui
from vertexui import anim, fonts, sound, theme, widgets

PRESETS = [theme.NOIR_RED, theme.NOIR_BLUE, theme.SLATE_LIME]


class Demo:
    def __init__(self):
        self.tab = 0
        self.preset = 0
        self.running = False
        self.checks = {"Auto-scroll": True, "Plain english": True, "Disabled": False}
        self.tray = None
        self.log = [f"{i:02d}:00:0{i%10} demo line {i} - value {i*7}" for i in range(12)]

    # -- sections -------------------------------------------------------
    def header(self):
        dl = imgui.get_window_draw_list()
        o = imgui.get_cursor_screen_pos()
        avail = imgui.get_content_region_avail().x
        t = theme.current()
        dl.add_rect_filled(ImVec2(o.x, o.y + 6), ImVec2(o.x + 3.5, o.y + 34),
                           imgui.get_color_u32(t.accent), 2.0)
        with fonts.use("title"):
            dl.add_text(ImVec2(o.x + 14, o.y + 8), imgui.get_color_u32(t.text),
                        "VERTEXUI")
            w = imgui.calc_text_size("VERTEXUI").x
        dl.add_text(ImVec2(o.x + 24 + w, o.y + 14),
                    imgui.get_color_u32(t.text_mute), f"v{vui.__version__}")
        imgui.dummy(ImVec2(avail, 40))

        imgui.set_cursor_screen_pos(ImVec2(o.x + 24 + w + 60, o.y + 9))
        widgets.status_pill(on=self.running)
        imgui.set_cursor_screen_pos(ImVec2(o.x, o.y + 44))
        widgets.activity_rule(avail, self.running)

    def controls(self):
        if widgets.button("Start", 120, primary=True, enabled=not self.running):
            self.running = True
            self.notify("ok", "Started")
        imgui.same_line()
        if widgets.button("Stop", 110, enabled=self.running):
            self.running = False
            self.notify("info", "Stopped")
        imgui.same_line()
        if widgets.button("Warn", 110):
            self.notify("warn", "Something needs a look")
        imgui.same_line()
        if widgets.button("Error", 110):
            self.notify("error", "Something went wrong")
        imgui.same_line()
        if widgets.button("Disabled", 120, enabled=False):
            pass

        imgui.dummy(ImVec2(0, 4))
        for label in list(self.checks):
            self.checks[label] = widgets.checkbox(label, self.checks[label])
            imgui.same_line()
        imgui.new_line()

    def theming(self):
        t = theme.current()
        imgui.text_colored(t.text_dim,
                           "Every colour is a token on a Theme. Swap the object "
                           "and every widget follows.")
        imgui.dummy(ImVec2(0, 6))
        for i, p in enumerate(PRESETS):
            if widgets.button(p.name, 150, primary=(i == self.preset)):
                self.preset = i
                theme.use(p)
                theme.apply_style(p)
                self.notify("info", f"Theme: {p.name}")
            imgui.same_line()
        imgui.new_line()
        imgui.dummy(ImVec2(0, 8))

        imgui.text_colored(t.text_dim, "Status colours:")
        dl = imgui.get_window_draw_list()
        o = imgui.get_cursor_screen_pos()
        for i, kind in enumerate(("ok", "warn", "error", "info")):
            x = o.x + i * 92
            dl.add_rect_filled(ImVec2(x, o.y), ImVec2(x + 84, o.y + 26),
                               imgui.get_color_u32(t.status(kind)), 6.0)
            dl.add_text(ImVec2(x + 10, o.y + 5),
                        imgui.get_color_u32(ImVec4_black()), kind)
        imgui.dummy(ImVec2(0, 34))

    def sounds(self):
        t = theme.current()
        imgui.text_colored(t.text_dim,
                           "Cues are synthesised at runtime - nothing to ship. "
                           "The set is data, so a project can swap it.")
        imgui.dummy(ImVec2(0, 6))
        for name in ("hover", "click", "ok", "warn", "error"):
            if widgets.button(name, 110):
                sound.current()._last = 0.0
                sound.play(name)
            imgui.same_line()
        imgui.new_line()

    def logpane(self):
        t = theme.current()
        imgui.text_colored(t.text_dim, "LOG")
        if imgui.begin_child("log", ImVec2(0, 0), imgui.ChildFlags_.borders):
            with fonts.use("mono"):
                for line in self.log:
                    imgui.text_unformatted(line)
        imgui.end_child()

    # -- plumbing -------------------------------------------------------
    def notify(self, kind, text):
        if self.tray is not None:
            self.tray.notify(kind, text)

    def gui(self):
        self.header()
        self.tab = widgets.tabs("demo", ["Controls", "Theme", "Sound", "Log"], self.tab)
        (self.controls, self.theming, self.sounds, self.logpane)[self.tab]()
        vui.end_frame()

    def run(self):
        from vertexui import toasts
        self.tray = toasts.Toasts(seconds=3.5, on_status=print)
        self.tray.start()

        params = hello_imgui.RunnerParams()
        params.app_window_params.window_title = "VertexUI demo"
        params.app_window_params.window_geometry.size = (900, 600)
        params.imgui_window_params.default_imgui_window_type = (
            hello_imgui.DefaultImGuiWindowType.provide_full_screen_window)
        params.callbacks.show_gui = self.gui
        vui.install(params, theme_=PRESETS[0])
        hello_imgui.run(params)
        self.tray.stop()


def ImVec4_black():
    from imgui_bundle import ImVec4
    return ImVec4(0.06, 0.06, 0.07, 1.0)


if __name__ == "__main__":
    Demo().run()
