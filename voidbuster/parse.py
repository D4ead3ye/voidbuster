"""Turn an arbitrary console log line into something structured.

Everything here works with no configuration at all. That is the whole point:
a game we have never seen still gets tags, severities, colours and counters,
because all of them are derived from the shape of the text rather than from a
list of patterns somebody had to write first. Profiles (see rules.py) add
precision on top; they are never required to get a usable view.
"""

import colorsys
import re

# Aroma's logging module and a few homebrew loggers emit colour codes. They are
# meaningless once the text is in a table with its own colours, and they wreck
# the tag regexes below if left in.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Loggers that stamp their own time. We keep the text but do not let it be
# mistaken for a tag, and we surface it so two sources can be correlated.
STAMP = re.compile(
    r"^\s*(?:\[|\()?\s*"
    r"(?P<stamp>\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,6})?|\d+\.\d{3,6})"
    r"\s*(?:\]|\))?\s*")

# "[gfx] ", "[gfx][gx2] " - the common homebrew convention.
BRACKET_TAG = re.compile(r"^\s*\[([A-Za-z0-9_.\- /]{1,24})\]\s*")
# "Module::Func: msg" or "gfx: msg" - what OSReport output from retail titles
# and Nintendo's own SDK usually looks like.
COLON_TAG = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_:.\-]{1,31})\s*:\s+(?=\S)")
# A source location prefix, which is a tag in every way that matters here.
FILE_TAG = re.compile(r"^\s*([A-Za-z0-9_\-]{1,32}\.(?:c|cpp|cc|h|hpp))"
                      r"(?::\d+)?\s*[:|]\s*")

# Numbers worth watching, in the three shapes logs actually use:
#   "draws=340"      the explicit one
#   "340 draws"      the one every frame-timing line in existence uses
#   "frame 12"       label first, which reads naturally and so gets written a lot
KV = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_.]{0,31})\s*[=:]\s*"
                r"(-?\d+(?:\.\d+)?)(?![\w.])")
# The lookahead keeps "0x02001234" from being read as the number 0 followed by
# a counter called x02001234. Hex addresses are all over a console log.
NUM_WORD = re.compile(r"(?<![\w.=:])(-?\d+(?:\.\d+)?)\s*(?![xX][0-9a-fA-F])"
                      r"([A-Za-z_][A-Za-z0-9_]{1,19})(?![\w.])")
WORD_NUM = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]{1,19})\s+"
                      r"(-?\d+(?:\.\d+)?)(?![\w.])")

# Words that carry no meaning as a counter name. Without this, one line of
# English prose fills the counter table with "the", "of" and "at".
FILLER = frozenset("""
a an and are as at be been by for from had has have in into is it its of on
or that the this to was were will with we you i he she they not no yes if
""".split())

# One prose line should never be able to flood the table.
MAX_AUTO = 8

LEVELS = ("fatal", "error", "warn", "info", "debug", "trace")

# Ordered: the first hit wins, so "assertion failed" is fatal, not merely a
# line that happens to contain "fail".
LEVEL_RULES = [
    ("fatal", (
        "std::terminate", "back chain", "oscrash", "osfatal", "unhandled exception",
        "segmentation fault", "dsi exception", "isi exception", "program exception",
        "assertion failed", "assert failed", "panic", "fatal", "abort()",
        "*** exception", "crash", "stack dump", "guru", "data storage interrupt",
    )),
    ("error", (
        "error", "err:", "failed", "failure", "cannot ", "could not", "unable to",
        "invalid", "denied", "refused", "corrupt", "missing", "not found",
        "0x80004005", "einval", "enomem", "nullptr", "null pointer",
    )),
    ("warn", (
        "warn", "warning", "deprecat", "fallback", "falling back", "retry",
        "retrying", "timeout", "timed out", "dropped", "skipped", "stall",
        "clamped", "truncat", "unsupported",
    )),
    ("debug", ("debug", "dbg:", "verbose")),
    ("trace", ("trace", "enter ", "leave ", "-> ", "<- ")),
]


def _hue_for(name):
    """A stable colour per tag.

    Hashing to a hue rather than assigning from a palette means the same tag is
    the same colour in every session and across machines, which is what makes a
    scrolling log skimmable. Python's str hash is salted per process, so this
    uses an explicit FNV-1a instead.
    """
    h = 2166136261
    for ch in name.encode("utf-8"):
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return (h % 997) / 997.0


def tag_color(name):
    """RGBA for a tag. Fixed saturation and value so every tag reads as part of
    one set instead of a bag of arbitrary colours."""
    r, g, b = colorsys.hsv_to_rgb(_hue_for(name), 0.55, 1.0)
    return (r, g, b, 1.0)


def level_of(text):
    low = text.lower()
    for level, needles in LEVEL_RULES:
        for n in needles:
            if n in low:
                return level
    return "info"


def split_tag(text):
    """Peel tags off the front of a line.

    Returns (tags, remainder). Several prefixes are consumed because
    "[net][sync] joined" carries two useful facets, and filtering on either
    should find the line.
    """
    tags = []
    rest = text
    for _ in range(3):
        m = BRACKET_TAG.match(rest)
        if m:
            tags.append(m.group(1).strip().lower())
            rest = rest[m.end():]
            continue
        m = FILE_TAG.match(rest)
        if m:
            tags.append(m.group(1).lower())
            rest = rest[m.end():]
            continue
        if not tags:
            m = COLON_TAG.match(rest)
            # A bare "colon tag" is only believable at the very start of a line
            # and only if what follows is not itself a number - otherwise
            # "count: 5" would be read as a tag named "count".
            if m and not re.match(r"-?\d", rest[m.end():]):
                tags.append(m.group(1).strip().lower())
                rest = rest[m.end():]
                continue
        break
    return tags, rest


def counters_in(text):
    """Numeric key=value pairs, as a dict.

    Nothing is filtered by name. A game we have never seen logs whatever it
    logs, and the interesting number is more often than not one nobody thought
    to add a rule for.
    """
    out = {}

    def put(key, val, loose=True):
        key = key.lower()
        if key in out or len(out) >= MAX_AUTO:
            return
        # The stoplist only applies to the loose shapes. "in=180" is an
        # explicit pair and means what it says, even though "in" is a word.
        if loose and key in FILLER:
            return
        try:
            out[key] = float(val) if "." in val else int(val)
        except ValueError:
            pass

    # Explicit pairs first: they are the ones the author meant, and taking them
    # first means a later loose match cannot overwrite one with a worse guess.
    for key, val in KV.findall(text):
        put(key, val, loose=False)
    for val, key in NUM_WORD.findall(text):
        put(key, val)
    for key, val in WORD_NUM.findall(text):
        put(key, val)
    return out


def parse(raw):
    """One line in, a dict of facets out."""
    text = ANSI.sub("", raw).rstrip("\r\n")
    stamp = ""
    m = STAMP.match(text)
    if m:
        stamp = m.group("stamp")
        text = text[m.end():]
    tags, body = split_tag(text)
    return {
        "text": text,
        "body": body,
        "tags": tags,
        "stamp": stamp,
        "level": level_of(text),
        "counters": counters_in(body),
    }
