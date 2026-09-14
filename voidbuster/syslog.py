"""Aroma / Cafe OS crash dumps: finding them, and putting the ring back in order.

A crash dump is not a file. It is a directory named for the moment it was
written, holding 100 fixed-size chunks that the console fills round-robin, plus
a 4-byte meta.bin. Read the chunks in filename order and you get a log that
jumps back in time somewhere in the middle, because the ring has wrapped; read
only the ones ending in .txt and you get nothing at all.

Two details make naive reading fail even once the files are found:

  * records are separated by CR, not LF. Splitting on newlines yields one
    enormous line per chunk and every downstream parser gives up.
  * each record is prefixed with its own clock, `HH;MM;SS;mmm:` (some system
    components use colons instead). That clock is the console's uptime-ish
    log clock, and it is NOT the same clock the game stamps into its own
    messages - in real dumps the two differ by the better part of an hour.

What comes out of here is ordinary text, oldest first, which the existing dump
parser already understands.
"""

import re
from pathlib import Path

# 100 chunks of 32KB is what the console writes today. Neither number is
# assumed anywhere below - the ring is measured, not declared - but they are
# useful for recognising a dump directory on sight.
TYPICAL_CHUNKS = 100
TYPICAL_CHUNK_BYTES = 32768

# `13;16;38;965: ` and the system-component variant `00:05:19:446: `.
RECORD = re.compile(r"(?:^|[\r\n])(\d{2})[;:](\d{2})[;:](\d{2})[;:](\d{3}):[ \t]?")
# Full dates appear in syslog headers and in replayed game breadcrumbs alike,
# so they order chunks well but must never be mistaken for the record clock.
FULL_DATE = re.compile(r"(20\d\d)-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# A chunk file is named for its index: 0.log, 1.log ... Some builds write .bin
# or no extension at all, so the name is what identifies it, not the suffix.
CHUNK_NAME = re.compile(r"^(\d{1,3})(?:\.[A-Za-z0-9]+)?$")
META_NAME = re.compile(r"^meta\.bin$", re.I)


class Chunk:
    """One slice of the ring, with whatever clock could be read out of it."""

    __slots__ = ("index", "path", "raw", "first_secs", "first_date", "records")

    def __init__(self, index, raw, path=None):
        self.index = index
        self.path = Path(path) if path else None
        self.raw = raw
        self.first_secs = None
        self.first_date = ""
        self.records = 0
        self._measure()

    def _measure(self):
        text = decode(self.raw)
        m = RECORD.search(text)
        if m:
            self.first_secs = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
        d = FULL_DATE.search(text)
        if d:
            self.first_date = "".join(d.groups())
        self.records = len(RECORD.findall(text))

    def key(self):
        """What to order by. The full date wins when present: it survives the
        midnight rollover that a bare HH;MM;SS cannot."""
        return (self.first_date or "", self.first_secs if self.first_secs is not None else -1)

    def empty(self):
        return self.records == 0 and not self.raw.strip(b"\x00").strip()


def decode(raw):
    return raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw


def is_dump_dir(directory):
    """True for a directory that looks like a chunked crash dump."""
    directory = Path(directory)
    if not directory.is_dir():
        return False
    return any(CHUNK_NAME.match(p.name) for p in directory.iterdir() if p.is_file())


def find_dumps(root):
    """Every chunked dump directory at or under `root`, newest name last.

    Aroma names the directory for the crash time, so the name sorts
    chronologically without having to open anything.
    """
    root = Path(root)
    out = []
    if not root.is_dir():
        return out
    if is_dump_dir(root):
        out.append(root)
    for path in sorted(root.rglob("*")):
        if path.is_dir() and is_dump_dir(path):
            out.append(path)
    return out


def read_chunks(directory):
    """Load the numbered files. meta.bin and anything else is left alone."""
    directory = Path(directory)
    chunks = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        m = CHUNK_NAME.match(path.name)
        if not m:
            continue
        try:
            chunks.append(Chunk(int(m.group(1)), path.read_bytes(), path))
        except OSError:
            continue
    chunks.sort(key=lambda c: c.index)
    return chunks


def order(chunks):
    """Rotate the ring so the oldest chunk is first.

    A round-robin buffer read in index order is already chronological apart
    from exactly one step: the seam where the newest chunk is followed by the
    oldest. Finding that seam and rotating is exact, and unlike sorting it
    copes with the many chunks that share a first timestamp.

    If more than one seam appears - a dump that was interrupted, or clocks that
    went backwards for their own reasons - this falls back to a stable sort,
    which is merely a good guess, and says so via `seam` being None.
    """
    live = [c for c in chunks if not c.empty()]
    if len(live) < 2:
        return live, None
    keys = [c.key() for c in live]
    seams = [i for i in range(len(keys) - 1) if keys[i + 1] < keys[i]]
    if len(seams) == 1:
        cut = seams[0] + 1
        return live[cut:] + live[:cut], live[seams[0]].index
    if not seams:
        return live, live[-1].index
    return sorted(live, key=Chunk.key), None


def split_records(text):
    """Records, in order, as (clock, body).

    `clock` is the record's own `HH;MM;SS;mmm` prefix or "" when a line carries
    none - syslog headers and continuation lines do not.
    """
    text = decode(text)
    out = []
    pos = 0
    pending = ""
    for m in RECORD.finditer(text):
        body = text[pos:m.start()] if pos else text[:m.start()]
        if pending or body.strip():
            out.append((pending, body))
        pending = "%s;%s;%s;%s" % (m[1], m[2], m[3], m[4])
        pos = m.end()
    tail = text[pos:]
    if pending or tail.strip():
        out.append((pending, tail))
    return out


def to_lines(text, keep_clock=True):
    """Flatten reassembled text into log lines a parser can read.

    Splitting is on CR first, because that is what separates records here, then
    on LF for the multi-line blocks (the register dump is one record spanning
    twenty lines). The record clock is put back on the front in the shape the
    rest of the tool already recognises as a timestamp.
    """
    out = []
    for clock, body in split_records(text):
        body = ANSI.sub("", body)
        for piece in body.replace("\r", "\n").split("\n"):
            piece = piece.rstrip()
            if not piece.strip():
                continue
            if clock and keep_clock:
                out.append(clock.replace(";", ":", 2).replace(";", ".") + " " + piece)
            else:
                out.append(piece)
    return out


def assemble(directory):
    """One dump directory in, ordered text plus a description of what was read.

    The description matters: a dump whose seam could not be found is still
    worth reading, but you should know the order is inferred rather than
    certain before you trust the tail of it.
    """
    chunks = read_chunks(directory)
    if not chunks:
        return "", {"chunks": 0, "error": "no numbered chunk files in " + str(directory)}
    ordered, seam = order(chunks)
    text = "".join(decode(c.raw) for c in ordered)
    info = {
        "dir": str(directory),
        "chunks": len(chunks),
        "used": len(ordered),
        "bytes": sum(len(c.raw) for c in ordered),
        "seam": seam,
        "ordered": [c.index for c in ordered],
        "newest": ordered[-1].index if ordered else None,
        "records": sum(c.records for c in ordered),
        "certain": seam is not None,
    }
    return text, info


def tail_records(text, count=400):
    """The last N records - where the exception lives, every time."""
    return to_lines(text)[-count:]
