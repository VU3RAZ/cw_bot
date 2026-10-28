"""CW skimmer: decode every keyed signal in a KiwiSDR's I/Q passband at once.

The I/Q (12 kHz complex, centred on the dial, +-5 kHz) is scanned every SCAN_EVERY
seconds for keyed tones (decoders.keyed_contrast over the last HIST seconds). A tone
found in two scans in a row gets a channel: a CW decoder tuned to its offset from the
centre (the decoders take the complex I/Q directly, so there are no images), its own
frequency-locked loop to stay centred, and an OverReader that splits the copy into overs
and repairs calls by voting (cwparse.correct_calls). A channel goes when its tone has
not been seen for IDLE seconds. Channels are at least SPACING Hz apart: two stations
closer than that can't be told apart by any skimmer.

The Std decoder costs about 0.1 % of one core per channel, so MAX_CHANNELS is far below
what the CPU allows; the limit is the span the receiver sends.
"""
import time

import numpy as np

from cwparse import OverReader
from decoders import FS, CWDecoder, PitchTracker, keyed_spectrum

SPAN = 4800             # Hz either side of the centre that is scanned (the Kiwi sends +-5000)


class ChannelLoop(PitchTracker):
    FLL_BW = 35.0           # narrower than the main decoder's: a neighbour 60 Hz away doesn't pull it


class Channel:
    def __init__(self, offset, now):
        self.dec = CWDecoder(offset, bandwidth=Skimmer.BANDWIDTH)
        self.fll = ChannelLoop()
        self.reader = OverReader()
        self.anchor = offset            # where the scan found it: the loop may move +-PULL from here
        self.seen = now                 # last scan that found its tone
        self.born = now
        self.snr = 0.0                  # keyed contrast, dB
        self.peak = 0.0                 # its strongest contrast lately (decays 1 dB per scan)
        self.text = ""                  # recent copy, cleaned and call-repaired, newest last
        self.call, self.call_t = "", 0.0
        self.copied = 0.0               # last time it printed anything
        self.wpm = 0.0
        self.overs = {}                 # over id -> cleaned text, for the live over

    @property
    def offset(self):
        return self.dec.freq


class Skimmer:
    SCAN_EVERY, HIST = 0.5, 2.0
    MIN_CONTRAST = 20.0     # dB; complex noise across the whole span peaks at ~17
    SPACING = 60.0          # Hz between channels
    PULL = 40.0             # Hz the loop may move a channel from where it was found
    IDLE = 30.0             # s without its tone before a channel is dropped
    QUIET = 3.0             # s without its tone before its copy is ignored
    MAX_CHANNELS = 80
    TEXT = 90               # characters of copy kept per channel
    BANDWIDTH = 60.0        # Hz tone filter (the main decoder's is 100): a neighbour 70 Hz away stays out
    SIDE_HZ = 250.0         # Hz: a peak this close to a stronger signal may be its side lobe or key clicks:
    SIDE_CORR = 0.7         # it is if its level rises and falls with the stronger one's keying,
    SIDE_DB = 12.0          # or if it is this much weaker than that signal has lately been
                            # (clicks sit ~20 dB down; a real neighbour 75 Hz away measured 7 dB down)

    def __init__(self):
        self.channels = []
        self.fc = None
        self.reset_scan()
        self.hints = {}         # call -> weight: spotted near the dial, extra votes for the readers
        self.mycall = ""

    def reset_scan(self):
        self.hist = np.zeros(0, np.complex64)
        self.n, self.t, self.pending = 0, 0.0, []

    def clear(self):
        self.channels = []
        self.reset_scan()

    def retune(self, fc):
        """The centre moved (a QSY): channels keep their RF frequency, those now outside
        the span go, and the scan starts over on the new I/Q."""
        if self.fc is not None and fc != self.fc:
            d = self.fc - fc
            keep = []
            for ch in self.channels:
                off = ch.offset + d
                if abs(off) <= SPAN:
                    ch.dec.retune(off)
                    ch.anchor += d
                    keep.append(ch)
            self.channels = keep
            self.reset_scan()
        self.fc = fc

    def feed(self, iq, fc, now=None):
        """iq: complex64 at FS centred on fc (Hz)."""
        now = time.time() if now is None else now
        self.retune(fc)
        self.hist = np.concatenate([self.hist, iq])[-int(FS * self.HIST):]
        self.n += len(iq)
        self.t += len(iq) / FS
        if self.n >= FS * self.SCAN_EVERY and len(self.hist) >= FS * self.HIST:
            self.n = 0
            self._scan(now)
        for ch in self.channels:
            hz = ch.fll.track(iq, ch.offset)
            if hz is not None and now - ch.seen < self.HIST and abs(hz - ch.anchor) <= self.PULL:
                ch.dec.retune(hz)
            txt = ch.dec.feed(iq)
            if now - ch.seen > self.QUIET:
                txt = ""                # its tone is gone: what it prints now is a neighbour or noise
            elif txt:
                ch.wpm = ch.dec.wpm     # its speed while it is the one keying
            overs = ch.reader.feed(txt, now, self.mycall) if txt else ch.reader.tick(now, self.mycall)
            if txt:
                ch.copied = now
            for o in overs:
                self._over(ch, o, now)
        self.channels = [c for c in self.channels if now - c.seen < self.IDLE]
        for ch in self.channels:
            ch.reader.hints = self.hints

    def _over(self, ch, o, now):
        if o["text"]:
            ch.overs[o["id"]] = o["text"]
        else:
            ch.overs.pop(o["id"], None)
        if o["done"]:
            done = ch.overs.pop(o["id"], "")
            if done:
                ch.text = (ch.text + " | " + done if ch.text else done)[-self.TEXT * 2:]
        call = o.get("call") or next((c for c in o.get("calls", []) if c != self.mycall), "")
        # a call sent twice, spotted or heard before names the channel; one noisy copy
        # (H3Z out of static) doesn't
        sure = call and (o.get("calls", []).count(call) >= 2 or call in self.hints
                         or ch.reader.heard.get(call, (0, 0))[0] >= 2)
        if sure:
            ch.call, ch.call_t = call, now

    def _scan(self, now):
        f, c, S = keyed_spectrum(self.hist, -SPAN, SPAN)
        z = (S - S.mean(axis=0)) / (S.std(axis=0) + 1e-9)     # each bin's keying, normalised
        peaks = []
        for i in range(1, len(c) - 1):
            if c[i] >= self.MIN_CONTRAST and c[i] >= c[i - 1] and c[i] >= c[i + 1]:
                d = c[i - 1] - 2 * c[i] + c[i + 1]
                hz = f[i] + (0.5 * (c[i - 1] - c[i + 1]) / d * (f[1] - f[0]) if d < 0 else 0)
                peaks.append((float(c[i]), float(hz), i))
        peaks.sort(reverse=True)
        for ch in self.channels:
            ch.peak -= 1.0
        kept = []
        for snr, hz, i in peaks:                    # strongest first, SPACING apart, no side lobes
            if any(abs(hz - k) < self.SPACING or (abs(hz - k) < self.SIDE_HZ and (
                    float(np.mean(z[:, i] * z[:, j])) > self.SIDE_CORR or snr < ks - self.SIDE_DB))
                   for ks, k, j in kept):
                continue
            if any(self.SPACING <= abs(hz - ch.offset) < self.SIDE_HZ and snr < ch.peak - self.SIDE_DB
                   for ch in self.channels):
                continue                            # clicks of a channel that paused in this window
            kept.append((snr, hz, i))
        kept = [(snr, hz) for snr, hz, _ in kept]
        confirmed = []
        for snr, hz in kept:
            ch = min(self.channels, key=lambda ch: abs(ch.offset - hz), default=None)
            if ch and abs(ch.offset - hz) < self.SPACING:
                ch.seen, ch.snr, ch.peak = now, snr, max(ch.peak, snr)
            elif any(abs(hz - p) <= 25 for p in self.pending):
                confirmed.append((snr, hz))         # seen in the last scan too: a real tone
        self.pending = [hz for _, hz in kept]
        for snr, hz in confirmed:
            if len(self.channels) >= self.MAX_CHANNELS:
                break
            if all(abs(ch.offset - hz) >= self.SPACING for ch in self.channels):
                ch = Channel(hz, now)
                ch.snr = ch.peak = snr
                ch.reader.hints = self.hints
                self.channels.append(ch)
        # two channels whose loops pulled them onto one signal: keep the one with copy
        self.channels.sort(key=lambda ch: ch.offset)
        out = []
        for ch in self.channels:
            if out and abs(ch.offset - out[-1].offset) < self.SPACING / 2:
                if (ch.copied, ch.born) > (out[-1].copied, out[-1].born):
                    out[-1] = ch
                continue
            out.append(ch)
        self.channels = out

    def rows(self, now=None):
        """One row per channel that has copied something: RF Hz, call, WPM, contrast,
        seconds since it last printed, and its recent copy (live over last)."""
        now = time.time() if now is None else now
        out = []
        for ch in self.channels:
            live = " ".join(ch.overs[k] for k in sorted(ch.overs))
            text = (ch.text + (" | " if ch.text and live else "") + live)[-self.TEXT:]
            if not text:
                continue
            out.append({"hz": round((self.fc or 0) + ch.offset), "offset": round(ch.offset),
                        "call": ch.call, "spotted": ch.call in self.hints, "wpm": round(ch.wpm),
                        "snr": round(ch.snr), "age": round(now - ch.copied), "text": text.lstrip(" |")})
        return out
