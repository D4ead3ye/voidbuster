"""Colour, shape and spacing tokens - the part you swap per project.

Everything visual is a value on a Theme, never a literal inside a widget.
That is the whole point of the split: a different project changes one
object and every button, tab, checkbox and toast follows, instead of
hunting hardcoded reds through the drawing code.

    from vertexui import theme
    theme.use(theme.NOIR_RED)          # or your own Theme(...)

A Theme carries semantic names (`accent`, `surface`, `danger`), not
descriptive ones (`red`, `dark_grey`). A palette named after what colours
ARE stops making sense the moment you swap it; named after what they DO,
it survives.
"""

from dataclasses import dataclass, field, replace

from imgui_bundle import ImVec2, ImVec4, imgui


def rgb(hex_str: str, alpha: float = 1.0) -> ImVec4:
    """`rgb("#e0243b")` - hex is how palettes are written down everywhere
    else, so accept it directly rather than making callers convert."""
    h = hex_str.lstrip("#")
    return ImVec4(int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
                  int(h[4:6], 16) / 255.0, alpha)


def with_alpha(c: ImVec4, a: float) -> ImVec4:
    return ImVec4(c.x, c.y, c.z, a)


def lerp(a: ImVec4, b: ImVec4, t: float) -> ImVec4:
    return ImVec4(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t,
                  a.z + (b.z - a.z) * t, a.w + (b.w - a.w) * t)


@dataclass
class Theme:
    """One project's look. Copy a preset and override what differs:

        MINE = replace(theme.NOIR_RED, accent=theme.rgb("#3b82f6"))
    """

    name: str = "custom"

    # surfaces, back to front
    bg: ImVec4 = field(default_factory=lambda: rgb("#08080a"))
    panel: ImVec4 = field(default_factory=lambda: rgb("#0e0e11"))
    surface: ImVec4 = field(default_factory=lambda: rgb("#16161a"))
    surface_hover: ImVec4 = field(default_factory=lambda: rgb("#212127"))
    border: ImVec4 = field(default_factory=lambda: rgb("#23232a"))

    # the one colour that carries meaning
    accent: ImVec4 = field(default_factory=lambda: rgb("#da2538"))
    accent_bright: ImVec4 = field(default_factory=lambda: rgb("#ff4355"))
    accent_dim: ImVec4 = field(default_factory=lambda: rgb("#661019"))

    text: ImVec4 = field(default_factory=lambda: rgb("#eaeaef"))
    text_dim: ImVec4 = field(default_factory=lambda: rgb("#828290"))
    text_mute: ImVec4 = field(default_factory=lambda: rgb("#53535f"))

    # status, used by toasts and anything reporting an outcome
    ok: ImVec4 = field(default_factory=lambda: rgb("#53c971"))
    warn: ImVec4 = field(default_factory=lambda: rgb("#e5aa45"))
    danger: ImVec4 = field(default_factory=lambda: rgb("#e54355"))
    info: ImVec4 = field(default_factory=lambda: rgb("#9aa3b3"))

    # shape
    rounding: float = 7.0
    rounding_panel: float = 9.0
    border_width: float = 1.0

    # spacing
    padding: ImVec2 = field(default_factory=lambda: ImVec2(16, 14))
    frame_padding: ImVec2 = field(default_factory=lambda: ImVec2(14, 8))
    item_spacing: ImVec2 = field(default_factory=lambda: ImVec2(10, 10))

    def status(self, kind: str) -> ImVec4:
        """Colour for "ok" / "warn" / "error" / "info"."""
        return {"ok": self.ok, "warn": self.warn,
                "error": self.danger, "info": self.info}.get(kind, self.info)


NOIR_RED = Theme(name="noir-red")

# Two more so the mechanism is obviously usable, not just theoretically so.
NOIR_BLUE = replace(
    NOIR_RED, name="noir-blue",
    accent=rgb("#2f7fe8"), accent_bright=rgb("#5a9dff"), accent_dim=rgb("#123258"))

SLATE_LIME = replace(
    NOIR_RED, name="slate-lime",
    bg=rgb("#0d1013"), panel=rgb("#141920"), surface=rgb("#1c232c"),
    surface_hover=rgb("#26303b"), border=rgb("#2b3641"),
    accent=rgb("#8ed11f"), accent_bright=rgb("#a7e93c"), accent_dim=rgb("#3a5610"))

_active = NOIR_RED


def use(theme: Theme):
    """Make `theme` the one widgets read from."""
    global _active
    _active = theme
    return _active


def current() -> Theme:
    return _active


def apply_style(theme: Theme = None):
    """Push the theme into ImGui's own style.

    Widgets in this toolkit draw themselves, but stock ImGui widgets -
    inputs, tables, scrollbars, popups - read the global style, and a panel
    where half the controls are themed looks worse than one where none are.
    """
    t = theme or _active
    s = imgui.get_style()

    s.window_rounding = t.rounding_panel
    s.child_rounding = t.rounding_panel
    s.frame_rounding = t.rounding
    s.popup_rounding = t.rounding_panel
    s.scrollbar_rounding = t.rounding_panel
    s.grab_rounding = t.rounding
    s.tab_rounding = t.rounding

    s.window_border_size = t.border_width
    s.child_border_size = t.border_width
    s.frame_border_size = 0.0          # borders on every frame read as a grid
    s.popup_border_size = t.border_width
    s.tab_bar_border_size = 0.0

    s.window_padding = t.padding
    s.frame_padding = t.frame_padding
    s.item_spacing = t.item_spacing
    s.item_inner_spacing = ImVec2(8, 6)
    s.cell_padding = ImVec2(10, 7)
    s.scrollbar_size = 11.0
    s.grab_min_size = 11.0

    C = imgui.Col_

    def col(name, value):
        # Enum names drift between ImGui versions; a missing one should not
        # take the whole theme down with it.
        idx = getattr(C, name, None)
        if idx is None:
            return
        try:
            s.set_color_(int(idx.value), value)
        except Exception:
            pass

    clear = ImVec4(0, 0, 0, 0)
    col("text", t.text)
    col("text_disabled", t.text_mute)
    col("window_bg", t.bg)
    col("child_bg", t.panel)
    col("popup_bg", t.panel)
    col("border", t.border)
    col("border_shadow", clear)

    col("frame_bg", t.surface)
    col("frame_bg_hovered", t.surface_hover)
    col("frame_bg_active", t.surface_hover)

    col("title_bg", t.bg)
    col("title_bg_active", t.bg)
    col("title_bg_collapsed", t.bg)
    col("menu_bar_bg", t.panel)

    col("scrollbar_bg", clear)
    col("scrollbar_grab", t.surface_hover)
    col("scrollbar_grab_hovered", lerp(t.surface_hover, t.text_mute, 0.5))
    col("scrollbar_grab_active", t.accent)

    col("check_mark", t.accent_bright)
    col("slider_grab", t.accent)
    col("slider_grab_active", t.accent_bright)

    col("button", t.surface)
    col("button_hovered", t.surface_hover)
    col("button_active", t.accent_dim)

    col("header", with_alpha(t.accent, 0.22))
    col("header_hovered", with_alpha(t.accent, 0.32))
    col("header_active", with_alpha(t.accent, 0.42))

    col("separator", t.border)
    col("separator_hovered", t.accent_dim)
    col("separator_active", t.accent)

    col("tab", clear)
    col("tab_hovered", t.surface)
    col("tab_selected", t.surface)
    col("tab_selected_overline", clear)

    col("table_header_bg", t.surface)
    col("table_border_strong", t.border)
    col("table_border_light", lerp(t.panel, t.border, 0.5))
    col("table_row_bg", clear)
    col("table_row_bg_alt", ImVec4(1, 1, 1, 0.015))

    col("resize_grip", clear)
    col("resize_grip_hovered", with_alpha(t.accent, 0.26))
    col("resize_grip_active", t.accent)
    col("nav_cursor", t.accent_bright)
