"""Crash dumps: fetching them off the console, reading them, naming addresses.

When a title dies hard the UDP stream stops mid-sentence and the useful evidence
is not in it - it is in the dump Aroma's crash logger writes to the SD card. So
the logger pulls those over FTP, parses them without assuming an exact layout,
and turns the addresses into function names when an ELF is available.

The parsing here is deliberately shape-based rather than format-based. Crash
logger output has changed between Aroma releases and homebrew writes its own
variants, so anything that insisted on one layout would break on the next one.
What does not change is that registers look like registers and addresses look
like addresses.
"""

import ftplib
import re
import subprocess
from pathlib import Path

# Where crash dumps land on the SD card, across the setups that write them.
CRASH_DIRS = (
    "/fs/vol/external01/wiiu/crash_logs",
    "/wiiu/crash_logs",
    "/sd/wiiu/crash_logs",
    "/fs/vol/external01/wiiu/logs",
    "/wiiu/logs",
)

GPR = re.compile(r"\br(\d{1,2})\s*[:=]?\s*(?:0x)?([0-9a-fA-F]{8})\b")
SPR = re.compile(r"\b(SRR0|SRR1|LR|CTR|XER|CR|MSR|DAR|DSISR|GQR\d)\s*[:=]?\s*"
                 r"(?:0x)?([0-9a-fA-F]{8})\b", re.I)
ADDR = re.compile(r"\b(?:0x)?((?:0[0-9a-fA-F]|1[0-9a-fA-F]|e[0-9a-fA-F]|f[0-9a-fA-F])"
                  r"[0-9a-fA-F]{6})\b")
# "foo.rpl" / "coreinit.rpl at 0x101c400" - the loaded module list.
MODULE = re.compile(r"\b([A-Za-z0-9_.\-]+\.(?:rpl|rpx|wms|wps))\b", re.I)
EXC_TYPE = re.compile(r"(DSI|ISI|Program|Alignment|Machine\s*Check|Decrementer|"
                      r"External|System\s*Call|Trace|Breakpoint)\s*(?:exception|interrupt)?",
                      re.I)

# Homebrew RPX code is loaded here on this console. An address in this window
# belongs to the game and is worth handing to addr2line; anything in the
# 0x1nnnnnnn or 0xe/0xf ranges is an OS library and will not resolve.
RPX_BASE = 0x02000000
RPX_TOP = 0x10000000


class Dump:
    """One crash dump, read for what can be read without knowing the layout."""

    def __init__(self, text, name=""):
        self.name = name
        self.text = text
        self.gpr = {}
        self.spr = {}
        self.modules = []
        self.addresses = []
        self.exception = ""
        self._parse()

    def _parse(self):
        m = EXC_TYPE.search(self.text)
        if m:
            self.exception = re.sub(r"\s+", " ", m.group(0)).strip()
        for num, val in GPR.findall(self.text):
            self.gpr.setdefault("r" + num, val.lower())
        for name, val in SPR.findall(self.text):
            self.spr.setdefault(name.upper(), val.lower())
        seen = set()
        for name in MODULE.findall(self.text):
            low = name.lower()
            if low not in seen:
                seen.add(low)
                self.modules.append(name)
        # Registers are already accounted for; what is left is the call stack,
        # which is the part worth symbolising.
        for val in ADDR.findall(self.text):
            addr = int(val, 16)
            if addr not in self.addresses:
                self.addresses.append(addr)

    def code_addresses(self):
        """Addresses plausibly inside the game's own RPX."""
        out = [a for a in self.addresses if RPX_BASE <= a < RPX_TOP]
        for key in ("SRR0", "LR"):
            val = self.spr.get(key)
            if val:
                addr = int(val, 16)
                if RPX_BASE <= addr < RPX_TOP and addr not in out:
                    out.insert(0, addr)
        return out

    def headline(self):
        bits = [self.exception or "crash"]
        if self.spr.get("SRR0"):
            bits.append("at 0x" + self.spr["SRR0"])
        if self.modules:
            bits.append(self.modules[0])
        return "  ".join(bits)


def parse_file(path):
    path = Path(path)
    return Dump(path.read_text(encoding="utf-8", errors="replace"), path.name)


# ------------------------------------------------------------------- console


def fetch(host, user="", password="", dest=None, timeout=8.0, limit=40):
    """Pull crash dumps off the console over FTP.

    Returns (files, message). Only new files are downloaded, so this is safe to
    call on a timer while a session runs.
    """
    dest = Path(dest) if dest else Path.cwd() / "crash_logs"
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, 21, timeout=timeout)
        ftp.login(user or "anonymous", password or "")
    except ftplib.all_errors as e:
        return [], "ftp: " + str(e)

    found_dir = ""
    for candidate in CRASH_DIRS:
        try:
            ftp.cwd(candidate)
            found_dir = candidate
            break
        except ftplib.all_errors:
            continue
    if not found_dir:
        ftp.close()
        return [], "no crash_logs directory on the console (looked in " + \
                   ", ".join(CRASH_DIRS) + ")"

    try:
        names = [n.rsplit("/", 1)[-1] for n in ftp.nlst()]
    except ftplib.all_errors as e:
        ftp.close()
        return [], "ftp list: " + str(e)

    names = [n for n in sorted(names) if n.lower().endswith((".txt", ".log", ".md"))]
    names = names[-limit:]
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        ftp.close()
        return [], str(e)

    got = []
    for name in names:
        local = dest / name
        if local.exists():
            continue
        try:
            with open(local, "wb") as fh:
                ftp.retrbinary("RETR " + name, fh.write)
            got.append(local)
        except ftplib.all_errors:
            try:
                local.unlink()
            except OSError:
                pass
            continue
    ftp.close()
    return got, found_dir + ": " + str(len(got)) + " new of " + str(len(names))


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
