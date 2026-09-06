"""The window.

Five tabs, in the order you actually need them when something is wrong:

  Live      the stream, filtered
  Counters  the numbers the stream contained, whether or not anyone wrote a
            rule for them
  Signals   which subsystems are talking, how fast, and which have gone quiet -
            the view that finds a freeze
  Crashes   dumps pulled off the console, with addresses turned into names
  Setup     ports, profile, and the checklist for when nothing is arriving

Drawn with VertexUI so it matches the other tools in this workspace.
"""

import math
import queue
import threading
import time
from pathlib import Path

from imgui_bundle import ImVec2, ImVec4, hello_imgui, imgui

import vertexui as vui

from . import crash, look, paths, rules, session, sources

# Beside the exe when frozen, so a session outlives the process. See paths.py.
ROOT = paths.app_dir()
LOG_DIR = ROOT / "logs"
CRASH_DIR = ROOT / "crash_logs"
SETTINGS = ROOT / "voidbuster.json"

LEVELS = ["fatal", "error", "warn", "info", "debug", "trace"]
# How fast text is allowed to move past your eyes, in pixels per second. This
# is the control that actually decides whether a live log is readable: easing
# alone moves faster the further behind it is, which is precisely the case
# where you most want it slow. A row is about 20px, so "calm" is roughly seven
# lines a second.
GLIDE = [("calm", 140.0), ("steady", 260.0), ("quick", 520.0), ("snap", 0.0)]
GLIDE_NAMES = [name for name, _ in GLIDE]
# Lines drawn per frame. The buffer holds far more; the window only ever shows
# a tail, and clipping here is what keeps the UI responsive on a busy log.
DRAW_LIMIT = 700


def _load_settings():
    import json
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_settings(data):
    import json
    try:
        SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


class App:
    def __init__(self):
        self.settings = _load_settings()
        self.queue = queue.Queue()
        self.base = rules.load_base()
        self.profiles, self.profile_problems = rules.load_all()
        self.forced = self.settings.get("profile", "")
        self.sess = None
        self.sources = []
        self.tab = 0
        self.message = ""
        self.message_at = 0.0
        self.filter_text = ""
        self.filter_regex = False
        self.filter_levels = set()
        self.filter_tags = set()
        self.follow = True
        self.paused = False
        self.paused_at = 0
        # Scroll state. `stick` is whether the view is currently riding the
        # bottom; `commanded` is where we put the scrollbar last frame, which
        # is how a user grabbing it is told apart from the content growing.
        self.stick = True
        self.commanded = None
        self.gliding = False
        self.behind = 0.0
        self.sweep_result = None
        self.sweep_left = 0.0
        self.sweeping = False
        self.dumps = []
        self.dump_index = -1
        self.visible = []
        self.symbols = {}
        self.symbol_note = ""
        self.new_session()

    # ------------------------------------------------------------- lifecycle

    def new_session(self):
        if self.sess:
            self.sess.close()
        profile = self.base
        forced = next((p for p in self.profiles if p.name == self.forced), None)
        if forced:
            profile = rules.combine(self.base, forced)
        self.sess = session.Session(
            record_dir=None if self.settings.get("no_record") else LOG_DIR,
            profile=profile,
            title=forced.name if forced else "session")
        self.detected = bool(forced)

    def ports(self):
        raw = str(self.settings.get("ports", "4405"))
        out = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return out or [4405]

    def listening(self):
        return any(s.is_alive() and s.running for s in self.sources)

    def start(self):
        if self.listening():
            return
        self.sources = [sources.UdpSource(self.queue, self.ports())]
        tcp = int(self.settings.get("tcp", 0) or 0)
        if tcp:
            self.sources.append(sources.TcpSource(self.queue, tcp))
        for path in self.settings.get("files", []):
            self.sources.append(sources.FileSource(self.queue, path))
        for src in self.sources:
            src.start()

    def stop(self):
        for src in self.sources:
            src.stop()
        self.sources = []
        self.sess.flush()

    def note(self, text):
        self.message = text
        self.message_at = time.time()

    def host(self):
        return str(self.settings.get("host", "")).strip()

    def ignored(self, event):
        """True for traffic from a console we were told not to care about.

        Off by default, because most people have one console and pinning the
        address only costs them a confusing empty window when it changes. On,
        it is what makes two consoles - or a noisy network - workable.
        """
        if not self.settings.get("only_host"):
            return False
        host = self.host()
        # Our own notices carry no origin and must never be filtered out: they
        # are what explains an otherwise empty window.
        if not host or not event.origin:
            return False
        return not event.origin.startswith(host + ":")

    # ---------------------------------------------------------------- ingest

    def pump(self):
        """Drain the queue once per frame.

        Bounded, so a console that suddenly dumps ten thousand lines cannot
        stall the frame it arrives in - the rest is picked up next frame.
        """
        drained = 0
        while drained < 4000:
            try:
                event = self.queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if self.ignored(event):
                continue
            self.sess.add(event)
        if drained:
            self.sess.flush()
            if not self.detected and not self.forced:
                hit = rules.pick(self.profiles, self.sess.sample_lines())
                if hit:
                    self.sess.profile = rules.combine(self.base, hit)
                    self.detected = True
                    self.note("profile detected: " + hit.name)
        self.sess.check_stalls()

    # ------------------------------------------------------------------ draw

    def draw(self):
        t = vui.theme.current()
        self.pump()

        imgui.text_colored(t.accent_bright, "VOIDBUSTER")
        imgui.same_line()
        s = self.sess.summary()
        imgui.text_colored(t.text_dim, "%d lines   %.1f/s   %d tags" %
                           (s["lines"], s["rate"], s["tags"]))
        if s["fatal"]:
            imgui.same_line()
            imgui.text_colored(t.danger, "%d fatal" % s["fatal"])
        elif s["errors"]:
            imgui.same_line()
            imgui.text_colored(t.warn, "%d errors" % s["errors"])
        if self.sess.stalled_tags():
            imgui.same_line()
            imgui.text_colored(t.warn, "stalled: " + ", ".join(self.sess.stalled_tags()[:3]))
        if self.message and time.time() - self.message_at < 6.0:
            imgui.same_line()
            imgui.text_colored(t.ok, self.message)

        self.tab = vui.widgets.tabs(
            "main", ["Live", "Counters", "Signals", "Crashes", "Setup"], self.tab)
        imgui.dummy(ImVec2(0, 6))
        (self.draw_live, self.draw_counters, self.draw_signals,
         self.draw_crashes, self.draw_setup)[self.tab]()

    # ------------------------------------------------------------------ live

    def draw_live(self):
        t = vui.theme.current()
        listening = self.listening()
        vui.widgets.status_pill("STOPPED", "LISTENING", listening, key="lsn")
        imgui.same_line()
        if vui.widgets.button("Stop" if listening else "Listen", width=92.0):
            self.stop() if listening else self.start()
        imgui.same_line()
        if vui.widgets.button("Pause" if not self.paused else "Resume", width=92.0,
                              tooltip="freeze the view; lines keep arriving and recording"):
            self.paused = not self.paused
            self.paused_at = self.sess.total if self.paused else 0
        imgui.same_line()
        if vui.widgets.button("Clear log", tooltip="empty the view; counters and the recording stay"):
            self.sess.lines.clear()
            self.paused_at = 0 if not self.paused else self.sess.total
        imgui.same_line()
        if vui.widgets.button("Reset", tooltip="clear everything - lines, counters, tags, "
                                              "alerts - and start a new recording"):
            self.new_session()
            self.paused = False
            self.paused_at = 0
            self.stick = True
            self.visible = []
            self.note("new session")
        imgui.same_line()
        if vui.widgets.button("Save .txt", tooltip="write what is on screen to logs/*.txt"):
            self.save_visible()
        imgui.same_line()
        if vui.widgets.button("Snapshot", tooltip="the lines around the last fatal one"):
            path, err = self.sess.snapshot_crash(CRASH_DIR)
            self.note(("wrote " + path.name) if path else err)
        imgui.same_line()
        was = self.follow
        self.follow = vui.widgets.checkbox("follow", self.follow)
        if self.follow and not was:
            # Turning follow back on means "take me to the end", not "resume
            # from wherever I had scrolled to".
            self.stick = True
        imgui.same_line()
        imgui.set_next_item_width(110)
        changed, pick = imgui.combo("speed", self.glide_index(), GLIDE_NAMES)
        if changed:
            self.settings["glide"] = GLIDE_NAMES[pick]

        imgui.set_next_item_width(320)
        changed, text = imgui.input_text("filter", self.filter_text)
        if changed:
            self.filter_text = text
        imgui.same_line()
        self.filter_regex = vui.widgets.checkbox("regex", self.filter_regex)
        imgui.same_line()
        for level in LEVELS:
            on = level in self.filter_levels
            col = t.status({"fatal": "error", "error": "error", "warn": "warn"}
                           .get(level, "info"))
            imgui.push_style_color(imgui.Col_.text, col if on else t.text_mute)
            if imgui.small_button(level):
                self.filter_levels.symmetric_difference_update({level})
            imgui.pop_style_color()
            imgui.same_line()
        if self.filter_levels or self.filter_tags:
            if imgui.small_button("reset"):
                self.filter_levels.clear()
                self.filter_tags.clear()
        else:
            imgui.text_colored(t.text_mute, "severity")

        # Tag chips, busiest first - on a strange game these are the map.
        tags = sorted(self.sess.tags.values(), key=lambda s: -s.count)[:14]
        if tags:
            imgui.dummy(ImVec2(0, 2))
            for i, stat in enumerate(tags):
                if i:
                    imgui.same_line()
                on = stat.name in self.filter_tags
                col = ImVec4(*stat.color) if (on or not self.filter_tags) else t.text_mute
                imgui.push_style_color(imgui.Col_.text, col)
                if imgui.small_button(stat.name + " " + str(stat.count)):
                    self.filter_tags.symmetric_difference_update({stat.name})
                imgui.pop_style_color()

        imgui.dummy(ImVec2(0, 4))
        lines, err = self.sess.filtered(
            self.filter_text, self.filter_levels or None,
            self.filter_tags or None, self.filter_regex, limit=DRAW_LIMIT,
            max_n=self.paused_at)
        self.visible = lines
        if err:
            imgui.text_colored(t.danger, err)
        if self.paused:
            behind = self.sess.total - self.paused_at
            imgui.same_line()
            imgui.text_colored(t.warn, "paused - %d lines behind" % behind)
        elif self.behind > 240.0:
            # The speed cap means a console louder than you can read leaves the
            # view trailing. Saying so beats letting it look like a stutter.
            imgui.same_line()
            imgui.text_colored(t.warn, "behind by ~%d lines - filter, pause, or "
                                       "raise speed" % int(self.behind / 20.0))

        show_time = bool(look.get(self.settings, "show_time"))
        show_tags = bool(look.get(self.settings, "show_tags"))
        mono = bool(look.get(self.settings, "log_mono"))
        imgui.begin_child("log", ImVec2(0, 0), True)
        imgui.push_style_var(imgui.StyleVar_.item_spacing,
                             ImVec2(8, look.spacing_px(self.settings)))
        with vui.fonts.use("mono" if mono else "ui"):
            for line in lines:
                if show_time:
                    imgui.text_colored(t.text_mute, line.stamp())
                    imgui.same_line(0, 6)
                tagged = show_tags and line.tags
                if tagged:
                    stat = self.sess.tags.get(line.tags[0])
                    imgui.text_colored(ImVec4(*(stat.color if stat else (0.6, 0.6, 0.6, 1))),
                                       "[" + line.tags[0] + "]")
                    imgui.same_line(0, 4)
                col = {"fatal": t.danger, "error": t.danger, "warn": t.warn,
                       "debug": t.text_dim, "trace": t.text_mute}.get(line.level, t.text)
                imgui.text_colored(col, line.body if tagged else line.text)
        imgui.pop_style_var()
        self.follow_scroll()
        imgui.end_child()

    # Eases in on small gaps so the last line settles rather than arrives.
    SCROLL_EASE = 12.0
    # Past this much of a gap you are not reading along any more, and creeping
    # there at a readable pace would take longer than the console takes to
    # produce the next thousand lines. Jump.
    SNAP_GAP = 2400.0
    # How near the bottom still counts as following along.
    STICK_GAP = 48.0

    def glide_index(self):
        name = str(self.settings.get("glide", "calm"))
        return GLIDE_NAMES.index(name) if name in GLIDE_NAMES else 0

    def glide_speed(self):
        return GLIDE[self.glide_index()][1]

    def follow_scroll(self):
        """Ease toward the bottom instead of snapping to it.

        Snapping is what makes a live log unreadable: the text jumps by however
        many lines happened to land in that frame, so the line you were reading
        is somewhere else by the time your eye gets to it. Easing at a fixed
        rate per second - not per frame - keeps the motion continuous and the
        same whether the window is running at 9fps or 60.
        """
        self.gliding = False
        cur = imgui.get_scroll_y()
        bottom = imgui.get_scroll_max_y()

        # New lines change scroll_max_y, never scroll_y. So a scroll_y that is
        # not where we left it means a person moved it - wheel, scrollbar or
        # keyboard, no need to know which.
        if self.commanded is None or abs(cur - self.commanded) > 2.0:
            self.stick = (bottom - cur) <= self.STICK_GAP
            self.commanded = cur
        # Otherwise keep our own float position rather than reading back the
        # rounded one. ImGui stores scroll in whole pixels, and at a few hundred
        # frames a second the sub-pixel steps below round up every frame, which
        # feeds the rounding back in and quietly doubles the speed cap.
        pos = self.commanded
        gap = bottom - pos
        self.behind = gap if (self.follow and self.stick and not self.paused) else 0.0

        if self.paused or not (self.follow and self.stick) or gap <= 0.5:
            return
        speed = self.glide_speed()
        if gap > self.SNAP_GAP or not speed:
            imgui.set_scroll_y(bottom)
            self.commanded = bottom
            return
        dt = min(imgui.get_io().delta_time, 0.1)
        # Ease, then cap. The ease settles the last few pixels so a line
        # arrives rather than snaps; the cap is what makes this readable, since
        # easing alone moves faster the further behind it is - exactly the case
        # where you most want it slow. Falling behind is what Pause and the
        # filters are for.
        step = min(gap * (1.0 - math.exp(-self.SCROLL_EASE * dt)), speed * dt)
        target = pos + step
        imgui.set_scroll_y(target)
        self.commanded = target
        self.gliding = True

    def save_visible(self):
        bits = []
        if self.filter_text:
            bits.append(("regex " if self.filter_regex else "filter ") + self.filter_text)
        if self.filter_levels:
            bits.append("levels " + ",".join(sorted(self.filter_levels)))
        if self.filter_tags:
            bits.append("tags " + ",".join(sorted(self.filter_tags)))
        path, err = self.sess.save_text(LOG_DIR, self.visible,
                                        note="; ".join(bits) if bits else "")
        self.note(("saved " + path.name) if path else ("save failed: " + err))

    def busy(self):
        """True while there is motion worth rendering at full rate."""
        return self.gliding or (time.time() - self.sess.last_at) < 1.0

    # -------------------------------------------------------------- counters

    def draw_counters(self):
        t = vui.theme.current()
        watch = list(self.sess.profile.watch) if self.sess.profile else []
        names = [w for w in watch if w in self.sess.counters]
        names += sorted(n for n, c in self.sess.counters.items()
                        if c.rule and n not in names)
        names += sorted(n for n in self.sess.counters if n not in names)
        if not names:
            imgui.text_colored(t.text_dim,
                               "No numbers yet. Any key=value, \"340 draws\" or "
                               "\"frame 12\" in the log lands here on its own.")
            return

        imgui.text_colored(t.text_dim, "%d counters, watched ones first" % len(names))
        flags = (imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.row_bg |
                 imgui.TableFlags_.scroll_y | imgui.TableFlags_.sizing_stretch_prop)
        if not imgui.begin_table("counters", 6, flags, ImVec2(0, 0)):
            return
        imgui.table_setup_column("name", imgui.TableColumnFlags_.width_stretch, 2.0)
        imgui.table_setup_column("value", imgui.TableColumnFlags_.width_stretch, 1.2)
        imgui.table_setup_column("min", imgui.TableColumnFlags_.width_stretch, 1.0)
        imgui.table_setup_column("max", imgui.TableColumnFlags_.width_stretch, 1.0)
        imgui.table_setup_column("updates", imgui.TableColumnFlags_.width_stretch, 1.0)
        imgui.table_setup_column("recent", imgui.TableColumnFlags_.width_stretch, 3.0)
        imgui.table_headers_row()
        for name in names:
            c = self.sess.counters[name]
            imgui.table_next_row()
            imgui.table_next_column()
            imgui.text_colored(t.accent_bright if name in watch else t.text, name)
            imgui.table_next_column()
            imgui.text_colored(t.text, _fmt(c.value))
            imgui.table_next_column()
            imgui.text_colored(t.text_dim, _fmt(c.lo))
            imgui.table_next_column()
            imgui.text_colored(t.text_dim, _fmt(c.hi))
            imgui.table_next_column()
            imgui.text_colored(t.text_dim, str(c.updates))
            imgui.table_next_column()
            _sparkline(c.history, 22.0)
        imgui.end_table()

    # --------------------------------------------------------------- signals

    def draw_signals(self):
        t = vui.theme.current()
        now = time.time()
        imgui.text_colored(t.text_dim,
                           "Who is talking, and who stopped. A subsystem going quiet is "
                           "what a freeze looks like from out here.")
        imgui.dummy(ImVec2(0, 6))

        stalled = set(self.sess.stalled_tags())
        stats = sorted(self.sess.tags.values(), key=lambda s: -s.rate(now))
        peak = max([s.rate(now) for s in stats] or [1.0]) or 1.0
        imgui.begin_child("signals", ImVec2(0, -220), True)
        for stat in stats:
            rate = stat.rate(now)
            quiet = stat.quiet_for(now)
            imgui.text_colored(ImVec4(*stat.color), "%-16s" % stat.name[:16])
            imgui.same_line(0, 8)
            _bar(rate / peak, 150.0, ImVec4(*stat.color) if stat.name not in stalled else t.danger)
            imgui.same_line(0, 8)
            imgui.text_colored(t.text_dim, "%6.1f/s  %6d lines" % (rate, stat.count))
            if stat.name in stalled:
                imgui.same_line()
                imgui.text_colored(t.danger, "quiet %.0fs" % quiet)
            elif stat.levels.get("error") or stat.levels.get("fatal"):
                imgui.same_line()
                imgui.text_colored(t.warn, "%d bad" %
                                   (stat.levels.get("error", 0) + stat.levels.get("fatal", 0)))
        imgui.end_child()

        imgui.text_colored(t.text_dim, "alerts")
        imgui.separator()
        imgui.begin_child("alerts", ImVec2(0, 0), True)
        for line in list(self.sess.alerts)[-120:][::-1]:
            imgui.text_colored(t.text_mute, line.stamp())
            imgui.same_line(0, 6)
            imgui.text_colored(t.danger if line.level == "fatal" else t.warn, line.text)
        imgui.end_child()

    # --------------------------------------------------------------- crashes

    def draw_crashes(self):
        t = vui.theme.current()
        host = self.settings.get("host", "192.168.1.110")
        imgui.set_next_item_width(180)
        changed, host = imgui.input_text("console IP", host)
        if changed:
            self.settings["host"] = host
        imgui.same_line()
        if vui.widgets.button("Fetch dumps", tooltip="FTP: sd:/wiiu/crash_logs"):
            got, msg = crash.fetch(host, dest=CRASH_DIR)
            self.note(msg)
            self.reload_dumps()
        imgui.same_line()
        if vui.widgets.button("Rescan folder"):
            self.reload_dumps()

        elf = self.settings.get("elf", "")
        imgui.set_next_item_width(430)
        changed, elf = imgui.input_text("ELF for symbols", elf)
        if changed:
            self.settings["elf"] = elf

        imgui.dummy(ImVec2(0, 6))
        if not self.dumps:
            imgui.text_colored(t.text_dim,
                               "No dumps in " + str(CRASH_DIR) + ". Fetch them from the "
                               "console, or drop files in that folder.")
            self.draw_session_crashes()
            return

        imgui.begin_child("dumplist", ImVec2(300, 0), True)
        for i, path in enumerate(self.dumps):
            if imgui.selectable(path.name, i == self.dump_index)[0]:
                self.dump_index = i
                self.symbols = {}
                self.symbol_note = ""
        imgui.end_child()
        imgui.same_line()
        imgui.begin_child("dumpview", ImVec2(0, 0), True)
        if 0 <= self.dump_index < len(self.dumps):
            self.draw_dump(crash.parse_file(self.dumps[self.dump_index]), elf)
        else:
            imgui.text_colored(t.text_dim, "pick a dump")
        imgui.end_child()

    def draw_dump(self, dump, elf):
        t = vui.theme.current()
        imgui.text_colored(t.danger, dump.headline())
        if dump.spr:
            imgui.text_colored(t.text_dim,
                               "  ".join(k + " " + v for k, v in sorted(dump.spr.items())))
        if dump.modules:
            imgui.text_colored(t.text_dim, "modules: " + ", ".join(dump.modules[:10]))
        addrs = dump.code_addresses()
        imgui.dummy(ImVec2(0, 4))
        if addrs and vui.widgets.button("Symbolise", enabled=bool(elf),
                                        tooltip="powerpc-eabi-addr2line against the ELF above"):
            self.symbols, self.symbol_note = crash.symbolize(elf, addrs)
            if not self.symbols and not self.symbol_note:
                self.symbol_note = "nothing resolved"
        if self.symbol_note:
            imgui.text_colored(t.warn, self.symbol_note)
        for addr in addrs[:60]:
            imgui.text_colored(t.text, "0x%08x" % addr)
            name = self.symbols.get(addr)
            if name:
                imgui.same_line(0, 10)
                imgui.text_colored(t.accent_bright, name)
        imgui.dummy(ImVec2(0, 8))
        imgui.text_colored(t.text_dim, "raw")
        imgui.separator()
        for line in dump.text.splitlines()[:400]:
            imgui.text_colored(t.text_dim, line)

    def draw_session_crashes(self):
        t = vui.theme.current()
        if not self.sess.crashes:
            return
        imgui.dummy(ImVec2(0, 10))
        imgui.text_colored(t.text_dim, "fatal lines in this session")
        imgui.separator()
        for line in self.sess.crashes[-40:]:
            imgui.text_colored(t.danger, line.stamp() + "  " + line.text)

    def reload_dumps(self):
        try:
            self.dumps = sorted(CRASH_DIR.glob("*.txt")) + sorted(CRASH_DIR.glob("*.log"))
        except OSError:
            self.dumps = []
        self.dump_index = 0 if self.dumps else -1

    # ----------------------------------------------------------------- setup

    def draw_setup(self):
        t = vui.theme.current()
        imgui.begin_child("setup", ImVec2(0, 0), False)

        imgui.separator_text("console")
        imgui.set_next_item_width(200)
        changed, host = imgui.input_text("console IP", self.host())
        if changed:
            self.settings["host"] = host.strip()
        imgui.same_line()
        imgui.text_colored(t.text_dim, "used for crash-dump fetch, and for the filter below")
        only = vui.widgets.checkbox("only accept lines from this address",
                                    bool(self.settings.get("only_host")))
        self.settings["only_host"] = only
        if only and not self.host():
            imgui.same_line()
            imgui.text_colored(t.warn, "no address set - filter inactive")
        seen = sorted({o.split(":")[0] for o in self.sess.consoles})
        imgui.text_colored(t.text_dim, "seen so far   " + (", ".join(seen) or "none"))
        # Offered only for addresses that are not already the configured one -
        # a button that sets what is already set is just clutter.
        for ip in [a for a in seen if a != self.host()][:4]:
            imgui.same_line()
            if imgui.small_button("use " + ip):
                self.settings["host"] = ip

        imgui.dummy(ImVec2(0, 8))
        imgui.separator_text("capture")
        imgui.set_next_item_width(200)
        changed, ports = imgui.input_text("UDP ports", str(self.settings.get("ports", "4405")))
        if changed:
            self.settings["ports"] = ports
        imgui.same_line()
        imgui.text_colored(t.text_dim, "comma separated; restart the listener to apply")

        imgui.set_next_item_width(200)
        changed, tcp = imgui.input_text("TCP port", str(self.settings.get("tcp", "0")))
        if changed:
            self.settings["tcp"] = tcp

        imgui.dummy(ImVec2(0, 8))
        imgui.separator_text("profile")
        names = ["(auto-detect)"] + [p.name for p in self.profiles]
        current = names.index(self.forced) if self.forced in names else 0
        changed, current = imgui.combo("profile", current, names)
        if changed:
            self.forced = "" if current == 0 else names[current]
            self.settings["profile"] = self.forced
            self.new_session()
            self.note("profile: " + (self.forced or "auto"))
        if self.sess.profile is not None:
            imgui.same_line()
            imgui.text_colored(t.text_dim, "active: " + self.sess.profile.name +
                               " (%d rules)" % len(self.sess.profile.rules))
        for bad in self.profile_problems:
            imgui.text_colored(t.danger, "profile " + bad)

        imgui.dummy(ImVec2(0, 8))
        self.draw_appearance()

        imgui.dummy(ImVec2(0, 8))
        imgui.separator_text("diagnostics")
        if vui.widgets.button("Port sweep", enabled=not self.sweeping,
                              tooltip="listen across every likely port for 8s"):
            self.start_sweep()
        imgui.same_line()
        if vui.widgets.button("Self-test", tooltip="send a datagram to our own listener"):
            ok, msg = sources.loopback_test(self.ports()[0])
            self.note(("self-test " + msg) if ok else ("self-test failed: " + msg))
        imgui.same_line()
        if vui.widgets.button("Save settings"):
            _save_settings(self.settings)
            self.note("saved " + SETTINGS.name)

        if self.sweeping:
            imgui.text_colored(t.warn, "sweeping... %.0fs left - start the game now"
                               % self.sweep_left)
        if self.sweep_result is not None:
            imgui.dummy(ImVec2(0, 4))
            busy = self.sweep_result.pop("_busy", [])
            if busy:
                imgui.text_colored(t.warn, "could not bind " +
                                   ", ".join(str(p) for p in busy))
            if not self.sweep_result:
                imgui.text_colored(t.danger, "nothing heard on any port")
            for port in sorted(self.sweep_result):
                e = self.sweep_result[port]
                imgui.text_colored(t.ok, "udp/%d  %d packets from %s"
                                   % (port, e["packets"], ", ".join(sorted(e["hosts"]))))
                if e["sample"]:
                    imgui.text_colored(t.text_dim, "    " + e["sample"])

        imgui.dummy(ImVec2(0, 10))
        imgui.text_colored(t.text_dim, "recording   " +
                           (str(self.sess.record_path) if self.sess.record_path else "off"))
        bound = []
        for src in self.sources:
            bound.append(src.kind + (":" + ",".join(str(p) for p in src.bound)
                                     if getattr(src, "bound", None) else ""))
        imgui.text_colored(t.text_dim, "sources     " + (", ".join(bound) or "none"))
        imgui.text_colored(t.text_dim, "consoles    " +
                           (", ".join(sorted(self.sess.consoles)) or "none seen"))

        imgui.dummy(ImVec2(0, 10))
        imgui.text_colored(t.text_dim, "nothing arriving?")
        imgui.separator()
        for step in CHECKLIST:
            imgui.text_colored(t.text_dim, "  - " + step)
        imgui.end_child()

    def draw_appearance(self):
        """Looks and sound. Applied as you change them, saved with everything
        else - picking a theme you cannot see until next launch is no way to
        pick a theme."""
        t = vui.theme.current()
        touched = False
        imgui.separator_text("appearance")

        imgui.set_next_item_width(160)
        changed, pick = imgui.combo("theme", look.theme_index(self.settings),
                                    look.THEME_NAMES)
        if changed:
            self.settings["theme"] = look.THEME_NAMES[pick]
            # A preset carries its own accent; keep an override only while the
            # user actually wants one.
            self.settings["accent"] = ""
            touched = True
        imgui.same_line()
        # A swatch, not three number boxes: clicking it opens the full picker,
        # and the row stays readable next to the theme combo.
        changed, rgb = imgui.color_edit3("accent", look.accent_rgb(self.settings),
                                         imgui.ColorEditFlags_.no_inputs)
        if changed:
            self.settings["accent"] = look.rgb_to_hex(rgb)
            touched = True
        if self.settings.get("accent"):
            imgui.same_line()
            if imgui.small_button("reset accent"):
                self.settings["accent"] = ""
                touched = True

        imgui.set_next_item_width(220)
        changed, scale = imgui.slider_float("text size", float(look.get(self.settings, "font_scale")),
                                            look.FONT_MIN, look.FONT_MAX, "%.2fx")
        if changed:
            self.settings["font_scale"] = round(scale, 2)
            touched = True

        imgui.set_next_item_width(160)
        changed, pick = imgui.combo("row spacing", look.spacing_index(self.settings),
                                    look.SPACING_NAMES)
        if changed:
            self.settings["log_spacing"] = look.SPACING_NAMES[pick]

        self.settings["log_mono"] = vui.widgets.checkbox(
            "monospaced log", bool(look.get(self.settings, "log_mono")))
        imgui.same_line()
        self.settings["show_time"] = vui.widgets.checkbox(
            "timestamps", bool(look.get(self.settings, "show_time")))
        imgui.same_line()
        self.settings["show_tags"] = vui.widgets.checkbox(
            "tag column", bool(look.get(self.settings, "show_tags")))

        imgui.separator_text("sound")
        on = vui.widgets.checkbox("interface sounds",
                                  bool(look.get(self.settings, "sounds")))
        if on != bool(look.get(self.settings, "sounds")):
            self.settings["sounds"] = on
            touched = True
        imgui.same_line()
        imgui.set_next_item_width(200)
        changed, vol = imgui.slider_float("volume", float(look.get(self.settings, "volume")),
                                          0.0, 1.0, "%.2f")
        if changed:
            self.settings["volume"] = round(vol, 2)
            touched = True
        if not on:
            imgui.same_line()
            imgui.text_colored(t.text_dim, "silent")

        if touched:
            look.apply(self.settings)

    def start_sweep(self):
        self.sweeping = True
        self.sweep_result = None

        def worker():
            try:
                self.sweep_result = sources.sweep(
                    sources.SWEEP_PORTS, 8.0,
                    progress=lambda left: setattr(self, "sweep_left", left))
            finally:
                self.sweeping = False

        # The sweep binds the same ports the listener wants, so it has to have
        # them to itself while it runs.
        was = self.listening()
        self.stop()
        threading.Thread(target=lambda: (worker(), was and self.start()), daemon=True).start()


CHECKLIST = [
    "Aroma: sd:/wiiu/environments/aroma/modules/ needs the logging module for "
    "retail titles to log at all (it redirects OSReport).",
    "Homebrew you build yourself: call WHBLogUdpInit() early, then WHBLogPrintf.",
    "Console and PC on the same subnet, and the PC firewall allowing inbound UDP "
    "on the port (Windows blocks it for new apps by default).",
    "Self-test above proves the socket and the view; if that works and the "
    "console does not, the problem is on the console side.",
    "Port sweep finds a title that logs somewhere other than 4405.",
    "No log at all after a hard freeze is normal - fetch the crash dump instead.",
]


def _fmt(value):
    if isinstance(value, float):
        return "%.3f" % value if abs(value) < 1000 else "%.0f" % value
    return str(value)


def _sparkline(history, height):
    """Drawn by hand rather than with plot_lines: this needs to sit inside a
    table cell, size itself to the column and stay legible at 20 pixels."""
    values = [v for v in history if isinstance(v, (int, float))]
    width = max(60.0, imgui.get_content_region_avail().x - 4)
    origin = imgui.get_cursor_screen_pos()
    imgui.dummy(ImVec2(width, height))
    if len(values) < 2:
        return
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    draw = imgui.get_window_draw_list()
    t = vui.theme.current()
    col = imgui.color_convert_float4_to_u32(t.accent)
    step = width / (len(values) - 1)
    prev = None
    for i, v in enumerate(values):
        x = origin.x + i * step
        y = origin.y + height - ((v - lo) / span) * (height - 2) - 1
        if prev is not None:
            draw.add_line(ImVec2(prev[0], prev[1]), ImVec2(x, y), col, 1.4)
        prev = (x, y)


def _bar(fraction, width, color):
    height = 10.0
    origin = imgui.get_cursor_screen_pos()
    imgui.dummy(ImVec2(width, height))
    draw = imgui.get_window_draw_list()
    t = vui.theme.current()
    draw.add_rect_filled(ImVec2(origin.x, origin.y + 1),
                         ImVec2(origin.x + width, origin.y + height - 1),
                         imgui.color_convert_float4_to_u32(t.surface), 2.0)
    fill = max(0.0, min(1.0, fraction)) * width
    if fill > 1.0:
        draw.add_rect_filled(ImVec2(origin.x, origin.y + 1),
                             ImVec2(origin.x + fill, origin.y + height - 1),
                             imgui.color_convert_float4_to_u32(color), 2.0)


def run():
    app = App()
    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = "VoidBuster"
    params.app_window_params.window_geometry.size = (1280, 860)

    def gui():
        app.draw()
        vui.end_frame(params)
        # end_frame drops to the idle frame rate once animations settle. A log
        # still arriving is motion too, and an eased scroll at 9fps stutters
        # worse than the snap it replaced. Idling resumes when the console goes
        # quiet, which is the case that idling was for.
        if app.busy():
            params.fps_idling.enable_idling = False

    params.callbacks.show_gui = gui
    vui.install(params, theme_=look.build_theme(app.settings))
    # Font scale and sound need a live ImGui context, which exists by post_init
    # but not yet here.
    params.callbacks.post_init = lambda: look.apply(app.settings)
    app.reload_dumps()
    app.start()
    try:
        hello_imgui.run(params)
    finally:
        app.stop()
        app.sess.close()
        _save_settings(app.settings)
    return 0
