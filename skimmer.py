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
what the CPU allows; the limit is the span the receiver sends: +-5 kHz from most KiwiSDRs,
+-10 kHz from those in a 3-channel mode (rx3.wf3, 20.25 kHz audio). MultiSkimmer covers
the wide ones with two Skimmers, one on each half (kiwi.Subband), +-8.8 kHz in all: the
receiver's passband is flat to about +-8 kHz and 10 dB down at 9 (measured on OK1XOM).
"""
import time

import numpy as np

import kiwi
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
        self.cq = None                  # (time, call, text, done) of its last over with a CQ in it

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

    def __init__(self, span=SPAN):
        self.span = span        # Hz either side of the centre scanned
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
                if abs(off) <= self.span:
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
        is_cq = "CQ" in o["text"].split() or o.get("cq")
        if o["done"] and o["text"] and not is_cq:
            ch.cq = None                # a CQ followed by a contact: no longer calling
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
            # a CQ anywhere: "CQ TEST" sent over and over without a K is all one over
            if is_cq and call != self.mycall:
                ch.cq = (now, call, o["text"], o["done"])

    def _scan(self, now):
        f, c, S = keyed_spectrum(self.hist, -self.span, self.span)
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

    def cqs(self, now=None, within=20.0):
        """Stations calling CQ now: a channel whose last CQ over (with a sure call) was
        live or ended within `within` s and whose tone is still there. Hunt picks from these."""
        now = time.time() if now is None else now
        out = []
        for ch in self.channels:
            if ch.cq and now - ch.cq[0] <= within and now - ch.seen <= self.QUIET:
                t, call, text, done = ch.cq
                out.append({"hz": round((self.fc or 0) + ch.offset), "call": call, "snr": round(ch.snr),
                            "wpm": round(ch.wpm), "age": round(now - t, 1), "done": done, "text": text[-80:]})
        return out

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


class MultiSkimmer:
    """The skimmer for whatever I/Q rate the receiver sends: one Skimmer at FS (+-4.8 kHz),
    or at 2 FS (a wideband receiver) two, on 12 kHz halves centred HALF below and above
    the dial, each scanning +-HALF: together +-2 HALF. Same interface as Skimmer."""
    HALF = 4400

    def __init__(self):
        self.rate, self.parts = None, []        # [(centre offset Hz, Subband or None, Skimmer)]
        self.fc = None
        self._hints, self._mycall = {}, ""

    @property
    def wide(self):
        return self.rate is not None and self.rate > FS

    @property
    def span(self):
        return 2 * self.HALF if self.wide else SPAN

    @property
    def channels(self):
        return [ch for _, _, sk in self.parts for ch in sk.channels]

    @property
    def hints(self):
        return self._hints

    @hints.setter
    def hints(self, h):
        self._hints = h
        for _, _, sk in self.parts:
            sk.hints = h

    @property
    def mycall(self):
        return self._mycall

    @mycall.setter
    def mycall(self, c):
        self._mycall = c
        for _, _, sk in self.parts:
            sk.mycall = c

    def clear(self):
        self.rate, self.parts, self.fc = None, [], None

    def feed(self, iq, fc, rate=FS, now=None):
        if rate != self.rate:
            self.rate = rate
            if rate > FS:
                self.parts = [(c, kiwi.Subband(c), Skimmer(self.HALF)) for c in (-self.HALF, self.HALF)]
            else:
                self.parts = [(0, None, Skimmer())]
            self.hints, self.mycall = self._hints, self._mycall
        self.fc = fc
        for c, sub, sk in self.parts:
            sk.feed(sub(iq) if sub else iq, fc + c, now)

    def retune(self, fc):
        for c, _, sk in self.parts:
            sk.retune(fc + c)
        self.fc = fc

    def cqs(self, now=None, within=20.0):
        out = []
        for r in sorted((r for _, _, sk in self.parts for r in sk.cqs(now, within)), key=lambda r: r["hz"]):
            if out and r["hz"] - out[-1]["hz"] < Skimmer.SPACING:   # the seam
                if r["snr"] > out[-1]["snr"]:
                    out[-1] = r
                continue
            out.append(r)
        return out

    def rows(self, now=None):
        out = []
        for r in sorted((r for _, _, sk in self.parts for r in sk.rows(now)), key=lambda r: r["hz"]):
            # one signal on the seam, seen by both halves: keep the half it is further inside
            # (r["offset"] is still from that half's centre)
            if out and r["hz"] - out[-1]["hz"] < Skimmer.SPACING:
                if abs(r["offset"]) < abs(out[-1]["offset"]):
                    out[-1] = r
                continue
            out.append(r)
        return [dict(r, offset=r["hz"] - (self.fc or 0)) for r in out]
