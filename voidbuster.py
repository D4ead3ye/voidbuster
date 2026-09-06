"""VoidBuster - universal Wii U logger.

    python voidbuster.py                 window
    python voidbuster.py --sweep         find the port the console is using
    python voidbuster.py --quiet --stats 2
    python voidbuster.py --help
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    args = sys.argv[1:]
    gui_only = not args or args == ["--gui"]
    if gui_only:
        try:
            from voidbuster import gui
        except ImportError as e:
            print("no GUI available (" + str(e) + ")")
            print("the headless viewer needs nothing extra: python voidbuster.py --help")
            return 1
        return gui.run()
    from voidbuster import cli
    return cli.run(args)


if __name__ == "__main__":
    sys.exit(main())
