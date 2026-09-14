"""Telling "the console died" from "the network died".

A UDP log that stops looks exactly the same either way: the last line you have
is the last line there is, and nothing about it says whether the game hit an
exception, the console locked up, or Wi-Fi dropped a packet and never came
back. That ambiguity is expensive - it is the difference between reading a
crash and chasing one that never happened.

The fix is cheap: when the stream goes quiet, ask the console directly. If it
still answers a ping or accepts a socket, the console is alive and it is the
logging that stopped - a very different bug. If nothing answers, the console
went down with it.
"""

import socket
import subprocess
import sys
import threading
import time

# How long the stream has to be silent before it is worth asking. Long enough
# that a loading screen or a quiet menu does not trigger it.
QUIET_S = 8.0
# Do not re-probe more often than this while a console stays unreachable.
REPROBE_S = 20.0
PING_TIMEOUT_MS = 1200
TCP_TIMEOUT_S = 1.5
# Ports worth trying. ftpiiu is the usual one; a console with no FTP running
# can still answer a ping, which is why both signals are reported.
TCP_PORTS = (21, 80, 8080)


def ping(host, timeout_ms=PING_TIMEOUT_MS):
    """One ICMP echo, via the system tool so it needs no privileges."""
    if sys.platform == "win32":
        args = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        args = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host]
    try:
        res = subprocess.run(args, capture_output=True, text=True,
                             timeout=(timeout_ms / 1000.0) + 3.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return False
    # Windows ping exits 0 for "Destination host unreachable", so the text has
    # to be checked as well as the code.
    bad = ("unreachable", "timed out", "100% packet loss", "100% loss")
    low = (res.stdout or "").lower()
    return not any(b in low for b in bad)


def tcp_open(host, ports=TCP_PORTS, timeout=TCP_TIMEOUT_S):
    """First port that accepts, or "" if none do.

    A refused connection still proves something is home - the console answered
    - so refusal is reported separately from silence.
    """
    refused = False
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return str(port), False
        except socket.timeout:
            continue
        except ConnectionRefusedError:
            refused = True
        except OSError:
            continue
    return "", refused


def probe(host):
    """Ask the console whether it is still there.

    Returns a dict with a plain-language `verdict`, because the point of this
    is to remove a judgement call, not to hand over two more readings to
    interpret.
    """
    started = time.time()
    icmp = ping(host)
    port, refused = tcp_open(host)
    alive = bool(icmp) or bool(port) or refused
    bits = []
    if icmp is True:
        bits.append("ping ok")
    elif icmp is False:
        bits.append("ping timeout")
    else:
        bits.append("ping unavailable")
    if port:
        bits.append("tcp/" + port + " open")
    elif refused:
        bits.append("tcp refused (host is up)")
    else:
        bits.append("no tcp answer")
    return {
        "host": host,
        "alive": alive,
        "ping": icmp,
        "port": port,
        "refused": refused,
        "detail": ", ".join(bits),
        "took": time.time() - started,
        "at": started,
        "verdict": ("console still responding - the logging stopped, not the console"
                    if alive else "console unreachable - it went down with the log"),
    }


class Watchdog:
    """Watches the quiet and probes once it has gone on too long.

    Probing happens on its own thread: a ping costs a second or so and the UI
    must not stop for it. The result is delivered back as an ordinary log line
    so it lands in the recording alongside the silence it explains.
    """

    def __init__(self, emit, host_of, quiet=QUIET_S):
        self.emit = emit
        self.host_of = host_of
        self.quiet = quiet
        self.last_result = None
        self.checked_at = 0.0
        self.busy = False
        self.armed = False

    def note_traffic(self):
        """Called when a line arrives: the stream is alive, so re-arm."""
        self.armed = True

    def poll(self, last_at, now=None):
        """Call once a frame or tick. `last_at` is when a line last arrived."""
        now = now or time.time()
        if self.busy or not self.armed or not last_at:
            return
        if now - last_at < self.quiet:
            return
        if now - self.checked_at < REPROBE_S:
            return
        host = (self.host_of() or "").strip()
        if not host:
            return
        self.checked_at = now
        self.busy = True
        quiet_for = now - last_at

        def worker():
            try:
                result = probe(host)
                result["quiet_for"] = quiet_for
                self.last_result = result
                mark = "--" if result["alive"] else "!!"
                self.emit("%s stream quiet %.0fs: %s (%s) %s"
                          % (mark, quiet_for, result["verdict"], result["detail"], mark))
                # One answer per silence. A console that stays down should not
                # produce a line every twenty seconds forever.
                self.armed = False
            finally:
                self.busy = False

        threading.Thread(target=worker, daemon=True).start()
