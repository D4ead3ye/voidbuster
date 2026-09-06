"""Controls that animate, drawn rather than styled.

ImGui's own widgets take their geometry and colour from the global style,
which is tuned for one shape of control. Restyling them gets you a themed
panel that still snaps between states; these draw themselves, so hover,
press and selection can each ease independently.

Every widget reads the active theme and plays from the active sound set,
so a project sets those once and never passes them again:

    theme.use(theme.NOIR_BLUE)
    if widgets.button("Start", primary=True):
        ...
"""

from imgui_bundle import ImVec2, ImVec4, imgui

from . import anim, fonts, sound
from . import theme as theme_mod

_hover_was = {}


def hover_tick(key: str, hovered: bool):
    """Play the hover cue on the frame the cursor arrives, and not again
    until it leaves. Without the edge check it fires every frame."""
    was = _hover_was.get(key, False)
    if hovered and not was:
        sound.play("hover")
    _hover_was[key] = bool(hovered)


def button(label, width=0.0, height=36.0, primary=False, enabled=True,
           tooltip=None, key=None) -> bool:
    """A button whose fill eases, that sinks when pressed, and that wipes an
    accent underline in from the left on hover."""
    t = theme_mod.current()
    dl = imgui.get_window_draw_list()
    key = key or label
    if width <= 0:
        width = imgui.calc_text_size(label).x + 34
    pos = imgui.get_cursor_screen_pos()

    if not enabled:
        imgui.begin_disabled()
    imgui.invisible_button(f"##{key}", ImVec2(width, height))
    clicked = imgui.is_item_clicked() if enabled else False
    hovered = imgui.is_item_hovered() and enabled
    held = imgui.is_item_active() and enabled
    if not enabled:
        imgui.end_disabled()

    hover_tick(f"btn:{key}", hovered)
    hot = anim.to(f"btn:{key}", 1.0 if hovered else 0.0, 16.0)
    press = anim.to(f"btnp:{key}", 1.0 if held else 0.0, 26.0)

    base = t.accent_dim if primary else t.surface
    top = t.accent if primary else t.surface_hover
    fill = anim.lerp_col(base, top, hot)
    if not enabled:
        fill = ImVec4(fill.x * 0.5, fill.y * 0.5, fill.z * 0.5, 0.6)

    inset = 1.0 * press           # sinks rather than jumping to a third colour
    p0 = ImVec2(pos.x, pos.y + inset)
    p1 = ImVec2(pos.x + width, pos.y + height - inset)

    dl.add_rect_filled(p0, p1, imgui.get_color_u32(fill), t.rounding)
    if hot > 0.01:
        dl.add_rect(p0, p1, imgui.get_color_u32(
            theme_mod.with_alpha(t.accent, 0.55 * hot)), t.rounding, 1.0)
        uw = (width - 16) * hot
        dl.add_rect_filled(ImVec2(pos.x + 8, p1.y - 2.5),
                           ImVec2(pos.x + 8 + uw, p1.y - 1.0),
                           imgui.get_color_u32(t.accent_bright), 1.0)

    ts = imgui.calc_text_size(label)
    col = t.text if enabled else t.text_mute
    with fonts.use("semi"):
        dl.add_text(ImVec2(pos.x + (width - ts.x) * 0.5,
                           pos.y + (height - ts.y) * 0.5 + inset),
                    imgui.get_color_u32(anim.lerp_col(col, t.text, hot)), label)

    if tooltip and hovered:
        imgui.set_tooltip(tooltip)
    if clicked:
        sound.play("click")
    return clicked


def checkbox(label, value: bool, box=18.0, key=None) -> bool:
    """Three things ease independently: the hover lift, the fill as it turns
    on, and the tick drawing itself in rather than appearing whole."""
    t = theme_mod.current()
    dl = imgui.get_window_draw_list()
    key = key or label
    pos = imgui.get_cursor_screen_pos()
    with fonts.use("ui"):
        tw = imgui.calc_text_size(label).x
    gap = 9.0
    line_h = max(box, imgui.get_text_line_height())

    imgui.invisible_button(f"##chk{key}", ImVec2(box + gap + tw, line_h))
    if imgui.is_item_clicked():
        value = not value
        sound.play("click")

    hovered = imgui.is_item_hovered()
    hover_tick(f"chk:{key}", hovered)
    hot = anim.to(f"chk:{key}", 1.0 if hovered else 0.0, 15.0)
    on = anim.to(f"chkon:{key}", 1.0 if value else 0.0, 16.0)

    y0 = pos.y + (line_h - box) * 0.5
    p0, p1 = ImVec2(pos.x, y0), ImVec2(pos.x + box, y0 + box)
    rest = anim.lerp_col(t.surface, t.surface_hover, hot)
    # Checked matches the primary button - deep at rest, brighter under the
    # cursor. Full accent at rest makes a minor control shout.
    fill = anim.lerp_col(rest, anim.lerp_col(t.accent_dim, t.accent, hot), on)
    dl.add_rect_filled(p0, p1, imgui.get_color_u32(fill), 5.0)
    edge = anim.lerp_col(anim.lerp_col(t.border, t.text_mute, hot),
                         anim.lerp_col(t.accent, t.accent_bright, hot), on)
    dl.add_rect(p0, p1, imgui.get_color_u32(edge), 5.0, 1.0)

    if on > 0.02:
        cx, cy = pos.x + box * 0.5, y0 + box * 0.5
        a = ImVec2(cx - box * 0.22, cy + box * 0.02)
        b = ImVec2(cx - box * 0.06, cy + box * 0.18)
        c = ImVec2(cx + box * 0.24, cy - box * 0.18)
        ink = imgui.get_color_u32(ImVec4(1, 1, 1, min(1.0, on * 1.4)))
        t1 = min(1.0, on / 0.45)
        dl.add_line(a, ImVec2(a.x + (b.x - a.x) * t1, a.y + (b.y - a.y) * t1), ink, 2.2)
        if on > 0.45:
            t2 = (on - 0.45) / 0.55
            dl.add_line(b, ImVec2(b.x + (c.x - b.x) * t2, b.y + (c.y - b.y) * t2), ink, 2.2)

    col = anim.lerp_col(anim.lerp_col(t.text_dim, t.text, hot), t.text, on)
    ts = imgui.calc_text_size(label)
    dl.add_text(ImVec2(pos.x + box + gap, pos.y + (line_h - ts.y) * 0.5),
                imgui.get_color_u32(col), label)
    return value


def tabs(key: str, labels, current: int) -> int:
    """Tab strip whose indicator slides between tabs.

    Built from invisible buttons rather than ImGui's tab bar because the
    indicator has to interpolate between positions, which the built-in bar
    has no concept of - it can only be on one tab or another.
    """
    t = theme_mod.current()
    dl = imgui.get_window_draw_list()
    origin = imgui.get_cursor_screen_pos()
    h, pad = 34.0, 18.0

    widths, xs, x = [], [], origin.x
    with fonts.use("semi"):
        for lbl in labels:
            w = imgui.calc_text_size(lbl).x + pad * 2
            widths.append(w)
            xs.append(x)
            x += w + 4

    selected = current
    for i, lbl in enumerate(labels):
        imgui.set_cursor_screen_pos(ImVec2(xs[i], origin.y))
        imgui.invisible_button(f"##tab{key}{i}", ImVec2(widths[i], h))
        if imgui.is_item_clicked():
            selected = i
            sound.play("click")
        hovered = imgui.is_item_hovered()
        hover_tick(f"tab:{key}:{i}", hovered)
        hot = anim.to(f"tab:{key}:{i}", 1.0 if hovered else 0.0, 16.0)
        on = i == current
        if hot > 0.01 and not on:
            dl.add_rect_filled(ImVec2(xs[i], origin.y),
                               ImVec2(xs[i] + widths[i], origin.y + h),
                               imgui.get_color_u32(ImVec4(1, 1, 1, 0.045 * hot)),
                               t.rounding)
        ts = imgui.calc_text_size(lbl)
        col = anim.lerp_col(t.text_dim, t.text, max(hot, 1.0 if on else 0.0))
        with fonts.use("semi"):
            dl.add_text(ImVec2(xs[i] + (widths[i] - ts.x) * 0.5,
                               origin.y + (h - ts.y) * 0.5),
                        imgui.get_color_u32(col), lbl)

    # The indicator chases both x and width, so switching reads as one
    # object moving rather than two appearing.
    ix = anim.to(f"tabx:{key}", xs[current], 18.0)
    iw = anim.to(f"tabw:{key}", widths[current], 18.0)
    dl.add_rect_filled(ImVec2(ix + 10, origin.y + h - 3),
                       ImVec2(ix + iw - 10, origin.y + h - 0.5),
                       imgui.get_color_u32(t.accent), 2.0)

    imgui.set_cursor_screen_pos(ImVec2(origin.x, origin.y + h + 8))
    return selected


def status_pill(label_off="IDLE", label_on="RUNNING", on=False, key="status"):
    """A pill that breathes while active, and crossfades between its two
    words instead of swapping them in one frame. Returns its width."""
    t = theme_mod.current()
    dl = imgui.get_window_draw_list()
    pos = imgui.get_cursor_screen_pos()
    cy = pos.y + 11.0

    run = anim.to(f"pill:{key}", 1.0 if on else 0.0, 9.0)
    beat = anim.pulse() if on else 0.0

    with fonts.use("semi"):
        w_off = imgui.calc_text_size(label_off).x
        w_on = imgui.calc_text_size(label_on).x
    width = (w_off + (w_on - w_off) * run) + 34

    live = anim.lerp_col(t.ok, theme_mod.with_alpha(t.ok, 0.35), beat)
    dot = anim.lerp_col(t.text_mute, live, run)

    dl.add_rect_filled(ImVec2(pos.x, cy - 11), ImVec2(pos.x + width, cy + 11),
                       imgui.get_color_u32(anim.lerp_col(
                           t.surface, theme_mod.with_alpha(t.ok, 0.12), run)), 11.0)
    dl.add_circle_filled(ImVec2(pos.x + 13, cy), 4.0, imgui.get_color_u32(dot))
    if run > 0.01:
        dl.add_circle(ImVec2(pos.x + 13, cy), 4.0 + 4.0 * beat,
                      imgui.get_color_u32(theme_mod.with_alpha(
                          t.ok, 0.5 * (1.0 - beat) * run)), 0, 1.5)

    label = label_on if run > 0.5 else label_off
    fade = abs(run - 0.5) * 2.0
    with fonts.use("semi"):
        dl.add_text(ImVec2(pos.x + 24, cy - imgui.get_font_size() * 0.5),
                    imgui.get_color_u32(theme_mod.with_alpha(dot, max(0.15, fade))),
                    label)
    imgui.dummy(ImVec2(width, 22))
    return width


def activity_rule(width: float, active: bool, key="rule", thickness=1.6):
    """A hairline that sweeps an accent segment across as something starts,
    and retracts when it stops. Readable from the corner of the eye."""
    t = theme_mod.current()
    dl = imgui.get_window_draw_list()
    pos = imgui.get_cursor_screen_pos()
    run = anim.to(f"rule:{key}", 1.0 if active else 0.0, 9.0)
    dl.add_rect_filled(ImVec2(pos.x, pos.y), ImVec2(pos.x + width, pos.y + 1),
                       imgui.get_color_u32(t.border))
    if run > 0.005:
        dl.add_rect_filled(ImVec2(pos.x, pos.y),
                           ImVec2(pos.x + width * run, pos.y + thickness),
                           imgui.get_color_u32(theme_mod.with_alpha(t.accent, 0.85)))
    imgui.dummy(ImVec2(width, thickness + 2))
