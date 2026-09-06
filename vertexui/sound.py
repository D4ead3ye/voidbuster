"""Synthesised interface sounds - no asset files to ship.

Every cue is generated as WAV bytes on first use, so a frozen one-file exe
carries no audio assets and there is nothing to lose track of.

The default set was tuned by ear over several passes, and the lessons are
baked into the synthesis rather than left as taste:

  * pitch, not volume, is what makes a cue feel sharp. Quiet high beeps
    still cut through; the whole set sits low and is filtered.
  * a falling pitch reads as a physical knock. The same tone held flat
    reads as an alert.
  * raised-cosine fades at both ends. A waveform starting or stopping at
    non-zero amplitude clicks, and that click is most of "harsh".

    from vertexui import sound
    sfx = sound.SoundSet(volume=0.16)
    sfx.play("click")
"""

import io
import math
import queue
import struct
import threading
import time
import wave


# (segments, gain) - a segment is (start Hz, end Hz, milliseconds)
DEFAULT_SPECS = {
    "hover": ([(300.0, 240.0, 40)], 0.20),
    "click": ([(360.0, 150.0, 80)], 0.60),
    "ok":    ([(262.0, 262.0, 110), (330.0, 330.0, 180)], 0.50),
    "warn":  ([(247.0, 220.0, 130), (196.0, 185.0, 200)], 0.50),
    "error": ([(165.0, 150.0, 140), (124.0, 110.0, 230)], 0.55),
}

# A brighter alternative, to show the set is data. Swap with
# SoundSet(specs=sound.CRISP_SPECS).
CRISP_SPECS = {
    "hover": ([(720.0, 640.0, 26)], 0.16),
    "click": ([(880.0, 420.0, 55)], 0.50),
    "ok":    ([(523.0, 523.0, 80), (784.0, 784.0, 130)], 0.45),
    "warn":  ([(494.0, 440.0, 100), (392.0, 370.0, 150)], 0.45),
    "error": ([(330.0, 300.0, 110), (247.0, 220.0, 180)], 0.50),
}


class SoundSet:
    """A named collection of cues, rendered on demand and played async."""

    def __init__(self, specs=None, volume=0.16, enabled=True,
                 harmonic=0.06, attack_ms=14.0, release_ms=34.0,
                 lowpass_hz=900.0, rate=44100):
        self.specs = dict(specs or DEFAULT_SPECS)
        self.volume = max(0.0, min(1.0, float(volume)))
        self.enabled = bool(enabled)
        self.harmonic = harmonic        # 2nd partial: body without loudness
        self.attack_ms = attack_ms      # long enough that no cue has an onset
        self.release_ms = release_ms
        self.lowpass_hz = lowpass_hz    # only filtering truly dulls a sound
        self.rate = rate
        self._cache = {}
        self._q = None
        self._last = 0.0
        self._fails = 0

    # -- rendering ------------------------------------------------------
    def render(self, name: str) -> bytes:
        segments, gain = self.specs[name]
        rate = self.rate
        total_ms = sum(seg[2] for seg in segments)
        frames = bytearray()
        phase = 0.0
        elapsed = 0.0
        lp = 0.0
        dt = 1.0 / rate
        rc = 1.0 / (2.0 * math.pi * self.lowpass_hz)
        alpha = dt / (rc + dt)

        for f0, f1, ms in segments:
            n = max(1, int(rate * ms / 1000))
            for i in range(n):
                u = i / n
                # Integrate phase rather than evaluating sin(2*pi*f*t) with
                # a moving f - the latter warps the wave and buzzes as the
                # pitch slides.
                phase += 2.0 * math.pi * (f0 + (f1 - f0) * u) / rate
                t_ms = elapsed + i * 1000.0 / rate

                env = 1.0
                if t_ms < self.attack_ms:
                    env = 0.5 * (1.0 - math.cos(math.pi * t_ms / self.attack_ms))
                left = total_ms - t_ms
                if left < self.release_ms:
                    env *= 0.5 * (1.0 - math.cos(math.pi * left / self.release_ms))
                env *= math.exp(-2.2 * t_ms / max(1.0, total_ms))

                v = math.sin(phase) + self.harmonic * math.sin(2.0 * phase)
                v /= (1.0 + self.harmonic)
                lp += alpha * (v - lp)
                s = lp * env * gain * self.volume
                frames += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
            elapsed += ms

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(frames))
        return buf.getvalue()

    def wav(self, name: str) -> bytes:
        if name not in self._cache:
            self._cache[name] = self.render(name)
        return self._cache[name]

    # -- playback -------------------------------------------------------
    def _worker(self):
        import winsound
        while True:
            name = self._q.get()
            if name is None:
                return
            try:
                # winsound refuses SND_MEMORY together with SND_ASYNC
                # ("Cannot play asynchronously from memory"), so playback is
                # synchronous - on this thread, where blocking for the
                # 40-370ms a cue lasts costs nothing. On the UI thread it
                # would stutter every frame that made a sound.
                winsound.PlaySound(self.wav(name),
                                   winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            except Exception as e:
                self._fails += 1
                if self._fails == 1:
                    print(f"Interface sounds unavailable: {e!r}")
                if self._fails >= 3:
                    self.enabled = False
                    print("Interface sounds disabled after repeated failures.")
                    return

    def play(self, name: str = "click"):
        """Queue a cue. Never blocks, never raises."""
        if not self.enabled or name not in self.specs:
            return
        now = time.time()
        # Hover fires far more often than a click and needs more spacing,
        # or sweeping a toolbar becomes a burst rather than a series.
        gap = 0.07 if name == "hover" else 0.05
        if now - self._last < gap:
            return
        self._last = now
        if self._q is None:
            self._q = queue.Queue(maxsize=2)
            threading.Thread(target=self._worker, name="vertexui-sfx",
                             daemon=True).start()
        try:
            self._q.put_nowait(name)
        except queue.Full:
            pass          # one already in flight; dropping beats lagging


_active = None


def use(sound_set: SoundSet):
    global _active
    _active = sound_set
    return _active


def current() -> SoundSet:
    """The set widgets play from. Created silent-safe on first use."""
    global _active
    if _active is None:
        _active = SoundSet()
    return _active


def play(name: str = "click"):
    current().play(name)
