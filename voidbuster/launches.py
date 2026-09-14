"""Reading a capture as a sequence of runs rather than one undifferentiated log.

A capture left running routinely spans a whole evening: the game is started,
crashes or is quit, started again, eight or ten times over. Every one of those
runs is a separate experiment, and the questions you actually have are per-run
ones - which build was it, did it crash, how far did it get - not questions
about the file as a whole.

Three things are worked out here, all from the line stream and none of them
needing to know which game produced it:

  launches    where one run ends and the next begins
  verdicts    how each run ended: crashed, exited cleanly, or was cut off
  historical  lines that are a replay of an earlier run rather than live now

The last one matters more than it sounds. Ports commonly dump the previous
session's breadcrumbs at startup, and those lines are indistinguishable from
live ones by content - same tags, same format, same everything. The only tell
is that their embedded clock runs behind the clock of the run they appear in.
"""

import re
import time

# A gap in the stream this long means the console stopped talking for a reason,
# and whatever comes after it is a new run. Conservative: normal gameplay lulls
# and loading screens are seconds, not tens of seconds.
LAUNCH_GAP_S = 20.0

# How far an embedded clock has to fall behind the run's high-water mark before
# the line is called a replay. Log lines are not perfectly ordered - a few
# hundred milliseconds of jitter between threads is normal - so this is well
# clear of the noise.
HISTORICAL_LAG_S = 5.0

# Banners that mean "a program just started". Deliberately about shapes common
# to console software rather than any one title.
LAUNCH_PATTERNS = [
    re.compile(r"(?i)Cafe\s*OS\s*SDK\s*Version"),
    re.compile(r"(?i)###\s*Application\s+(?:Start|Launch)"),
    re.compile(r"(?i)(?:starting|booting|launching).{0,30}v\d+\.\d+"),
    re.compile(r"(?i)WHBLogUdpInit"),
]
# Not included, deliberately: a rule of dashes. "--Stack Trace-----" and
# "--Proc15-Core1-----" look exactly like a banner once the timestamp and tag
# are stripped off the front, and matching on it turned a 14-run capture into
# fifteen hundred.

# How a run ended well. "settings flushed" is the shape of it: some last piece
# of teardown that only happens on a deliberate exit.
CLEAN_PATTERNS = [
    re.compile(r"(?i)###\s*Application\s+Exit"),
    re.compile(r"(?i)\bsettings\s+(?:flushed|saved)\b"),
    re.compile(r"(?i)\bshutt?ing\s+down\b|\bclean\s+exit\b|\bgoodbye\b"),
    re.compile(r"(?i)\bexit(?:ing)?\b.{0,12}\bcode\s*[:=]?\s*0\b"),
]

# Ordered: the first to match a run wins, so explicit forms come before loose
# ones, and the value must look like an identifier rather than the next English
# word - otherwise "build date: Feb 4" reports a build called "date".
BUILD_ID = r"((?=[\w.\-]*\d)[0-9A-Za-z][\w.\-]{2,39})"
BUILD_PATTERNS = [
    re.compile(r"(?i)commit\s*[:=]?\s*([0-9a-f]{7,40})"),
    re.compile(r"(?i)(?:git|rev|revision)\s*[:=]?\s*([0-9a-f]{7,40})"),
    re.compile(r"(?i)build\s*(?:id|number)?\s*[:=]\s*" + BUILD_ID),
    re.compile(r"(?i)build\s+(?!date|time|type|host|machine|by)" + BUILD_ID),
    re.compile(r"(?i)version\s*[:=]?\s*(v?\d+\.\d+[\w.\-]*)"),
]

# A time the game stamped into its own message, as opposed to the one the
# transport or the console put on the front. These are the two clocks that
# disagree, sometimes by the better part of an hour.
EMBEDDED_TIME = re.compile(r"\[(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?\]")
LEADING_TIME = re.compile(r"^(\d{1,2})[:;](\d{2})[:;](\d{2})(?:[.,;](\d{1,3}))?")


def _secs(h, m, s, ms=None):
    return int(h) * 3600 + int(m) * 60 + int(s) + (int(ms or 0) / 1000.0)


def clock_of(text):
    """Seconds-of-day the *game* claims, or None.

    Prefers a bracketed time inside the message over one on the front, because
    the front one is usually the console's or the logger's, and it is the
    game's own clock that tells you whether a line is a replay.
    """
    m = EMBEDDED_TIME.search(text)
    if m:
        return _secs(*m.groups())
    m = LEADING_TIME.match(text)
    if m:
        return _secs(*m.groups())
    return None


class Launch:
    """One run of the program, and how it went."""

    __slots__ = ("index", "start_at", "first_n", "last_n", "build", "lines",
                 "fatal", "errors", "warns", "historical", "ended", "end_at",
                 "end_reason", "first_clock", "last_clock")

    def __init__(self, index, at, n):
        self.index = index
        self.start_at = at
        self.first_n = n
        self.last_n = n
        self.build = ""
        self.lines = 0
        self.fatal = 0
        self.errors = 0
        self.warns = 0
        self.historical = 0
        self.ended = False
        self.end_at = 0.0
        self.end_reason = ""
        self.first_clock = None
        self.last_clock = None

    def close(self, reason, at):
        if self.ended:
            return
        self.ended = True
        self.end_reason = reason
        self.end_at = at

    def verdict(self, now=None):
        """One word for how it went, computed rather than stored so a run still
        in progress reports honestly."""
        if self.end_reason:
            return self.end_reason
        if self.fatal:
            return "crashed"
        return "running"

    def duration(self, now=None):
        end = self.end_at or (now or time.time())
        return max(0.0, end - self.start_at)

    def span(self):
        """How long the game thought it ran, by its own clock. Immune to the
        capture being started late, which the wall-clock duration is not."""
        if self.first_clock is None or self.last_clock is None:
            return None
        span = self.last_clock - self.first_clock
        return span + 86400.0 if span < 0 else span


class Tracker:
    """Fed every line in order; annotates it and keeps the run list.

    Deliberately incremental: it has to work on a live stream, and working on a
    replayed file then falls out for free.
    """

    def __init__(self, profile=None, gap=LAUNCH_GAP_S):
        self.launches = []
        self.gap = gap
        self.last_at = 0.0
        self.skews = []
        self.skew = None
        # Some lines carry two clocks at once: one the transport or console put
        # on the front, one the game stamped into the message. Their difference
        # needs no capture clock at all, so it is the only skew that means
        # anything when reading a dump back later.
        self.pair_skews = []
        self.pair_skew = None
        self._hwm = None
        self._current = None
        self.set_profile(profile)

    def set_profile(self, profile):
        """A profile may name the patterns for its own game; without one the
        generic banners above are used."""
        self.launch_res = list(LAUNCH_PATTERNS)
        self.clean_res = list(CLEAN_PATTERNS)
        self.build_res = list(BUILD_PATTERNS)
        if profile is None:
            return
        for field, target in (("launch", self.launch_res),
                              ("clean_exit", self.clean_res),
                              ("build", self.build_res)):
            for pattern in getattr(profile, field, []) or []:
                try:
                    target.insert(0, re.compile(pattern))
                except re.error:
                    continue

    @property
    def current(self):
        return self._current

    def _start(self, at, n, reason=""):
        if self._current is not None and not self._current.ended:
            self._current.close(self._current.end_reason or
                                ("crashed" if self._current.fatal else "cut off"), at)
        launch = Launch(len(self.launches) + 1, at, n)
        self.launches.append(launch)
        self._current = launch
        self._hwm = None
        return launch

    def feed(self, line):
        """Annotate one parsed line. Sets `line.launch` and `line.historical`."""
        at = line.at
        gap = at - self.last_at if self.last_at else 0.0
        self.last_at = at

        banner = any(p.search(line.text) for p in self.launch_res)
        # A silent console is the one launch signal that needs no cooperation
        # from the program being debugged.
        quiet = self._current is not None and gap >= self.gap
        if self._current is None or banner or quiet:
            why = "banner" if banner else ("gap %.0fs" % gap if quiet else "first line")
            launch = self._start(at, line.n)
            launch.end_reason = ""
            self._note_start(launch, why)
        launch = self._current

        lead = LEADING_TIME.match(getattr(line, "clock", "") or "")
        inner = EMBEDDED_TIME.search(line.text)
        if lead and inner:
            delta = _secs(*lead.groups()) - _secs(*inner.groups())
            if -86400 < delta < 86400:
                self.pair_skews.append(delta)
                if len(self.pair_skews) > 400:
                    del self.pair_skews[:200]
                mid = sorted(self.pair_skews)
                self.pair_skew = mid[len(mid) // 2]

        clock = clock_of(line.text)
        historical = False
        if clock is not None:
            if self._hwm is None:
                self._hwm = clock
                launch.first_clock = clock
            elif clock < self._hwm - HISTORICAL_LAG_S:
                historical = True
            else:
                self._hwm = max(self._hwm, clock)
            if not historical:
                launch.last_clock = clock
            # Capture clock against the game's own, which is the offset that
            # has to be reconciled by hand otherwise.
            wall = time.localtime(at)
            here = wall.tm_hour * 3600 + wall.tm_min * 60 + wall.tm_sec
            delta = here - clock
            if -86400 < delta < 86400:
                self.skews.append(delta)
                if len(self.skews) > 400:
                    del self.skews[:200]
                mid = sorted(self.skews)
                self.skew = mid[len(mid) // 2]

        line.launch = launch.index
        line.historical = historical

        launch.lines += 1
        launch.last_n = line.n
        if historical:
            launch.historical += 1
        elif line.level == "fatal":
            launch.fatal += 1
            launch.close("crashed", at)
        elif line.level == "error":
            launch.errors += 1
        elif line.level == "warn":
            launch.warns += 1

        if not historical and not launch.build:
            for pattern in self.build_res:
                m = pattern.search(line.text)
                if m:
                    launch.build = m.group(1)
                    break
        if not historical and any(p.search(line.text) for p in self.clean_res):
            launch.close("clean exit", at)
        return line

    def _note_start(self, launch, why):
        launch.end_reason = ""
        self._start_reason = why

    def summary(self, now=None):
        """The table: one row per run."""
        now = now or time.time()
        rows = []
        for launch in self.launches:
            rows.append({
                "n": launch.index,
                "start": time.strftime("%H:%M:%S", time.localtime(launch.start_at)),
                "build": launch.build or "-",
                "lines": launch.lines,
                "historical": launch.historical,
                "errors": launch.errors,
                "fatal": launch.fatal,
                "verdict": launch.verdict(now),
                "duration": launch.duration(now),
                "span": launch.span(),
            })
        return rows

    def skew_text(self):
        """The offset, named so it is obvious which two clocks are meant."""
        if self.pair_skew is not None and abs(self.pair_skew) >= 1:
            return "console clock " + _offset(self.pair_skew) + " ahead of the game's"
        if self.skew is not None and abs(self.skew) >= 1:
            return "capture clock " + _offset(self.skew) + " ahead of the game's"
        return ""


def _offset(secs):
    sign = "-" if secs < 0 else "+"
    secs = abs(int(secs))
    if secs < 90:
        return "%s%ds" % (sign, secs)
    return "%s%dm%02ds" % (sign, secs // 60, secs % 60)
