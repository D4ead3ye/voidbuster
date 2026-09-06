"""VoidBuster - universal Wii U logger.

    python voidbuster.py                 window
    python voidbuster.py --sweep         find the port the console is using
    python voidbuster.py --quiet --stats 2
    python voidbuster.py --help

The released build is the same program: double-click it for the window, or run
it from a terminal with any of the flags above for the headless viewer.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _hide_own_console():
    """Drop the console window when the exe was double-clicked.

    The build is a console application on purpose - the headless viewer is half
    the tool and it needs somewhere to print. But a black window hanging behind
    the GUI looks like something went wrong.

    The check matters: GetConsoleWindow returns whichever console we are
    attached to, so hiding it blindly would hide the user's own terminal when
    they ran us from one. If we are the only process on this console, it was
    created for us and is ours to close.
    """
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buf = (wintypes.DWORD * 4)()
        count = kernel32.GetConsoleProcessList(buf, 4)
        if count != 1:
            return                      # launched from a shell; leave it alone
        window = kernel32.GetConsoleWindow()
        if window:
            ctypes.WinDLL("user32").ShowWindow(window, 0)   # SW_HIDE
    except Exception:
        # A cosmetic tidy-up must never be the reason the tool fails to start.
        pass


def _line_buffer_output():
    """Flush per line, not per block.

    Python block-buffers stdout the moment it is a pipe or a file - which is
    precisely when someone is capturing a session to read later. A logger that
    gets Ctrl+C'd, or whose console window is closed, would otherwise lose
    everything still sitting in the buffer.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def main():
    _line_buffer_output()
    args = sys.argv[1:]
    gui_only = not args or args == ["--gui"]
    if gui_only:
        _hide_own_console()
        try:
            from voidbuster import gui
        except ImportError as e:
            print("no GUI available (" + str(e) + ")")
            print("the headless viewer needs nothing extra: voidbuster --help")
            return 1
        return gui.run()
    from voidbuster import cli
    return cli.run(args)


if __name__ == "__main__":
    sys.exit(main())
