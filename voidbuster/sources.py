"""Where console log text comes from.

A Wii U can be made to talk in several ways and which one a given title uses is
not something you get to choose, so the logger listens to all of them at once
and labels each line with the transport it arrived on:

  UDP    WHBLogUdp (any homebrew built with wut) and Aroma's logging module,
         which is the one that matters here: it redirects OSReport and
         OSConsoleWrite, so *retail* titles start logging without being
         rebuilt. Default port 4405, broadcast, plain UTF-8 text.
  TCP    the same module's stream mode, and a few standalone loggers.
  file   a log already on disk - an SD capture, or a previous session being
         re-read. Also how the FTP puller feeds crash dumps in.

Everything lands in one queue as an Event, so the rest of the program never has
to care which of these produced a line.
"""

import os
import queue
import select
import socket
import sys
import threading
import time
from dataclasses import dataclass, field

DEFAULT_UDP_PORTS = (4405,)
# Ports that homebrew loggers are known to pick. Used by the sweep, which is
# how you find out what an unfamiliar title is doing without guessing.
SWEEP_PORTS = (4404, 4405, 4406, 4410, 4412, 4420, 5000, 5555, 6000, 7331, 8080)

# A datagram that does not end in a newline is assumed to be half a line, since
# WHBLogWrite sends exactly that. Held this long, then given up on - otherwise
# the last line of a session never appears.
PARTIAL_FLUSH_S = 0.25
RECV_SIZE = 65535

# SO_REUSEADDR means something different here than on a unix box: Windows lets
# a second process bind the same UDP port and then delivers each datagram to
# only one of them, so a forgotten copy of the logger silently swallows the
# stream and the new one shows an empty window. Refusing to share the port
# turns that into an error message instead of a mystery.
SHARE_UDP_PORTS = not sys.platform.startswith("win")


@dataclass
class Event:
    """One line, plus where it came from."""
    text: str
    source: str = "udp"
    origin: str = ""          # console address, or file path
    at: float = field(default_factory=time.time)


class Source(threading.Thread):
    """Common shape: a daemon thread pushing Events until stopped."""

    kind = "source"

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out = out
        self.running = True
        self.error = ""
        self.count = 0
        self.last_at = 0.0

    def emit(self, text, origin=""):
        self.count += 1
        self.last_at = time.time()
        self.out.put(Event(text=text, source=self.kind, origin=origin))

    def note(self, text):
        """The logger's own commentary, kept in the same stream so a recording
        explains itself when read back later."""
        self.out.put(Event(text=text, source="voidbuster", origin=""))

    def stop(self):
        self.running = False


class LineAssembler:
    """Rebuilds lines from datagrams that may split or batch them."""

    def __init__(self):
        self._buf = {}
        self._since = {}

    def feed(self, key, data):
        text = data.decode("utf-8", "replace")
        text = self._buf.pop(key, "") + text
        parts = text.split("\n")
        tail = parts.pop()
        if tail:
            self._buf[key] = tail
            self._since[key] = time.time()
        else:
            self._since.pop(key, None)
        return [p.rstrip("\r") for p in parts if p.strip()]

    def expired(self, now=None):
        """Held fragments old enough to count as lines in their own right."""
        now = now or time.time()
        out = []
        for key, since in list(self._since.items()):
            if now - since >= PARTIAL_FLUSH_S:
                text = self._buf.pop(key, "").rstrip("\r")
                self._since.pop(key, None)
                if text.strip():
                    out.append((key, text))
        return out


class UdpSource(Source):
    """Binds every requested port at once.

    One socket per port rather than one port per run: a title that logs
    somewhere unexpected is caught without restarting, and the cost is a few
    idle sockets.
    """

    kind = "udp"

    def __init__(self, out, ports=DEFAULT_UDP_PORTS, on_console=None):
        super().__init__(out)
        self.ports = list(ports)
        self.bound = []
        self.on_console = on_console
        self.consoles = {}
        self._asm = LineAssembler()

    def _bind(self, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if SHARE_UDP_PORTS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # Homebrew loggers broadcast rather than unicast, so the socket has
            # to be willing to receive that.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        sock.bind(("0.0.0.0", port))
        sock.setblocking(False)
        return sock

    def run(self):
        socks = {}
        for port in self.ports:
            try:
                socks[self._bind(port)] = port
            except OSError as e:
                self.note("!! udp " + str(port) + " unavailable: " + _busy(e))
        if not socks:
            self.error = "no UDP port could be bound"
            self.running = False
            return
        self.bound = sorted(socks.values())
        self.note("-- listening on UDP " + ", ".join(str(p) for p in self.bound) + " --")

        while self.running:
            ready, _, _ = select.select(list(socks), [], [], 0.2)
            for sock in ready:
                port = socks[sock]
                while True:
                    try:
                        data, addr = sock.recvfrom(RECV_SIZE)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError:
                        break
                    if not data:
                        break
                    ip = addr[0]
                    # Keyed by port as well as address: one console logging on
                    # two ports is worth knowing about, and is exactly what a
                    # game plus a plugin looks like.
                    if (ip, port) not in self.consoles:
                        self.consoles[(ip, port)] = time.time()
                        self.note("-- console " + ip + " talking on udp/" + str(port) + " --")
                        if self.on_console:
                            self.on_console(ip, port)
                    for line in self._asm.feed((ip, port), data):
                        self.emit(line, origin=ip + ":" + str(port))
            for key, line in self._asm.expired():
                self.emit(line, origin=str(key[0]) + ":" + str(key[1]))
        for sock in socks:
            sock.close()


class TcpSource(Source):
    """Stream mode. Accepts several consoles at once; a dropped connection is
    reported and then waited on again, because the console rebooting is the
    normal case rather than an error."""

    kind = "tcp"

    def __init__(self, out, port=4405):
        super().__init__(out)
        self.port = port
        self._asm = LineAssembler()

    def run(self):
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(4)
            srv.setblocking(False)
        except OSError as e:
            self.error = str(e)
            self.note("!! tcp " + str(self.port) + " unavailable: " + str(e))
            self.running = False
            return
        self.note("-- listening on TCP " + str(self.port) + " --")
        clients = {}
        while self.running:
            ready, _, _ = select.select([srv] + list(clients), [], [], 0.2)
            for sock in ready:
                if sock is srv:
                    try:
                        conn, addr = srv.accept()
                    except OSError:
                        continue
                    conn.setblocking(False)
                    clients[conn] = addr[0] + ":" + str(self.port)
                    self.note("-- console " + addr[0] + " connected on tcp/" + str(self.port) + " --")
                    continue
                origin = clients[sock]
                try:
                    data = sock.recv(RECV_SIZE)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    data = b""
                if not data:
                    self.note("-- " + origin + " disconnected --")
                    clients.pop(sock, None)
                    sock.close()
                    continue
                for line in self._asm.feed(origin, data):
                    self.emit(line, origin=origin)
            for key, line in self._asm.expired():
                self.emit(line, origin=str(key))
        for sock in clients:
            sock.close()
        srv.close()


class FileSource(Source):
    """tail -f, with the replacement case handled: an SD log that gets rotated
    while we watch it should keep working rather than silently stop."""

    kind = "file"

    def __init__(self, out, path, from_start=False, follow=True):
        super().__init__(out)
        self.path = str(path)
        self.from_start = from_start
        # A replay wants the file and then an exit; a live tail wants to sit
        # there. Same reader, one flag.
        self.follow = follow
        self.done = False

    def run(self):
        self.note("-- following " + self.path + " --")
        fh = None
        marker = None
        while self.running:
            try:
                if fh is None:
                    fh = open(self.path, "r", encoding="utf-8", errors="replace")
                    st = os.fstat(fh.fileno())
                    marker = (st.st_ino, st.st_dev)
                    if not self.from_start:
                        fh.seek(0, os.SEEK_END)
                line = fh.readline()
                if line:
                    if line.strip():
                        self.emit(line.rstrip("\r\n"), origin=self.path)
                    continue
                if not self.follow:
                    break
                # Nothing new. Check whether the file was replaced under us.
                try:
                    st = os.stat(self.path)
                    if (st.st_ino, st.st_dev) != marker:
                        fh.close()
                        fh = None
                        self.from_start = True
                        continue
                except OSError:
                    pass
                time.sleep(0.15)
            except OSError as e:
                self.error = str(e)
                time.sleep(1.0)
                fh = None
        if fh:
            fh.close()
        self.done = True
        self.running = False


def sweep(ports=SWEEP_PORTS, seconds=8.0, progress=None):
    """Listen across a spread of ports and report which ones a console is using.

    This exists because the honest answer to "does the logger work for this
    game" is often "it is logging somewhere else". Rather than guess, bind
    everything plausible for a few seconds and look.
    """
    socks = {}
    missed = []
    for port in ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if SHARE_UDP_PORTS:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            s.bind(("0.0.0.0", port))
            s.setblocking(False)
            socks[s] = port
        except OSError:
            # Almost always our own listener still holding it. Reported rather
            # than skipped, because a port we could not listen on is not the
            # same as a port the console is silent on.
            missed.append(port)
            continue
    found = {}
    deadline = time.time() + seconds
    while time.time() < deadline:
        if progress:
            progress(max(0.0, deadline - time.time()))
        ready, _, _ = select.select(list(socks), [], [], 0.25)
        for sock in ready:
            try:
                data, addr = sock.recvfrom(RECV_SIZE)
            except OSError:
                continue
            port = socks[sock]
            entry = found.setdefault(port, {"packets": 0, "bytes": 0,
                                            "hosts": set(), "sample": ""})
            entry["packets"] += 1
            entry["bytes"] += len(data)
            entry["hosts"].add(addr[0])
            if not entry["sample"]:
                entry["sample"] = data.decode("utf-8", "replace").strip()[:120]
    for sock in socks:
        sock.close()
    if missed:
        found["_busy"] = missed
    return found


def _busy(err):
    """Turn a bind error into the sentence that names the actual cause."""
    text = str(err)
    if getattr(err, "winerror", 0) == 10048 or "10048" in text or "in use" in text.lower():
        return ("port already in use - another copy of this logger, or a "
                "udplogserver window, is holding it")
    return text


def loopback_test(port=4405, text="voidbuster self-test"):
    """Send a datagram to our own listener.

    Separates "nothing is arriving" from "arriving but not displayed", which
    are very different problems and look identical from the couch.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(("[voidbuster] " + text + "\n").encode("utf-8"), ("127.0.0.1", port))
        s.close()
        return True, "sent to 127.0.0.1:" + str(port)
    except OSError as e:
        return False, str(e)
