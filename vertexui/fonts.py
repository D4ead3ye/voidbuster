"""Real typefaces instead of ImGui's built-in bitmap font.

ImGui ships Proggy: a 13px bitmap face. It is crisp and it is
unmistakably from 2005, and no amount of restyling rescues a panel drawn
in it. Loading system faces costs nothing to ship and survives being
frozen into a one-file exe, which bundled font assets did not.

    from vertexui import fonts
    params.callbacks.load_additional_fonts = fonts.loader()
    ...
    with fonts.use("semi"):
        imgui.text("heading")

Roles, not filenames: "ui", "semi", "title", "mono". A project on a
machine without Segoe can pass its own mapping and every widget follows.
"""

import os
from pathlib import Path

from imgui_bundle import imgui

# role -> (candidate filenames in order, size in px)
DEFAULT_FACES = {
    "ui":    (("segoeui.ttf", "arial.ttf"), 17.0),
    "semi":  (("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"), 17.0),
    "title": (("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"), 21.0),
    # Fixed-width for logs and tables: timestamps, scores and coordinates
    # only line up into scannable columns in a mono face.
    "mono":  (("CascadiaMono.ttf", "consola.ttf", "cour.ttf"), 14.0),
}

_loaded = {}


def loader(faces=None, fonts_dir=None, default_role="ui"):
    """Return a callback for hello_imgui's `load_additional_fonts`."""
    faces = faces or DEFAULT_FACES
    directory = Path(fonts_dir or (Path(os.environ.get("WINDIR", r"C:\Windows"))
                                   / "Fonts"))

    def load():
        io = imgui.get_io()
        for role, (names, size) in faces.items():
            for name in names:
                path = directory / name
                if not path.exists():
                    continue
                cfg = imgui.ImFontConfig()
                # Small UI text on a dark ground is where under-sampled
                # glyphs look muddiest.
                cfg.oversample_h = 3
                try:
                    font = io.fonts.add_font_from_file_ttf(str(path), size, cfg)
                except Exception:
                    continue
                # Keep the size: this ImGui wants push_font(font, size) and
                # rejects a bare font handle.
                _loaded[role] = (font, size)
                break
        if default_role in _loaded:
            io.font_default = _loaded[default_role][0]

    return load


def available() -> bool:
    return bool(_loaded)


class use:
    """`with fonts.use("semi"):` - silently does nothing if that role never
    loaded, so a machine missing a face still renders."""

    def __init__(self, role: str):
        self.entry = _loaded.get(role)

    def __enter__(self):
        if self.entry is not None:
            imgui.push_font(*self.entry)
        return self

    def __exit__(self, *exc):
        if self.entry is not None:
            imgui.pop_font()
        return False
