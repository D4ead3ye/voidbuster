"""Headless mode.

Everything the window can do, minus the window - because half of debugging a
console happens over SSH, in a second terminal, or piped into a file while you
go and do something else.
"""

import argparse
import queue
import sys
import time
from pathlib import Path

from . import crash, paths, rules, session, sources

# Beside the exe when frozen, so a session outlives the process. See paths.py.
ROOT = paths.app_dir()
LOG_DIR = ROOT / "logs"
CRASH_DIR = ROOT / "crash_logs"

# Terminal colour, one per severity. Disabled when stdout is redirected, so a
# piped log stays greppable.
ANSI = {
    "fatal": "\x1b[1;31m",
    "error": "\x1b[31m",
    "warn": "\x1b[33m",
    "info": "",
    "debug": "\x1b[2m",
    "trace": "\x1b[2;36m",
}
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def build_parser():
    p = argparse.ArgumentParser(
        prog="voidbuster",
        description="VoidBuster - a log viewer for any Wii U title: UDP/TCP capture, "
                    "parsing, counters and crash dumps.")
    p.add_argument("--gui", action="store_true", help="open the window (default when no flags)")
    p.add_argument("--ports", default="4405",
                   help="UDP ports to listen on, comma separated (default 4405)")
    p.add_argument("--tcp", type=int, default=0, help="also accept a TCP log stream on this port")
    p.add_argument("--host", default="",
                   help="console IP: used by --crashes, and with --only-host to ignore "
                        "traffic from anything else")
    p.add_argument("--only-host", action="store_true",
                   help="drop lines from consoles other than --host")
    p.add_argument("--file", action="append", default=[],
                   help="follow a log file as well (repeatable)")
    p.add_argument("--replay", metavar="PATH",
                   help="read a saved log from the start instead of listening")
    p.add_argument("--profile", help="force a profile by name instead of detecting one")
    p.add_argument("--no-profile", action="store_true", help="automatic parsing only")
    p.add_argument("--list-profiles", action="store_true", help="show the profiles on disk")
    p.add_argument("--record", metavar="DIR", default=str(LOG_DIR),
                   help="where session logs are written (default ./logs)")
    p.add_argument("--no-record", action="store_true", help="do not write a session log")
    p.add_argument("-g", "--grep", default="", help="only show lines matching this regex")
    p.add_argument("--level", default="",
                   help="minimum severity: trace, debug, info, warn, error, fatal")
    p.add_argument("--tag", action="append", default=[], help="only show these tags (repeatable)")
    p.add_argument("--stats", type=float, default=0.0, metavar="SECONDS",
                   help="print a counter summary this often")
    p.add_argument("--sweep", nargs="?", type=float, const=8.0, metavar="SECONDS",
                   help="find which port the console is logging on, then exit")
    p.add_argument("--self-test", action="store_true",
                   help="send a datagram to our own listener to prove the path works")
    p.add_argument("--crashes", nargs="?", const="", metavar="HOST",
                   help="pull crash dumps off the console over FTP, then exit; "
                        "takes the address, or none to use --host")
    p.add_argument("--read-crash", metavar="PATH", help="parse a crash dump and print it")
    p.add_argument("--elf", help="ELF used to turn crash addresses into function names")
    p.add_argument("--offset", default="0",
                   help="subtract this from addresses before symbolising (hex ok)")
    p.add_argument("--quiet", action="store_true", help="counters only, no lines")
    p.add_argument("--no-color", action="store_true")
    return p


LEVEL_ORDER = ["trace", "debug", "info", "warn", "error", "fatal"]


def wanted_levels(minimum):
    if not minimum:
        return None
    minimum = minimum.lower()
    if minimum not in LEVEL_ORDER:
        return None
    return set(LEVEL_ORDER[LEVEL_ORDER.index(minimum):])


def choose_profile(args, sess, profiles, base, sample, log):
    """Pick a profile once there is enough traffic to recognise one.

    Deferred rather than done at startup because the whole point is to
    recognise the game from what it says, and at startup it has not said
    anything yet.
    """
    if args.no_profile or sess.profile not in (None, rules.EMPTY, base):
        return
    hit = rules.pick(profiles, sample)
    if hit:
        sess.profile = rules.combine(base, hit)
        log("-- profile: " + hit.name + " --")


def run(argv=None):
    args = build_parser().parse_args(argv)
    color = sys.stdout.isatty() and not args.no_color

    if args.list_profiles:
        base = rules.load_base()
        profiles, problems = rules.load_all()
        print("base:", len(base.rules), "rules")
        for prof in profiles:
            print("  %-14s %2d rules  %s" % (prof.name, len(prof.rules), prof.about[:70]))
        for bad in problems:
            print("  !! " + bad)
        return 0

    if args.sweep is not None:
        ports = sources.SWEEP_PORTS
        print("sweeping UDP " + ", ".join(str(p) for p in ports) +
              " for %.0fs - start the game now" % args.sweep)
        found = sources.sweep(ports, args.sweep)
        busy = found.pop("_busy", [])
        if busy:
            print("could not bind " + ", ".join(str(p) for p in busy) +
                  " - stop the other listener and sweep again")
        if not found:
            print("nothing heard. See the diagnostics in README.md - the usual cause is "
                  "that the console has no logging module installed.")
            return 1
        for port in sorted(found):
            e = found[port]
            print("udp/%-5d %4d packets  %6d bytes  from %s" %
                  (port, e["packets"], e["bytes"], ", ".join(sorted(e["hosts"]))))
            if e["sample"]:
                print("          " + e["sample"])
        return 0

    if args.crashes is not None:
        target = args.crashes or args.host
        if not target:
            print("--crashes needs an address, either after it or via --host",
                  file=sys.stderr)
            return 2
        got, msg = crash.fetch(target, dest=CRASH_DIR)
        print(msg)
        for path in got:
            print("  " + str(path))
        return 0 if got or "new of" in msg else 1

    if args.read_crash:
        dump = crash.parse_file(args.read_crash)
        print(dump.headline())
        if dump.spr:
            print("  " + "  ".join(k + "=" + v for k, v in sorted(dump.spr.items())))
        if dump.modules:
            print("  modules: " + ", ".join(dump.modules[:12]))
        addrs = dump.code_addresses()
        print("  code addresses: " + (", ".join("0x%08x" % a for a in addrs[:20]) or "none"))
        if args.elf and addrs:
            offset = int(args.offset, 16) if args.offset.lower().startswith("0x") else int(args.offset)
            names, err = crash.symbolize(args.elf, addrs, offset=offset)
            if err:
                print("  addr2line: " + err)
            for addr in addrs:
                if names.get(addr):
                    print("  0x%08x  %s" % (addr, names[addr]))
        return 0

    # ------------------------------------------------------------- capture

    q = queue.Queue()
    base = rules.load_base()
    profiles, problems = rules.load_all()
    for bad in problems:
        print("!! profile " + bad, file=sys.stderr)

    forced = None
    if args.profile:
        forced = next((p for p in profiles if p.name == args.profile), None)
        if forced is None:
            print("no profile named " + args.profile, file=sys.stderr)
            return 2

    # Start on the base profile alone; the game one is chosen later, once the
    # traffic has said enough to be recognised.
    profile = None if args.no_profile else base
    if forced:
        profile = rules.combine(base, forced)

    sess = session.Session(
        record_dir=None if args.no_record else args.record,
        profile=profile,
        title=(forced.name if forced else "session"))

    started = []
    if args.replay:
        started.append(sources.FileSource(q, args.replay, from_start=True, follow=False))
    else:
        ports = [int(p) for p in args.ports.split(",") if p.strip()]
        started.append(sources.UdpSource(q, ports))
        if args.tcp:
            started.append(sources.TcpSource(q, args.tcp))
    for path in args.file:
        started.append(sources.FileSource(q, path))
    for src in started:
        src.start()

    if args.self_test:
        time.sleep(0.3)
        ok, msg = sources.loopback_test(int(args.ports.split(",")[0]))
        print(("self-test " + ("sent: " if ok else "failed: ")) + msg)

    levels = wanted_levels(args.level)
    tags = set(t.lower() for t in args.tag) or None
    grep = None
    if args.grep:
        import re
        try:
            grep = re.compile(args.grep, re.I)
        except re.error as e:
            print("bad regex: " + str(e), file=sys.stderr)
            return 2

    if sess.record_path:
        print("recording to " + str(sess.record_path))

    next_stats = time.time() + args.stats if args.stats else 0.0
    try:
        while True:
            try:
                event = q.get(timeout=0.25)
            except queue.Empty:
                event = None
            if event is not None and _from_console(event, args):
                line = sess.add(event)
                if len(sess.sample_lines()) in (40, 120, 400):
                    choose_profile(args, sess, profiles, base, sess.sample_lines(),
                                   lambda s: print(DIM + s + RESET if color else s))
                if not args.quiet and _show(line, levels, tags, grep):
                    _print_line(line, color)
            now = time.time()
            for at, tag, quiet, count in sess.check_stalls(now):
                msg = "-- stall: %s quiet for %.1fs after %d lines --" % (tag, quiet, count)
                print((ANSI["warn"] + msg + RESET) if color else msg)
            if next_stats and now >= next_stats:
                next_stats = now + args.stats
                _print_stats(sess, color)
            sess.flush()
            # A replay ends when the file does. A live capture never ends on
            # its own, which is the behaviour you want when the console is the
            # thing that stops.
            if event is None and all(getattr(s, "done", False) for s in started):
                break
    except KeyboardInterrupt:
        print("")
    finally:
        for src in started:
            src.stop()
        _print_stats(sess, color)
        if sess.crashes:
            path, err = sess.snapshot_crash(CRASH_DIR)
            if path:
                print("crash context written to " + str(path))
        sess.close()
    return 0


def _from_console(event, args):
    """Console filter, matching the window's "only this address" checkbox.

    Our own notices carry no origin and are always kept - they are what
    explains an otherwise empty session.
    """
    if not args.only_host or not args.host or not event.origin:
        return True
    return event.origin.startswith(args.host + ":")


def _show(line, levels, tags, grep):
    if levels and line.level not in levels:
        return False
    if tags and not (set(line.tags or ("(untagged)",)) & tags):
        return False
    if grep and not grep.search(line.text):
        return False
    return True


def _print_line(line, color):
    if not color:
        print("[" + line.stamp() + "] " + line.text)
        return
    print(DIM + "[" + line.stamp() + "]" + RESET + " " +
          ANSI.get(line.level, "") + line.text + RESET)


def _print_stats(sess, color):
    s = sess.summary()
    head = ("%(lines)d lines  %(rate).1f/s  %(tags)d tags  %(errors)d errors  "
            "%(fatal)d fatal" % s)
    print((DIM + "-- " + head + " --" + RESET) if color else "-- " + head + " --")
    watch = sess.profile.watch if sess.profile else []
    names = [w for w in watch if w in sess.counters]
    names += [n for n, c in sorted(sess.counters.items()) if c.rule and n not in names]
    names += [n for n in sorted(sess.counters) if n not in names]
    row = []
    for name in names[:16]:
        c = sess.counters[name]
        row.append(name + "=" + str(c.value))
    if row:
        print("   " + "  ".join(row))
