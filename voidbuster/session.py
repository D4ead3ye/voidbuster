"""The live state of one logging session.

Holds the lines, the numbers pulled out of them, and the two things that
actually tell you what a game is doing when you cannot attach a debugger:

  rates    how often each tag fires, so a subsystem going quiet is visible
           before anything crashes
  stalls   a gap in a stream that was previously steady, which on this console
           is what a freeze looks like from the outside

Recording is unconditional and happens as lines arrive. A session you forgot to
save is the one you needed, and a hard freeze does not give you the chance.
"""

import datetime
import io
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from . import parse

MAX_LINES = 20000
# Rate is measured over a window rather than since the start: a subsystem that
# was busy for a minute and then stopped should read as stopped, not as "still
# averaging 40/s".
RATE_WINDOW_S = 5.0
# How quiet a previously-talkative stream has to go before it is called a stall.
STALL_AFTER_S = 6.0
# Lines kept either side of a fatal line when a crash snapshot is written.
CRASH_CONTEXT = 400


class Line:
    """One parsed line, kept small - there can be twenty thousand of them."""

    __slots__ = ("n", "at", "text", "body", "tags", "level", "source", "origin",
                 "counters", "labels", "alert")

    def __init__(self, n, event, parsed):
        self.n = n
        self.at = event.at
        self.text = parsed["text"]
        self.body = parsed["body"]
        self.tags = parsed["tags"]
        self.level = parsed["level"]
        self.source = event.source
        self.origin = event.origin
        self.counters = parsed["counters"]
        self.labels = ()
        self.alert = False

    def stamp(self):
        return datetime.datetime.fromtimestamp(self.at).strftime("%H:%M:%S.%f")[:-3]

    def matches(self, needle, regex=None):
        if regex is not None:
            return bool(regex.search(self.text))
        return needle in self.text.lower()


class Counter:
    """One number over time. Keeps enough history to draw a sparkline and to
    say whether it is moving, without keeping the whole session."""

    __slots__ = ("name", "value", "lo", "hi", "first", "updates", "history", "at", "rule")

    def __init__(self, name, value, rule=False):
        self.name = name
        self.value = value
        self.lo = value
        self.hi = value
        self.first = value
        self.updates = 1
        self.history = deque(maxlen=120)
        self.history.append(value)
        self.at = time.time()
        self.rule = rule

    def update(self, value, rule=False):
        self.value = value
        if isinstance(value, (int, float)):
            self.lo = min(self.lo, value) if isinstance(self.lo, (int, float)) else value
            self.hi = max(self.hi, value) if isinstance(self.hi, (int, float)) else value
        self.updates += 1
        self.history.append(value)
        self.at = time.time()
        # Once a rule has claimed a counter it stays claimed, so a stray
        # automatic match cannot demote it out of the pinned group.
        self.rule = self.rule or rule


class TagStat:
    __slots__ = ("name", "count", "recent", "last_at", "levels", "color")

    def __init__(self, name):
        self.name = name
        self.count = 0
        self.recent = deque(maxlen=400)
        self.last_at = 0.0
        self.levels = defaultdict(int)
        self.color = parse.tag_color(name)

    def bump(self, at, level):
        self.count += 1
        self.recent.append(at)
        self.last_at = at
        self.levels[level] += 1

    def rate(self, now=None):
        now = now or time.time()
        cutoff = now - RATE_WINDOW_S
        while self.recent and self.recent[0] < cutoff:
            self.recent.popleft()
        return len(self.recent) / RATE_WINDOW_S

    def quiet_for(self, now=None):
        return (now or time.time()) - self.last_at if self.last_at else 0.0


class Session:
    """Everything the UI and the CLI read from."""

    def __init__(self, record_dir=None, profile=None, title="session"):
        self.lines = deque(maxlen=MAX_LINES)
        self.counters = {}
        self.tags = {}
        self.levels = defaultdict(int)
        self.sources = defaultdict(int)
        self.consoles = {}
        self.alerts = deque(maxlen=200)
        self.stalls = deque(maxlen=100)
        self.crashes = []
        self.profile = profile
        self.total = 0
        self.dropped = 0
        self.started = time.time()
        self.last_at = 0.0
        self.rate_recent = deque(maxlen=4000)
        self._n = 0
        self._stalled = set()
        self._record = None
        self._record_path = None
        self._first_lines = []
        self.title = title
        if record_dir:
            self._open_record(Path(record_dir), title)

    # ------------------------------------------------------------- recording

    def _open_record(self, directory, title):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", title).strip("-") or "session"
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            self._record_path = directory / (stamp + "-" + safe + ".log")
            self._record = io.open(self._record_path, "a", encoding="utf-8", newline="\n")
            self._record.write("# VoidBuster session " + datetime.datetime.now().isoformat(" ", "seconds") + "\n")
        except OSError:
            # Asked once and refused. Retrying every line would turn a
            # permissions problem into a performance one.
            self._record = None
            self._record_path = None

    @property
    def record_path(self):
        return self._record_path

    def _write(self, line):
        if not self._record:
            return
        try:
            self._record.write("[" + line.stamp() + "] " + line.text + "\n")
        except OSError:
            self._record = None

    def flush(self):
        if self._record:
            try:
                self._record.flush()
            except OSError:
                self._record = None

    def close(self):
        self.flush()
        if self._record:
            try:
                self._record.close()
            except OSError:
                pass
            self._record = None

    # ---------------------------------------------------------------- ingest

    def add(self, event):
        parsed = parse.parse(event.text)
        self._n += 1
        line = Line(self._n, event, parsed)

        if self.profile is not None:
            extra, level, alert, labels = self.profile.apply(parsed["text"])
            if extra:
                line.counters = dict(line.counters)
                line.counters.update(extra)
                for key, val in extra.items():
                    self._counter(key, val, rule=True)
            if level:
                line.level = level
            if labels:
                line.labels = tuple(labels)
            if alert:
                line.alert = True
                self.alerts.append(line)

        for key, val in parsed["counters"].items():
            self._counter(key, val)

        if line.level in ("fatal", "error") and not line.alert:
            line.alert = True
            self.alerts.append(line)
        if line.level == "fatal":
            self.crashes.append(line)

        for tag in line.tags or ("(untagged)",):
            stat = self.tags.get(tag)
            if stat is None:
                stat = self.tags[tag] = TagStat(tag)
                if self.profile is not None and tag in self.profile.tags:
                    stat.color = _hex_rgba(self.profile.tags[tag], stat.color)
            stat.bump(line.at, line.level)

        self.levels[line.level] += 1
        self.sources[line.source] += 1
        if line.origin:
            self.consoles[line.origin] = line.at
        self.total += 1
        self.last_at = line.at
        self.rate_recent.append(line.at)
        if len(self._first_lines) < 400:
            self._first_lines.append(line.text)

        if len(self.lines) == self.lines.maxlen:
            self.dropped += 1
        self.lines.append(line)
        self._write(line)
        return line

    def _counter(self, name, value, rule=False):
        existing = self.counters.get(name)
        if existing is None:
            self.counters[name] = Counter(name, value, rule=rule)
        else:
            existing.update(value, rule=rule)

    # --------------------------------------------------------------- reading

    def rate(self, now=None):
        now = now or time.time()
        cutoff = now - RATE_WINDOW_S
        while self.rate_recent and self.rate_recent[0] < cutoff:
            self.rate_recent.popleft()
        return len(self.rate_recent) / RATE_WINDOW_S

    def check_stalls(self, now=None):
        """Tags that were talking and have stopped.

        Reported once per stall rather than every tick, and cleared when the tag
        speaks again, so a stall that resolves does not stay on the screen
        looking like it is still happening.
        """
        now = now or time.time()
        new = []
        for tag, stat in self.tags.items():
            if stat.count < 8 or not stat.last_at:
                continue
            quiet = now - stat.last_at
            if quiet >= STALL_AFTER_S:
                if tag not in self._stalled:
                    self._stalled.add(tag)
                    entry = (now, tag, quiet, stat.count)
                    self.stalls.append(entry)
                    new.append(entry)
            elif tag in self._stalled:
                self._stalled.discard(tag)
        return new

    def stalled_tags(self):
        return sorted(self._stalled)

    def filtered(self, needle="", levels=None, tags=None, regex=False, limit=None,
                 max_n=0):
        """The lines a view should show.

        Walks newest-first and stops once `limit` is reached, because the common
        case is a tail of a very long session and scanning all of it every frame
        is what makes a log viewer feel slow.
        """
        pattern = None
        if needle and regex:
            try:
                pattern = re.compile(needle, re.I)
            except re.error:
                return [], "bad regex"
        needle = needle.lower()
        out = []
        for line in reversed(self.lines):
            # A paused view is pinned to the line it was paused at. Lines after
            # it keep arriving, keep counting and keep being recorded - pausing
            # is about reading, not about dropping evidence.
            if max_n and line.n > max_n:
                continue
            if levels and line.level not in levels:
                continue
            if tags and not (set(line.tags or ("(untagged)",)) & tags):
                continue
            if needle and not line.matches(needle, pattern):
                continue
            out.append(line)
            if limit and len(out) >= limit:
                break
        out.reverse()
        return out, ""

    def snapshot_crash(self, directory, line=None):
        """Write the lines around a fatal one to their own file.

        The context is the point: a crash line on its own says almost nothing,
        and the interesting part is always the two hundred lines before it.
        """
        directory = Path(directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return None, str(e)
        target = line or (self.crashes[-1] if self.crashes else None)
        lines = list(self.lines)
        if target is not None:
            idx = next((i for i, l in enumerate(lines) if l.n == target.n), len(lines) - 1)
            lo = max(0, idx - CRASH_CONTEXT)
            hi = min(len(lines), idx + 40)
            lines = lines[lo:hi]
        else:
            lines = lines[-CRASH_CONTEXT:]
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = directory / ("crash-" + stamp + ".log")
        try:
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("# VoidBuster crash snapshot " + stamp + "\n")
                if self.profile is not None:
                    fh.write("# profile: " + self.profile.name + "\n")
                for l in lines:
                    marker = ">>" if target is not None and l.n == target.n else "  "
                    fh.write(marker + " [" + l.stamp() + "] " + l.text + "\n")
        except OSError as e:
            return None, str(e)
        return path, ""

    def save_text(self, directory, lines=None, note=""):
        """Write lines out as plain text, for sending to someone else.

        Takes the lines it is given rather than the whole buffer, so what gets
        saved is what was on screen - a filter narrowed down to the one
        subsystem that misbehaves is usually the thing worth keeping, and the
        header records which filter that was.
        """
        directory = Path(directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return None, str(e)
        lines = list(self.lines) if lines is None else list(lines)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = directory / ("saved-" + stamp + ".txt")
        try:
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("# VoidBuster " + datetime.datetime.now().isoformat(" ", "seconds") + "\n")
                if self.profile is not None:
                    fh.write("# profile: " + self.profile.name + "\n")
                if note:
                    fh.write("# " + note + "\n")
                fh.write("# %d of %d lines\n\n" % (len(lines), self.total))
                for line in lines:
                    fh.write("[" + line.stamp() + "] " + line.text + "\n")
        except OSError as e:
            return None, str(e)
        return path, ""

    def sample_lines(self):
        """Early traffic, used to choose a profile."""
        return list(self._first_lines)

    def summary(self):
        now = time.time()
        return {
            "lines": self.total,
            "dropped": self.dropped,
            "rate": round(self.rate(now), 1),
            "uptime": round(now - self.started, 1),
            "tags": len(self.tags),
            "counters": len(self.counters),
            "errors": self.levels.get("error", 0),
            "fatal": self.levels.get("fatal", 0),
            "stalled": len(self._stalled),
            "consoles": len(self.consoles),
        }


def _hex_rgba(text, fallback):
    text = str(text).lstrip("#")
    if len(text) != 6:
        return fallback
    try:
        return (int(text[0:2], 16) / 255.0, int(text[2:4], 16) / 255.0,
                int(text[4:6], 16) / 255.0, 1.0)
    except ValueError:
        return fallback
