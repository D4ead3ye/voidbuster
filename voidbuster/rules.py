"""Optional per-game precision on top of the automatic parsing.

A profile is a JSON file naming regexes worth treating specially. Nothing here
is required - parse.py already produces tags, levels and counters for a title
nobody has ever profiled. A profile exists to say the things the text cannot:
that this line means a frame boundary, that this one is fatal even though it
reads calmly, that this counter is the one to watch.

Profiles are picked automatically by scoring their `match` patterns against the
first lines of a session, so plugging in a different game generally just works.
"""

import json
import re
import time
from pathlib import Path

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


class Rule:
    """One pattern and what it means.

    JSON shape:
        {
          "re":    "\\[gfx\\] frame (?P<frame>\\d+) ([0-9]+) draws",
          "label": "frame",          # what to call it in the UI
          "level": "warn",           # override the guessed severity
          "count": "frames",         # bump this counter on every match
          "set":   {"draws": 2},     # counter <- numbered capture group
          "alert": true,             # surface it in the alerts pane
          "color": "#5ad0ff"
        }
    Named capture groups become counters automatically, which is usually all
    you need; `set` is for patterns already written with numbered groups.
    """

    def __init__(self, spec):
        self.raw = dict(spec)
        self.pattern = re.compile(spec["re"])
        self.label = spec.get("label", "")
        self.level = spec.get("level", "")
        self.count = spec.get("count", "")
        self.set = spec.get("set", {})
        self.alert = bool(spec.get("alert", False))
        self.color = spec.get("color", "")
        self.hits = 0

    def apply(self, line, out):
        m = self.pattern.search(line)
        if not m:
            return None
        self.hits += 1
        for name, val in (m.groupdict() or {}).items():
            if val is None:
                continue
            out[name.lower()] = _num(val)
        for name, group in self.set.items():
            try:
                out[name.lower()] = _num(m.group(int(group) if str(group).isdigit() else group))
            except (IndexError, ValueError, TypeError):
                continue
        if self.count:
            out[self.count.lower()] = out.get(self.count.lower(), 0) + 1
        return m


def _num(text):
    try:
        return float(text) if "." in str(text) else int(text)
    except (TypeError, ValueError):
        return text


class Profile:
    """A named set of rules, plus how to recognise the game it belongs to."""

    def __init__(self, data, path=None):
        self.path = Path(path) if path else None
        self.name = data.get("name") or (self.path.stem if self.path else "unnamed")
        self.about = data.get("about", "")
        self.match = [re.compile(p, re.I) for p in data.get("match", [])]
        self.rules = [Rule(r) for r in data.get("rules", [])]
        self.tags = {k.lower(): v for k, v in data.get("tags", {}).items()}
        self.watch = [w.lower() for w in data.get("watch", [])]
        self.mtime = self.path.stat().st_mtime if self.path and self.path.exists() else 0.0

    def score(self, lines):
        """How much this profile looks like it belongs to the traffic seen.

        A profile with no `match` patterns can never win automatically; it has
        to be chosen by hand. That keeps a catch-all profile from claiming
        every session.
        """
        if not self.match:
            return 0
        hits = 0
        for line in lines:
            for pat in self.match:
                if pat.search(line):
                    hits += 1
                    break
        return hits

    def apply(self, line):
        """Returns (counters, level_override, alert, labels)."""
        out = {}
        level = ""
        alert = False
        labels = []
        for rule in self.rules:
            if rule.apply(line, out) is None:
                continue
            if rule.level:
                level = rule.level
            if rule.alert:
                alert = True
            if rule.label:
                labels.append(rule.label)
        return out, level, alert, labels

    def stale(self):
        """True when the file changed on disk. Editing a profile while the
        console is running is the normal way to work on one, so the GUI reloads
        rather than making you restart and lose the session."""
        if not self.path or not self.path.exists():
            return False
        try:
            return self.path.stat().st_mtime > self.mtime
        except OSError:
            return False


EMPTY = Profile({"name": "generic",
                 "about": "no rules - everything comes from the automatic parser"})


BASE_NAME = "_base.json"


def _read(path):
    return Profile(json.loads(Path(path).read_text(encoding="utf-8")), path)


def load_all(directory=PROFILE_DIR):
    """Every game profile on disk. A broken one is reported, not fatal: a typo
    in a profile should not take the logger down mid-session.

    Files starting with an underscore are building blocks (see load_base), not
    games, so they are not offered as something to select.
    """
    out, problems = [], []
    directory = Path(directory)
    if not directory.is_dir():
        return out, problems
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            out.append(_read(path))
        except (OSError, ValueError, re.error) as e:
            problems.append(path.name + ": " + str(e))
    return out, problems


def load_base(directory=PROFILE_DIR):
    """The always-on profile: console behaviour that is true of every title."""
    path = Path(directory) / BASE_NAME
    if not path.exists():
        return EMPTY
    try:
        return _read(path)
    except (OSError, ValueError, re.error):
        return EMPTY


def combine(base, game):
    """One profile behaving as both.

    Base rules run first so a game profile can override the severity or label
    they set - the specific description of a line should win over the general
    one.
    """
    if game is None:
        return base
    if base is None or base is EMPTY:
        return game
    merged = Profile({"name": game.name, "about": game.about})
    merged.path = game.path
    merged.mtime = game.mtime
    merged.match = list(game.match)
    merged.rules = list(base.rules) + list(game.rules)
    merged.tags = dict(base.tags)
    merged.tags.update(game.tags)
    merged.watch = list(dict.fromkeys(list(game.watch) + list(base.watch)))
    return merged


def pick(profiles, lines, minimum=2):
    """Best-scoring profile, or None when nothing recognises the traffic."""
    best, best_score = None, 0
    for prof in profiles:
        score = prof.score(lines)
        if score > best_score:
            best, best_score = prof, score
    return best if best_score >= minimum else None


def reload(profile):
    """Re-read one profile from disk, keeping the old one if it now fails."""
    if not profile.path:
        return profile, ""
    try:
        fresh = Profile(json.loads(profile.path.read_text(encoding="utf-8")), profile.path)
        fresh.mtime = time.time()
        return fresh, ""
    except (OSError, ValueError, re.error) as e:
        return profile, str(e)
