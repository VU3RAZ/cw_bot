"""Audio-domain DSP: spectrum, CW, RTTY and FT8/FT4 decoders.

All decoders take float32 mono samples at FS (12 kHz) via feed().
"""
import json
import os
import subprocess
import tempfile
import time
import wave

import numpy as np
from scipy import signal

FS = 12000
HERE = os.path.dirname(os.path.abspath(__file__))
FT8_BIN = os.path.join(HERE, "vendor", "ft8_lib", "decode_ft8")
CW_PARAMS = os.environ.get("CW_DECODER_PARAMS") or os.path.join(HERE, "decoder_params.json")  # cwtune.py


# ---------------------------------------------------------------- spectrum
class Spectrum:
    """Audio waterfall rows: 0..MAX_HZ, uint8 scaled dB."""
    NFFT = 4096
    HOP = 1500          # 8 rows / second
    MAX_HZ = 4000

    def __init__(self):
        self.buf = np.zeros(0, np.float32)
        self.win = np.hanning(self.NFFT).astype(np.float32)
        self.nbins = int(self.MAX_HZ * self.NFFT / FS)
        self.last_db = np.zeros(self.nbins, np.float32)

    def feed(self, x):
        self.buf = np.concatenate([self.buf, x])
        rows = []
        while len(self.buf) >= self.NFFT:
            seg = self.buf[:self.NFFT] * self.win
            self.buf = self.buf[self.HOP:]
            mag = np.abs(np.fft.rfft(seg))[:self.nbins]
            db = 20 * np.log10(mag + 1e-9)
            self.last_db = db
            rows.append(np.clip((db + 30) * 2.5, 0, 255).astype(np.uint8).tobytes())
        return rows

    def peak_near(self, hz, width=60):
        """Strongest bin within +-width Hz of hz (for click-to-snap)."""
        b = self.NFFT / FS
        lo, hi = max(0, int((hz - width) * b)), min(self.nbins, int((hz + width) * b))
        if hi <= lo:
            return hz
        i = lo + int(np.argmax(self.last_db[lo:hi]))
        return i / b


class ToneFilter:
    """Complex mix to baseband + lowpass, keeping state between chunks."""

    def __init__(self, freq, cutoff, order=4):
        self.freq = freq
        self.phase = 0.0
        self.b, self.a = signal.butter(order, cutoff / (FS / 2))
        self.zi = signal.lfilter_zi(self.b, self.a) * 0

    def __call__(self, x):
        n = np.arange(len(x))
        w = 2 * np.pi * self.freq / FS
        lo = np.exp(-1j * (self.phase + w * n))
        self.phase = (self.phase + w * len(x)) % (2 * np.pi)
        y, self.zi = signal.lfilter(self.b, self.a, x * lo, zi=self.zi)
        return np.abs(y)


# ---------------------------------------------------------------- CW
MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F",
    "--.": "G", "....": "H", "..": "I", ".---": "J", "-.-": "K", ".-..": "L",
    "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
    "-.--": "Y", "--..": "Z", "-----": "0", ".----": "1", "..---": "2",
    "...--": "3", "....-": "4", ".....": "5", "-....": "6", "--...": "7",
    "---..": "8", "----.": "9", ".-.-.-": ".", "--..--": ",", "..--..": "?",
    "-..-.": "/", "-...-": "=", ".-.-.": "<AR>", "-....-": "-", ".--.-.": "@",
    "-.--.": "<KN>", "-.--.-": ")", "---...": ":", ".----.": "'", "...-.-": "<SK>",
    ".-...": "<AS>", "-.-.-": "<KA>", "...---...": "<SOS>", "........": "<HH>",
}


class CWDecoder:
    DECIM = 60          # envelope at 200 Hz -> 5 ms resolution
    # Tunable (cwtune.py fits them to real KiwiSDR audio and saves decoder_params.json):
    BANDWIDTH = 100.0   # tone filter width, Hz
    SQUELCH = 6.0       # required peak/noise ratio before keying
    KEY_ON, KEY_OFF = 0.55, 0.40    # key thresholds between noise floor and peak (hysteresis)
    DASH = 2.0          # marks longer than this many dots are dashes
    CHAR_GAP = 2.5      # silence (in dots) that ends a character
    WORD_GAP_DEFAULT = 4.0  # silence (in dots) that ends a word, until the sender's spacing is learnt
    MIN_WORD_GAP, WORD_GAP = 3.4, 6.0   # limits of the learnt threshold
    TUNABLE = ("BANDWIDTH", "SQUELCH", "KEY_ON", "KEY_OFF", "DASH", "CHAR_GAP", "WORD_GAP_DEFAULT",
               "MIN_WORD_GAP", "WORD_GAP")

    @classmethod
    def params(cls):
        return {k: getattr(cls, k) for k in cls.TUNABLE}

    @classmethod
    def load_params(cls, path=CW_PARAMS):
        """Apply tuned values from decoder_params.json ({"params": {...}}) if it exists."""
        try:
            with open(path) as f:
                p = json.load(f)["params"]
        except (OSError, ValueError, KeyError):
            return {}
        for k, v in p.items():
            if k in cls.TUNABLE:
                setattr(cls, k, float(v))
        return p

    def __init__(self, freq=700.0, bandwidth=None, squelch=None):
        self.bandwidth = bandwidth or self.BANDWIDTH
        self.squelch = squelch or self.SQUELCH
        self.set_freq(freq)
        self.dot = 0.060 * 200      # dot length in envelope samples (20 WPM)
        self.noise = 1.0    # starts high, falls quickly to the real floor
        self.peak = 0.0
        self.key = False
        self.run = 0                # samples in current key state
        self.symbol = ""
        self.word_sent = True
        self.phase_acc = np.zeros(0, np.float32)
        self.gaps = []              # recent gaps longer than 2 dots (in dots), for word_gap
        self.word_gap = self.WORD_GAP_DEFAULT

    def set_freq(self, freq):
        self.freq = float(freq)
        self.filt = ToneFilter(self.freq, self.bandwidth / 2)

    def retune(self, freq):
        """Move the tone filter without resetting it (AFC), so copy isn't disturbed."""
        self.freq = float(freq)
        self.filt.freq = self.freq

    @property
    def wpm(self):
        return 1200 / (self.dot * 5) if self.dot else 0

    def feed(self, x):
        env = self.filt(x)
        self.phase_acc = np.concatenate([self.phase_acc, env])
        n = len(self.phase_acc) // self.DECIM
        if n == 0:
            return ""
        e = self.phase_acc[:n * self.DECIM].reshape(n, self.DECIM).mean(axis=1)
        self.phase_acc = self.phase_acc[n * self.DECIM:]
        out = []
        for v in e:
            # Fast-attack/slow-decay peak; noise floor = running mean while key-up.
            self.peak = max(v, self.peak * 0.995)
            if not self.key:
                rate = 0.2 if self.noise > 4 * v else 0.02
                self.noise += rate * (v - self.noise)
            snr_ok = self.peak > self.squelch * self.noise
            hi = self.noise + self.KEY_ON * (self.peak - self.noise)
            lo = self.noise + self.KEY_OFF * (self.peak - self.noise)
            key = snr_ok and (v > hi or (self.key and v > lo))
            if key == self.key:
                self.run += 1
                if not key:
                    out.append(self._gap_tick())
                continue
            if self.key:
                self._mark_done(self.run)
            elif self.run > 2 * self.dot:
                self._learn_gap(self.run / self.dot)
            self.key = key
            self.run = 1
        return "".join(out)

    def _mark_done(self, n):
        if n < 0.3 * self.dot:      # glitch
            return
        if n < self.DASH * self.dot:
            self.symbol += "."
            self.dot = 0.85 * self.dot + 0.15 * n
        else:
            self.symbol += "-"
            self.dot = 0.85 * self.dot + 0.15 * (n / 3)
        self.dot = min(max(self.dot, 4), 60)   # 5 ms units: ~60..4 WPM
        self.word_sent = False

    def _learn_gap(self, g):
        """Fit the word-gap threshold to this sender. Many operators and keyers use word
        spaces of 4-5 dots, not 7, so a fixed threshold merges their words. The last 30
        character/word gaps are split into two clusters (2-means) and the threshold goes
        midway between them."""
        if g > 15:                  # end of an over, not a word space
            return
        self.gaps = (self.gaps + [g])[-30:]
        lo, hi = min(self.gaps), max(self.gaps)
        a = b = []
        for _ in range(8):
            a = [x for x in self.gaps if abs(x - lo) <= abs(x - hi)]
            b = [x for x in self.gaps if abs(x - lo) > abs(x - hi)]
            if not a or not b:
                break
            lo, hi = sum(a) / len(a), sum(b) / len(b)
        if len(a) >= 3 and len(b) >= 2 and hi - lo > 0.7:
            self.word_gap = min(max((lo + hi) / 2, self.MIN_WORD_GAP), self.WORD_GAP)
        else:
            self.word_gap = self.WORD_GAP_DEFAULT

    def _gap_tick(self):
        # Called on each silent sample; emit at char/word boundaries exactly once.
        if self.symbol and self.run == int(self.CHAR_GAP * self.dot):
            ch = MORSE.get(self.symbol, "*")
            self.symbol = ""
            return ch
        if not self.word_sent and not self.symbol and self.run == int(self.word_gap * self.dot):
            self.word_sent = True
            return " "
        return ""

    def level(self):
        return float(self.peak / (self.noise + 1e-12))


CWDecoder.load_params()


# ---------------------------------------------------------------- RTTY
ITA2_LTRS = "\x00E\nA SIU\rDRJNFCKTZLWHYPQOBG\x0eMXV\x0f"
ITA2_FIGS = "\x003\n- '87\r$4\x07,!:(5\")2#6019?&\x0e./;\x0f"


class RTTYDecoder:
    DECIM = 10          # 1200 Hz decision rate

    def __init__(self, center=2210.0, shift=170.0, baud=45.45, mark_high=False,
                 unshift_on_space=True):
        self.shift = shift
        self.baud = baud
        self.mark_high = mark_high
        self.unshift_on_space = unshift_on_space
        self.rate = FS / self.DECIM
        self.spb = self.rate / baud
        self.set_freq(center)
        self.state = "idle"
        self.count = 0.0
        self.bits = []
        self.figs = False
        self.prev = 1
        self.mpk = self.spk = 1e-3
        self.acc = np.zeros(0)

    def set_freq(self, center, mark_high=None):
        if mark_high is not None:
            self.mark_high = mark_high
        self.center = float(center)
        sign = 1 if self.mark_high else -1
        cut = self.baud * 1.2
        self.mark = ToneFilter(self.center + sign * self.shift / 2, cut, order=3)
        self.space = ToneFilter(self.center - sign * self.shift / 2, cut, order=3)

    def feed(self, x):
        m, s = self.mark(x), self.space(x)
        d = np.stack([m, s], axis=1)
        self.acc = np.concatenate([self.acc.reshape(-1, 2), d])
        n = len(self.acc) // self.DECIM
        if n == 0:
            return ""
        blk = self.acc[:n * self.DECIM].reshape(n, self.DECIM, 2).mean(axis=1)
        self.acc = self.acc[n * self.DECIM:]
        out = []
        for mv, sv in blk:
            # automatic threshold correction: normalise each tone by its own peak
            self.mpk = max(mv, self.mpk * 0.998)
            self.spk = max(sv, self.spk * 0.998)
            bit = 1 if mv / self.mpk > sv / self.spk else 0
            ch = self._uart(bit)
            if ch:
                out.append(ch)
        return "".join(out)

    def _uart(self, bit):
        if self.state == "idle":
            if self.prev == 1 and bit == 0:
                self.state, self.count, self.bits = "start", 0.0, []
            self.prev = bit
            return None
        self.prev = bit
        self.count += 1
        if self.state == "start":
            if self.count >= self.spb / 2:
                if bit == 0:
                    self.state, self.count = "data", -self.spb / 2
                else:
                    self.state = "idle"     # false start
            return None
        if self.state == "data":
            # sample in the middle of each bit
            if self.count >= self.spb * (len(self.bits) + 1) - self.spb / 2:
                self.bits.append(bit)
                if len(self.bits) == 6:     # 5 data + stop
                    self.state = "idle"
                    self.prev = bit
                    if self.bits[5] != 1:
                        return None         # framing error
                    code = sum(b << i for i, b in enumerate(self.bits[:5]))
                    return self._ita2(code)
        return None

    def _ita2(self, code):
        if code == 0x1F:
            self.figs = False
            return None
        if code == 0x1B:
            self.figs = True
            return None
        ch = (ITA2_FIGS if self.figs else ITA2_LTRS)[code]
        if ch == " " and self.unshift_on_space:
            self.figs = False
        if ch in "\x00\x07\x0e\x0f\r":
            return None
        return ch


# ---------------------------------------------------------------- FT8 / FT4
class FT8Decoder:
    """Collects audio per UTC slot and runs ft8_lib's decode_ft8 on it."""

    def __init__(self, ft4=False):
        self.ft4 = ft4
        self.period = 7.5 if ft4 else 15.0
        self.buf = []
        self.buf_start = None   # unix time of first sample in buf
        self.slot = None

    def set_ft4(self, ft4):
        self.__init__(ft4)

    def feed(self, x, t_end):
        """x: samples ending at unix time t_end. Returns finished slot audio."""
        t_start = t_end - len(x) / FS
        slot = int(t_start // self.period)
        done = None
        if self.slot is None:
            self.slot = slot
        if slot != self.slot:
            # chunk straddles the boundary: split it
            boundary = slot * self.period
            k = int(round((boundary - t_start) * FS))
            k = min(max(k, 0), len(x))
            self._append(x[:k], t_start)
            done = (self.slot * self.period, self.buf_start, np.concatenate(self.buf) if self.buf else None)
            self.buf, self.buf_start, self.slot = [], None, slot
            x, t_start = x[k:], boundary
        self._append(x, t_start)
        return done

    def _append(self, x, t_start):
        if len(x) == 0:
            return
        if self.buf_start is None:
            self.buf_start = t_start
        self.buf.append(x)

    def decode(self, slot_start, buf_start, audio):
        """Blocking. Returns list of decode dicts."""
        if audio is None or not os.path.exists(FT8_BIN):
            return []
        # Align: pad the front if we started mid-slot.
        lead = int(round((buf_start - slot_start) * FS))
        if lead > 0:
            audio = np.concatenate([np.zeros(lead, np.float32), audio])
        n = int(self.period * FS)
        audio = audio[:n]
        if len(audio) < n * 0.6:
            return []
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(FS)
                w.writeframes(pcm.tobytes())
            args = [FT8_BIN] + (["-ft4"] if self.ft4 else []) + [path]
            out = subprocess.run(args, capture_output=True, text=True, timeout=20).stdout
        finally:
            os.unlink(path)
        utc = time.strftime("%H%M%S", time.gmtime(slot_start))
        res = []
        for line in out.splitlines():
            if "~" not in line:
                continue
            head, msg = line.split("~", 1)
            f = head.split()
            if len(f) < 4:
                continue
            try:
                # ft8_lib's DT runs ~0.65 s later than WSJT-X's on its own test vectors
                res.append({"utc": utc, "snr": float(f[1]), "dt": float(f[2]) - 0.65,
                            "freq": float(f[3]), "msg": msg.strip()})
            except ValueError:
                continue
        return res
