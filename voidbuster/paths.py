"""Where things live, running from source or from a one-file build.

A frozen exe unpacks itself into a temporary directory that Windows deletes on
exit. Anything written relative to the code therefore vanishes the moment the
tool closes - which for a logger means losing exactly the session you were
recording. So the two kinds of path are kept apart:

  app_dir     the folder the exe sits in. Sessions, crash dumps, settings and
              any profiles you add yourself. Yours, and persistent.
  bundle_dir  the unpacked payload. Read-only, replaced by every new build, and
              the right place only for defaults that ship with the tool.

Running from a checkout the two are the same directory, which is why this is
easy to get wrong and only shows up once someone downloads the release.
"""

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
_SOURCE_ROOT = Path(__file__).resolve().parent.parent


def app_dir():
    """Beside the executable: files the user owns and expects to find again."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return _SOURCE_ROOT


def bundle_dir():
    """Inside the build: defaults shipped with the tool, read-only."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
    return _SOURCE_ROOT


def profile_dirs():
    """Profile folders, most specific first.

    Yours beside the exe wins over the ones that shipped, so a bundled profile
    can be overridden by dropping a file of the same name next to the tool -
    and a new build never overwrites your edits.
    """
    seen, out = set(), []
    for base in (app_dir(), bundle_dir()):
        directory = base / "profiles"
        key = str(directory).lower()
        if key not in seen:
            seen.add(key)
            out.append(directory)
    return out
