"""Crash dumps: fetching them off the console, reading them, naming addresses.

When a title dies hard the UDP stream stops mid-sentence and the useful evidence
is not in it - it is in the dump the console wrote to the SD card. On Aroma that
dump is a *directory* of ring chunks rather than a file, which is why a fetch
that lists one folder and keeps the `.txt` files comes back empty. See
syslog.py for the ring; this module is what to do with it once it is in order.

The parsing here is deliberately shape-based rather than format-based. Crash
logger output has changed between Aroma releases and homebrew writes its own
variants, so anything that insisted on one layout would break on the next one.
What does not change is that registers look like registers, stack frames look
like stack frames, and addresses look like addresses.
"""

import ftplib
import re
import subprocess
from pathlib import Path

from . import syslog

# Where crash dumps land on the SD card, across the setups that write them.
CRASH_DIRS = (
    "/fs/vol/external01/wiiu/crash_logs",
    "/wiiu/crash_logs",
    "/sd/wiiu/crash_logs",
    "/fs/vol/external01/wiiu/logs",
    "/wiiu/logs",
)

GPR = re.compile(r"\br(\d{1,2})\s*[:=]?\s*(?:0x)?([0-9a-fA-F]{8})\b")
SPR = re.compile(r"\b(SRR0|SRR1|LR|CTR|XER|CR|MSR|DAR|DSISR|TBR|GQR\d)\s*[:=]?\s*"
                 r"(?:0x)?([0-9a-fA-F]{8})\b", re.I)
ADDR = re.compile(r"\b(?:0x)?((?:0[0-9a-fA-F]|1[0-9a-fA-F]|e[0-9a-fA-F]|f[0-9a-fA-F])"
                  r"[0-9a-fA-F]{6})\b")
MODULE = re.compile(r"\b([A-Za-z0-9_.\-]+\.(?:rpl|rpx|wms|wps))\b", re.I)
EXC_TYPE = re.compile(r"(DSI|ISI|Program|Alignment|Machine\s*Check|Decrementer|"
                      r"External|System\s*Call|Trace|Breakpoint)\s*(?:exception|interrupt)?",
                      re.I)
# What Cafe OS actually prints, which names the core, the faulting address and
# the address that was touched - more useful than the exception class alone.
CAFE_EXC = re.compile(
    r"(Core\d)\s*:\s*(.+?)\s+at\s+0x([0-9a-fA-F]{8})\s*\(from (\w+)\)"
    r"(?:\s+invalid access of\s+0x([0-9a-fA-F]{8}))?", re.I)
# A stack row: "0x10ae3cd0:   0x10ae3cd8    0x0c9c1040 safe|_localeconv_r+0x10"
FRAME = re.compile(
    r"0x([0-9a-fA-F]{8})\s*:\s+0x([0-9a-fA-F]{8})\s+0x([0-9a-fA-F]{8})"
    r"(?:\s+([\w.\-]+)\|([A-Za-z_][\w.:<>~]*)(?:\+0x([0-9a-fA-F]+))?)?", re.M)
# The console resolves some addresses itself, in registers as well as frames.
ANNOTATED = re.compile(
    r"0x([0-9a-fA-F]{8})\s+([\w.\-]+)\|([A-Za-z_][\w.:<>~]*)(?:\+0x([0-9a-fA-F]+))?")

# Homebrew RPX code is loaded here on this console. An address in this window
# belongs to the game and is worth handing to addr2line; anything in the
# 0x1nnnnnnn or 0xe/0xf ranges is an OS library and will not resolve.
RPX_BASE = 0x02000000
RPX_TOP = 0x10000000


class Frame:
    """One row of the stack trace."""

    __slots__ = ("sp", "back", "lr", "module", "symbol", "offset")

    def __init__(self, sp, back, lr, module="", symbol="", offset=""):
        self.sp, self.back, self.lr = sp, back, lr
        self.module = module or ""
        self.symbol = symbol or ""
        self.offset = offset or ""

    def resolved(self):
        return bool(self.symbol)

    def label(self):
        if not self.symbol:
            return ""
        text = self.module + "|" + self.symbol if self.module else self.symbol
        return text + ("+0x" + self.offset if self.offset else "")


class Dump:
    """One crash dump, read for what can be read without knowing the layout."""

    def __init__(self, text, name="", info=None):
        self.name = name
        self.text = text
        self.info = info or {}
        self.gpr = {}
        self.spr = {}
        self.modules = []
        self.addresses = []
        self.frames = []
        self.symbols = {}
        self.exception = ""
        self.core = ""
        self.fault_at = ""
        self.fault_from = ""
        self.bad_access = ""
        self._parse()

    # The exception is always at the end, and a reassembled ring is megabytes
    # of ordinary gameplay logging before it. Parsing only the tail keeps this
    # fast and, more importantly, stops a register-shaped line from an hour
    # earlier being read as part of the crash.
    TAIL_CHARS = 20000

    def _parse(self):
        tail = self.text[-self.TAIL_CHARS:] if len(self.text) > self.TAIL_CHARS else self.text
        m = CAFE_EXC.search(tail)
        if m:
            self.core = m[1]
            self.exception = m[2].strip()
            self.fault_at = m[3].lower()
            self.fault_from = m[4].upper()
            self.bad_access = (m[5] or "").lower()
        else:
            e = EXC_TYPE.search(tail)
            if e:
                self.exception = re.sub(r"\s+", " ", e.group(0)).strip()
        for num, val in GPR.findall(tail):
            self.gpr.setdefault("r" + num, val.lower())
        for name, val in SPR.findall(tail):
            self.spr.setdefault(name.upper(), val.lower())
        for addr, module, symbol, off in ANNOTATED.findall(tail):
            self.symbols.setdefault(int(addr, 16),
                                    (module + "|" if module else "") + symbol +
                                    ("+0x" + off if off else ""))
        for sp, back, lr, module, symbol, off in FRAME.findall(tail):
            self.frames.append(Frame(int(sp, 16), int(back, 16), int(lr, 16),
                                     module, symbol, off))
        seen = set()
        for name in MODULE.findall(tail):
            low = name.lower()
            if low not in seen:
                seen.add(low)
                self.modules.append(name)
        for val in ADDR.findall(tail):
            addr = int(val, 16)
            if addr not in self.addresses:
                self.addresses.append(addr)

    def stack(self):
        """Frames with the terminating null row dropped."""
        return [f for f in self.frames if f.lr]

    def code_addresses(self):
        """Addresses plausibly inside the game's own RPX.

        Stack frames come first and in stack order, because that is the thing
        you want named; the loose sweep of the text is the fallback.
        """
        out = []
        for f in self.stack():
            if RPX_BASE <= f.lr < RPX_TOP and f.lr not in out:
                out.append(f.lr)
        for key in ("SRR0", "LR"):
            val = self.spr.get(key)
            if val:
                addr = int(val, 16)
                if RPX_BASE <= addr < RPX_TOP and addr not in out:
                    out.insert(0, addr)
        for addr in self.addresses:
            if RPX_BASE <= addr < RPX_TOP and addr not in out:
                out.append(addr)
        return out

    def headline(self):
        bits = []
        if self.core:
            bits.append(self.core)
        bits.append(self.exception or "crash")
        if self.fault_at:
            at = "at 0x" + self.fault_at
            named = self.symbols.get(int(self.fault_at, 16))
            bits.append(at + ("  " + named if named else ""))
        if self.bad_access:
            bits.append("touching 0x" + self.bad_access)
        return "  ".join(bits)


def parse_file(path):
    path = Path(path)
    return Dump(path.read_text(encoding="utf-8", errors="replace"), path.name)


def parse_dump_dir(directory):
    """A chunked Aroma dump directory, reassembled and then read."""
    directory = Path(directory)
    text, info = syslog.assemble(directory)
    joined = "\n".join(syslog.to_lines(text))
    return Dump(joined, directory.name, info)


def open_any(path):
    """Read whichever kind of dump `path` is - directory or single file."""
    path = Path(path)
    if path.is_dir():
        return parse_dump_dir(path)
    return parse_file(path)


def local_dumps(root):
    """Everything under `root` that can be opened as a dump, newest last."""
    root = Path(root)
    if not root.is_dir():
        return []
    out = list(syslog.find_dumps(root))
    known = {str(p) for p in out}
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix.lower() in (".txt", ".log", ".md") \
                and str(path) not in known:
            out.append(path)
    return out


# ------------------------------------------------------------------- console


def _entries(ftp, path):
    """(name, is_directory) for one remote folder.

    MLSD gives the type outright; where it is missing, the only portable test
    is to try to enter it. Slow, but a crash-log folder holds a handful of
    entries, not thousands.
    """
    try:
        return [(name, facts.get("type") == "dir")
                for name, facts in ftp.mlsd(path)
                if name not in (".", "..")]
    except ftplib.all_errors:
        pass
    try:
        names = [n.rsplit("/", 1)[-1] for n in ftp.nlst(path)]
    except ftplib.all_errors:
        return []
    out = []
    here = ftp.pwd()
    for name in names:
        if name in (".", ".."):
            continue
        try:
            ftp.cwd(path.rstrip("/") + "/" + name)
            out.append((name, True))
            ftp.cwd(here)
        except ftplib.all_errors:
            out.append((name, False))
    return out


def _pull_dir(ftp, remote, local, budget):
    """Download one remote folder. Returns how many files arrived."""
    try:
        local.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    got = 0
    for name, is_dir in _entries(ftp, remote):
        if budget[0] <= 0:
            break
        child_remote = remote.rstrip("/") + "/" + name
        child_local = local / name
        if is_dir:
            got += _pull_dir(ftp, child_remote, child_local, budget)
            continue
        if child_local.exists():
            continue
        try:
            with open(child_local, "wb") as fh:
                ftp.retrbinary("RETR " + child_remote, fh.write)
            budget[0] -= 1
            got += 1
        except ftplib.all_errors:
            try:
                child_local.unlink()
            except OSError:
                pass
    return got


def fetch(host, user="", password="", dest=None, timeout=8.0, limit=40, max_files=1200):
    """Pull crash dumps off the console over FTP.

    Recurses, because on Aroma a dump *is* a directory - a hundred chunk files
    and a meta.bin - and the old flat listing found nothing to download. Only
    files not already present are fetched, so this is safe to call on a timer
    while a session runs.

    Returns (paths, message), where a path may be a dump directory or a file.
    """
    dest = Path(dest) if dest else Path.cwd() / "crash_logs"
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, 21, timeout=timeout)
        ftp.login(user or "anonymous", password or "")
    except ftplib.all_errors as e:
        return [], "ftp: " + str(e)

    root = ""
    for candidate in CRASH_DIRS:
        try:
            ftp.cwd(candidate)
            root = candidate
            break
        except ftplib.all_errors:
            continue
    if not root:
        ftp.close()
        return [], ("no crash_logs directory on the console (looked in " +
                    ", ".join(CRASH_DIRS) + ")")

    entries = _entries(ftp, root)
    if not entries:
        ftp.close()
        return [], root + ": empty"

    # Newest last by name: Aroma names a dump for the time it was written.
    dirs = sorted(n for n, is_dir in entries if is_dir)[-limit:]
    files = sorted(n for n, is_dir in entries if not is_dir
                   and n.lower().endswith((".txt", ".log", ".md")))[-limit:]

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        ftp.close()
        return [], str(e)

    budget = [max_files]
    got = []
    for name in dirs:
        local = dest / name
        before = len(list(local.rglob("*"))) if local.exists() else 0
        count = _pull_dir(ftp, root + "/" + name, local, budget)
        if count or before:
            got.append(local)
    for name in files:
        local = dest / name
        if local.exists():
            continue
        try:
            with open(local, "wb") as fh:
                ftp.retrbinary("RETR " + root + "/" + name, fh.write)
            got.append(local)
        except ftplib.all_errors:
            try:
                local.unlink()
            except OSError:
                pass
    ftp.close()
    fresh = max_files - budget[0]
    return got, ("%s: %d dump folder(s), %d file(s) downloaded"
                 % (root, len(dirs), fresh))


# -------------------------------------------------------------- symbolisation


def find_addr2line(devkitpro=r"C:\devkitPro"):
    for name in ("powerpc-eabi-addr2line.exe", "powerpc-eabi-addr2line"):
        cand = Path(devkitpro) / "devkitPPC" / "bin" / name
        if cand.exists():
            return str(cand)
    return ""


def symbolize(elf, addresses, tool="", offset=0):
    """Map addresses to function and source line via addr2line.

    `offset` is subtracted first, for the case where the running image was
    loaded somewhere other than where it was linked. Zero is right for an
    ordinary homebrew RPX built from the ELF being passed in.
    """
    tool = tool or find_addr2line()
    if not tool:
        return {}, "powerpc-eabi-addr2line not found (set the devkitPro path)"
    if not Path(elf).exists():
        return {}, "no such ELF: " + str(elf)
    args = [tool, "-e", str(elf), "-f", "-C", "-p"]
    args += ["0x%08x" % (a - offset) for a in addresses]
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return {}, str(e)
    out = {}
    for addr, line in zip(addresses, res.stdout.splitlines()):
        line = line.strip()
        # addr2line says "??" for anything it cannot place; showing that is
        # more useful than hiding the address entirely.
        out[addr] = line if line and not line.startswith("??") else ""
    return out, res.stderr.strip()
