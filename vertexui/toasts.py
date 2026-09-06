"""Click-through notifications that draw over another application.

A panel you have to alt-tab to in order to read defeats the point when the
thing being reported is happening in a fullscreen app. This is a separate
always-on-top window that can never take focus and never receives a click,
so it can report over a game, a video, a kiosk - anything.

Three window styles do the work:

    WS_EX_TRANSPARENT   clicks pass straight through to what is behind
    WS_EX_NOACTIVATE    can never take focus, even if clicked
    WS_EX_TOOLWINDOW    stays out of alt-tab and the taskbar

Two details that are easy to get wrong and fail silently:

  * the window MUST pump its message queue. A window whose thread never
    does is, to the rest of Windows, a hung window - and GetWindowText
    sends WM_GETTEXT to windows in the same process, so the owning app can
    deadlock on its own overlay.
  * UpdateLayeredWindow wants premultiplied alpha, or everything washes
    out pale instead of reading as translucent.

    from vertexui import toasts
    tray = toasts.Toasts(anchor_titles=["My Game"])
    tray.start()
    tray.notify("ok", "Connected")

It does not depend on ImGui and runs happily in a process with no GUI.
"""

import ctypes
import ctypes.wintypes as wt
import math
import queue
import threading
import time

WIDTH, ROW_H, GAP, PAD = 520, 44, 10, 14
SLIDE_IN, SLIDE_OUT = 0.32, 0.26

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

# LRESULT/WPARAM/LPARAM are pointer-sized. Left as ctypes' default c_int
# they overflow on 64-bit (the window proc raises "int too long to
# convert" on every message) and handle-returning calls get truncated into
# handles that look valid and are not.
LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)
_user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
_user32.DefWindowProcW.restype = LRESULT


WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x00000008, 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP, SW_SHOWNA, ULW_ALPHA = 0x80000000, 8, 0x00000002
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# BGR, matching the default theme's status colours.
DEFAULT_COLOURS = {"ok": (113, 201, 83), "warn": (69, 170, 229),
                   "error": (85, 67, 229), "info": (179, 163, 154)}


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


# CreateWindowExW takes a module handle and window styles that exceed a
# signed 32-bit int (WS_POPUP is 0x80000000). Without argtypes ctypes
# guesses per call and raises "int too long to convert" whenever the
# values happen to be large - which depends on the process, so it fails
# on some runs and not others.
_kernel32 = ctypes.windll.kernel32
_kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wt.HMODULE
_user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
_user32.CreateWindowExW.restype = wt.HWND
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
_user32.GetDC.restype = wt.HDC
_user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
_gdi32.CreateCompatibleDC.restype = wt.HDC
_gdi32.CreateDIBSection.restype = wt.HBITMAP
_gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
_gdi32.SelectObject.restype = wt.HANDLE
_gdi32.DeleteObject.argtypes = [wt.HANDLE]
_gdi32.DeleteDC.argtypes = [wt.HDC]


def _title(hwnd) -> str:
    # No GetWindowTextLength: it sends WM_GETTEXTLENGTH and blocks for as
    # long as the target process is hung. GetWindowText is documented not
    # to, so a fixed buffer is the safe pairing.
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value or ""


def _find(titles):
    found = []

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        t = _title(hwnd).strip().lower()
        if t and any((p or "").strip().lower() == t for p in titles):
            found.append(hwnd)
            return False
        return True

    try:
        _user32.EnumWindows(_ENUM_PROC(cb), 0)
    except Exception:
        return None
    return found[0] if found else None


def _ease(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _make_box(kind, label, colours):
    import cv2
    import numpy as np
    col = colours.get(kind, colours["info"])
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_DUPLEX, 0.48, 1)
    w = min(WIDTH, tw + 74)
    box = np.zeros((ROW_H, w, 4), np.uint8)
    r = 9
    body = (16, 16, 18, 236)
    cv2.rectangle(box, (r, 0), (w - r - 1, ROW_H - 1), body, -1)
    cv2.rectangle(box, (0, r), (w - 1, ROW_H - r - 1), body, -1)
    for cx, cy in ((r, r), (w - r - 1, r), (r, ROW_H - r - 1), (w - r - 1, ROW_H - r - 1)):
        cv2.circle(box, (cx, cy), r, body, -1, cv2.LINE_AA)
    cv2.rectangle(box, (0, 7), (3, ROW_H - 8), col + (255,), -1)
    _icon(box, 28, ROW_H // 2, 9, kind, col)
    cv2.putText(box, label, (48, ROW_H // 2 + 5), cv2.FONT_HERSHEY_DUPLEX,
                0.48, (238, 238, 242, 255), 1, cv2.LINE_AA)
    return box


def _icon(img, cx, cy, r, kind, col):
    """Drawn with primitives - no font dependency, crisp at any DPI."""
    import cv2
    import numpy as np
    ink = (16, 16, 18, 255)
    if kind == "warn":
        pts = np.array([[cx, cy - r], [cx - r, cy + r - 2], [cx + r, cy + r - 2]], np.int32)
        cv2.fillPoly(img, [pts], col + (255,), cv2.LINE_AA)
        cv2.line(img, (cx, cy - 3), (cx, cy + 2), ink, 2, cv2.LINE_AA)
        cv2.circle(img, (cx, cy + 5), 1, ink, -1, cv2.LINE_AA)
        return
    cv2.circle(img, (cx, cy), r, col + (255,), -1, cv2.LINE_AA)
    if kind == "ok":
        cv2.line(img, (cx - 4, cy), (cx - 1, cy + 4), ink, 2, cv2.LINE_AA)
        cv2.line(img, (cx - 1, cy + 4), (cx + 4, cy - 4), ink, 2, cv2.LINE_AA)
    elif kind == "error":
        cv2.line(img, (cx - 3, cy - 3), (cx + 3, cy + 3), ink, 2, cv2.LINE_AA)
        cv2.line(img, (cx + 3, cy - 3), (cx - 3, cy + 3), ink, 2, cv2.LINE_AA)
    else:
        cv2.circle(img, (cx, cy - 4), 1, ink, -1, cv2.LINE_AA)
        cv2.line(img, (cx, cy - 1), (cx, cy + 4), ink, 2, cv2.LINE_AA)


def render(items, now, height, dt=0.016, colours=None):
    """Composite the stack into one BGRA surface.

    Three things move independently: the slide (inside a fixed window -
    moving a layered window every frame flickers), the opacity, and each
    row's Y easing toward its slot. That last one matters most: without it
    rows SNAP upward the moment one expires.
    """
    import cv2
    import numpy as np
    colours = colours or DEFAULT_COLOURS

    surf = np.zeros((height, WIDTH, 4), np.uint8)
    slot = 0
    for t in items:
        age = now - t["born"]
        label = t["text"] if t.get("count", 1) < 2 else f"{t['text']}  x{t['count']}"
        box = _make_box(t["kind"], label, colours)
        bw = box.shape[1]

        target_y = float(slot * (ROW_H + GAP))
        if t.get("y") is None:
            t["y"] = target_y
        else:
            t["y"] += (target_y - t["y"]) * (1.0 - math.exp(-13.0 * dt))
        y = int(round(t["y"]))

        if age < SLIDE_IN:
            k = _ease(age / SLIDE_IN)
            off, alpha = int(-bw * (1.0 - k)), k
        elif t["leaving"] is not None:
            k = _ease((now - t["leaving"]) / SLIDE_OUT)
            off, alpha = int(-bw * k), 1.0 - k
        else:
            off, alpha = 0, 1.0

        if off > -bw and 0 <= y and y + ROW_H <= height and alpha > 0.01:
            piece = box[:, max(0, -off):max(0, -off) + bw - abs(off)]
            if alpha < 0.999:
                piece = piece.copy()
                piece[:, :, 3] = (piece[:, :, 3].astype(np.float32) * alpha).astype(np.uint8)
            x0 = max(0, off)
            surf[y:y + ROW_H, x0:x0 + piece.shape[1]] = piece
        slot += 1
    return surf


class Toasts:
    """A tray of click-through notifications pinned to another window."""

    def __init__(self, anchor_titles=None, seconds=4.0, margin=16,
                 max_visible=5, colours=None, exclude_from_capture=True,
                 on_status=None):
        self.anchor_titles = [t for t in (anchor_titles or []) if t]
        self.hold = float(seconds)
        self.margin = int(margin)
        self.max_visible = int(max_visible)
        self.colours = colours or DEFAULT_COLOURS
        # Hidden from screen capture so an app that reads the screen never
        # photographs its own notifications. On a few graphics setups the
        # flag stops the window drawing entirely - hence the switch.
        self.exclude_from_capture = bool(exclude_from_capture)
        # Reports whether the window actually came up. Without it, "no
        # toasts" and "the overlay never started" look identical.
        self.on_status = on_status
        self._q = queue.Queue()
        self._items = []
        self._thread = None
        self._stop = threading.Event()
        self._hwnd = None
        self._last_frame = time.time()

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="vertexui-toasts",
                                        daemon=True)
        self._thread.start()

    def notify(self, kind: str, text: str):
        self._q.put((kind, text))

    def stop(self):
        self._stop.set()

    # -- internals ------------------------------------------------------
    def _height(self):
        return self.max_visible * (ROW_H + GAP)

    def _create_window(self):
        cls = "VertexUIToasts"
        # Keep a reference: without one the trampoline is collected and
        # Windows calls into freed memory on the first message.
        self._wndproc = _WNDPROC(lambda h, m, w, l: _user32.DefWindowProcW(h, m, w, l))
        wc = WNDCLASS()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = _kernel32.GetModuleHandleW(None)
        wc.lpszClassName = cls
        _user32.RegisterClassW(ctypes.byref(wc))
        ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
              | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self._hwnd = _user32.CreateWindowExW(
            ex, cls, "VertexUI toasts", WS_POPUP, 0, 0, WIDTH, self._height(),
            None, None, wc.hInstance, None)
        excluded = False
        if self.exclude_from_capture:
            try:
                excluded = bool(_user32.SetWindowDisplayAffinity(
                    self._hwnd, WDA_EXCLUDEFROMCAPTURE))
            except Exception:
                excluded = False
        _user32.ShowWindow(self._hwnd, SW_SHOWNA)
        if self.on_status:
            self.on_status(f"Notifications ready (window {self._hwnd}, "
                           f"hidden from capture: {excluded}).")

    def _anchor(self):
        hwnd = _find(self.anchor_titles) if self.anchor_titles else None
        if hwnd:
            r = wt.RECT()
            if _user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return r.left + self.margin, r.top + self.margin
        return self.margin, self.margin

    def _blit(self, surf):
        import numpy as np
        h, w = surf.shape[:2]
        a = surf[:, :, 3:4].astype(np.uint16)
        pm = surf.copy()
        pm[:, :, :3] = (surf[:, :, :3].astype(np.uint16) * a // 255).astype(np.uint8)

        hdc = _user32.GetDC(None)
        mem = _gdi32.CreateCompatibleDC(hdc)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = w, -h        # negative: top-down rows
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        bits = ctypes.c_void_p()
        bmp = _gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = _gdi32.SelectObject(mem, bmp)
        ctypes.memmove(bits, pm.ctypes.data, pm.nbytes)
        x, y = self._anchor()
        _user32.UpdateLayeredWindow(self._hwnd, hdc, ctypes.byref(wt.POINT(x, y)),
                                    ctypes.byref(wt.SIZE(w, h)), mem,
                                    ctypes.byref(wt.POINT(0, 0)), 0,
                                    ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)), ULW_ALPHA)
        _gdi32.SelectObject(mem, old)
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(mem)
        _user32.ReleaseDC(None, hdc)

    def _drain(self):
        now = time.time()
        while True:
            try:
                kind, text = self._q.get_nowait()
            except queue.Empty:
                break
            # A repeating source emits the same line over and over; collapse
            # those into a counter rather than letting them push everything
            # else off screen.
            for t in self._items:
                if t["text"] == text and t["leaving"] is None and now - t["born"] < 2.5:
                    t["born"], t["count"] = now, t.get("count", 1) + 1
                    break
            else:
                self._items.append({"kind": kind, "text": text, "born": now,
                                    "leaving": None, "count": 1, "y": None})
        if len(self._items) > self.max_visible:
            for t in self._items[:-self.max_visible]:
                if t["leaving"] is None:
                    t["leaving"] = now

    def _run(self):
        import numpy as np
        try:
            self._create_window()
        except Exception as e:
            if self.on_status:
                self.on_status(f"Notifications could not start: {e!r}")
            return
        height = self._height()
        blank = True
        msg = wt.MSG()
        while not self._stop.is_set():
            # A window whose thread never pumps messages is a hung window to
            # the rest of Windows - and the owning process can deadlock on
            # it while enumerating windows.
            while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            now = time.time()
            self._drain()
            for t in self._items:
                if t["leaving"] is None and now - t["born"] >= self.hold:
                    t["leaving"] = now
            self._items = [t for t in self._items
                           if t["leaving"] is None or now - t["leaving"] < SLIDE_OUT]
            dt = min(0.1, max(0.001, now - self._last_frame))
            self._last_frame = now
            if self._items:
                try:
                    self._blit(render(self._items, now, height, dt, self.colours))
                except Exception:
                    pass
                blank = False
            elif not blank:
                try:
                    self._blit(np.zeros((height, WIDTH, 4), np.uint8))
                except Exception:
                    pass
                blank = True
            time.sleep(0.016 if self._items else 0.15)
        try:
            _user32.DestroyWindow(self._hwnd)
        except Exception:
            pass
