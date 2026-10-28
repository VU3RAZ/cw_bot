"""A public KiwiSDR as a receive-only radio.

Same interface as radio.IC7300 (state dict, set_* methods, on_state callback) so the web
app can tune it like the radios on USB. Differences:
  * audio comes from the receiver over the network (on_audio, 100 ms s16le blocks at 12 kHz),
    not from a USB sound card (has_audio = True);
  * receive only: PTT, TUNE and CW sending raise; no preamp/attenuator/NR;
  * the panadapter is the receiver's waterfall (a second websocket, kiwi.Waterfall), which
    can be unavailable when its few waterfall channels are taken - audio carries on;
  * the receiver is chosen from the public list (kiwi.find) and kept in settings.json
    "kiwi"; set_receiver() switches to another one.

With the skimmer on, CW and CW-R run the connection in I/Q (+-5 kHz around the dial):
the speaker/decoder audio is then made here (kiwi.IQDemod, same passband and pitch) and
the raw I/Q goes to on_iq for skimmer.Skimmer. The Kiwi takes the switch mid-stream.

Modes are made from the Kiwi's USB/LSB/AM/NBFM demodulators so the dial means what it
means on the IC-7300: in CW the dial is the signal's frequency and it is heard at the CW
pitch; in RTTY the dial is the mark frequency, mark at 2125 Hz (RTTY) or 2295 Hz (RTTY-R).
"""
import asyncio
import time

import aiohttp
import numpy as np

import kiwi

PITCH = 600
RTTY_CENTER = 2210                 # Hz between mark 2125 and space 2295, the RTTY decoder default
# FIL1/2/3 widths in Hz, like the IC-7300 defaults
WIDTHS = {"cw": {1: 1200, 2: 500, 3: 250}, "rtty": {1: 2400, 2: 500, 3: 250},
          "ssb": {1: 3000, 2: 2400, 3: 1800}, "am": {1: 9000, 2: 6000, 3: 3000}}
AGC_DECAY = {"FAST": 250, "MID": 1000, "SLOW": 2500}
BLOCK = kiwi.FS // 10              # samples per on_audio call (100 ms, as the sound-card path)
WF_SPEEDS = {"fast": 4, "mid": 3, "slow": 2}


def scope_px(dbm):
    """Waterfall dBm per bin -> panadapter pixel values on the IC-7300's 0-160 scale
    (-130 dBm = 0, 1.6 per dB), so the Floor/Contrast sliders behave alike."""
    return np.clip((dbm + 130) * 1.6, 0, 160).astype(np.uint8).tobytes()


def zoom_for(half_span_hz, bandwidth_hz=30e6):
    """Smallest Kiwi zoom whose span (bandwidth / 2**zoom) covers +-half_span_hz."""
    z = int(np.floor(np.log2(bandwidth_hz / (2 * max(half_span_hz, 1)))))
    return max(0, min(kiwi.Waterfall.MAX_ZOOM, z))
RX_ONLY = "the KiwiSDR is receive only"
REFUSED = {}                       # url -> (time, reason): receivers that turned us away lately
WORKED = 60                        # s: a session this long means the receiver takes us; shorter = refused


def smeter_icom(dbm):
    """Receiver RSSI in dBm -> Icom-scale 0-241 (S9 = -73 dBm = 120, 6 dB per S unit, S9+60 = 241)."""
    v = (dbm + 127) / 54 * 120 if dbm < -73 else 120 + (dbm + 73) / 60 * 121
    return int(max(0, min(241, round(v))))


def demod(mode, filt, hz, pitch=PITCH):
    """(Kiwi mode, low cut, high cut, dial kHz, IF width) for an app mode/filter/frequency."""
    if mode in ("CW", "CW-R"):
        w = WIDTHS["cw"][filt]
        lo, hi = max(50, pitch - w / 2), pitch + w / 2
        if mode == "CW":
            return "usb", lo, hi, (hz - pitch) / 1e3, w
        return "lsb", -hi, -lo, (hz + pitch) / 1e3, w
    if mode in ("RTTY", "RTTY-R"):
        w = WIDTHS["rtty"][filt]
        lo, hi = max(50, RTTY_CENTER - w / 2), RTTY_CENTER + w / 2
        if mode == "RTTY":             # LSB: mark (the dial) at 2125 Hz, space 170 Hz below it at 2295
            return "lsb", -hi, -lo, (hz + 2125) / 1e3, w
        return "usb", lo, hi, (hz - 2295) / 1e3, w
    if mode == "AM":
        w = WIDTHS["am"][filt]
        return "am", -w / 2, w / 2, hz / 1e3, w
    if mode == "FM":
        return "nbfm", -6000, 6000, hz / 1e3, 12000
    w = WIDTHS["ssb"][filt]
    if mode == "LSB":
        return "lsb", -(200 + w), -200, hz / 1e3, w
    return "usb", 200, 200 + w, hz / 1e3, w


class KiwiRadio:
    name = "KiwiSDR"
    has_audio = True
    caps = {"name": "KiwiSDR", "scope": True, "rx_only": True, "skimmer": True,
            "filters": {"1": "WIDE", "2": "MID", "3": "NAR"},
            "filter_help": "CW 1.2 kHz / 500 Hz / 250 Hz, RTTY 2.4 kHz / 500 / 250, SSB 3.0 / 2.4 / 1.8 kHz",
            "preamp": ["P.AMP —"], "att": "no attenuator on a KiwiSDR",
            "notes": ["KiwiSDR: a public web receiver, receive only; the panadapter is its waterfall.",
                      "AF/RF/PWR/WPM do nothing; use the Listen volume."]}

    def __init__(self, rx=None):
        self.rx = rx                   # a kiwi.summarize() row, or None until one is chosen
        self.port = rx and rx["url"]
        self.state = {
            "connected": False, "freq": 7_030_000, "mode": "CW", "filter": 2, "data": False,
            "af": 128, "rf": 255, "power": 0, "keyspeed": 0, "preamp": 0, "att": False,
            "nb": False, "nr": False, "agc": "MID", "smeter": 0, "tx": False,
            "po": 0, "swr": 0, "if_width": 500, "radio": self.name, "cw_pitch": PITCH, "break_in": 0,
            "scope_span": 25000, "scope_mode": "center", "scope_speed": "fast",
            "receiver": self._rx_public(), "kiwi_status": "", "skim": False,
        }
        self.on_state = None           # callback(dict of changed keys)
        self.on_scope = None           # callback(lo_hz, hi_hz, bytes pixels 0-160)
        self.on_audio = None           # callback(bytes): 100 ms of 12 kHz s16le mono
        self.on_iq = None              # callback(complex64 at 12 kHz, centre Hz) while skimming
        self._iq_demod, self._iq_key = None, None
        self._iq_fc = 0
        self.client = None
        self.wf = None
        self._fixed_cf = None          # kHz: the waterfall centre in "fixed" scope mode
        self._status_text, self._wf_note = "", ""
        self.task = None
        self.loop = None
        self._buf = np.zeros(0, np.float32)
        self._rssi = []
        self._first_audio = None       # time of this session's first audio

    def _rx_public(self):
        r = self.rx
        return r and {k: r.get(k) for k in ("url", "name", "loc", "antenna", "snr")}

    def _status(self, text=None):
        if text is not None:
            self._status_text = text
        self._update(kiwi_status=self._status_text + (f" · waterfall: {self._wf_note}" if self._wf_note else ""))

    def _update(self, **kw):
        ch = {k: v for k, v in kw.items() if self.state.get(k) != v}
        self.state.update(ch)
        if ch and self.on_state:
            self.on_state(ch)

    # ---- connection (runs on the server's event loop) -------------------
    def start(self):
        self.loop = asyncio.get_running_loop()
        self.task = self.loop.create_task(self._run())

    def stop(self):
        """From any thread."""
        if self.task and self.loop:
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.task = None
        self._update(connected=False)

    def set_receiver(self, rx):
        """Switch to another receiver (on the event loop)."""
        if self.task:
            self.task.cancel()
        self.rx, self.port = rx, rx["url"]
        self._update(receiver=self._rx_public(), connected=False, smeter=0)
        self.task = self.loop.create_task(self._run())

    async def _run(self):
        wait = 10
        while True:
            if not self.rx:
                self._status("choose a receiver from the list")
                return
            label = self.rx.get("name") or self.rx["url"]
            ts = int(time.time())       # shared by audio and waterfall: one user to the receiver
            self.client = kiwi.Client(self.rx["host"], self.rx["port"], self._samples, ts=ts)
            self._retune()
            self._apply_agc()
            self._status(f"connecting to {label}")
            self._first_audio = None
            wf_task = asyncio.ensure_future(self._waterfall(ts))
            try:
                await self.client.run()
                err = "connection ended"
            except kiwi.KiwiError as e:
                err = str(e)
                if "channels in use" in err or "refused" in err:
                    wait = max(wait, 60)
                    REFUSED[self.rx["url"]] = (time.time(), err)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                err = f"can't reach it ({e.__class__.__name__})"
            finally:
                wf_task.cancel()
            if self._worked():             # a dropout after a good session: retry soon
                wait = 10
            self._wf_note = ""
            self._update(connected=False, smeter=0)
            self._status(f"{label}: {err}; retrying in {wait} s (or pick another receiver)")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 300)

    async def _waterfall(self, ts):
        """The panadapter, alongside the audio. Waterfall channels are fewer than audio
        channels (3 of 8 on some receivers): when it is refused, audio goes on and it retries."""
        await asyncio.sleep(1)          # let the audio connection claim the channel first
        while True:
            self.wf = kiwi.Waterfall(self.rx["host"], self.rx["port"], self._frame, ts=ts)
            self._view()
            self.wf.speed(WF_SPEEDS.get(self.state["scope_speed"], 4))
            try:
                await self.wf.run()
                err = "connection ended"
            except kiwi.KiwiError as e:
                err = str(e)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                err = f"can't reach it ({e.__class__.__name__})"
            finally:
                self.wf = None
            self._wf_note = f"{err}; retrying in 30 s"
            self._status()
            await asyncio.sleep(30)

    def _frame(self, lo, hi, dbm):
        if self._wf_note:
            self._wf_note = ""
            self._status()
        if self.on_scope:
            self.on_scope(lo, hi, scope_px(dbm))

    def _view(self):
        """Waterfall span and centre from scope_span / scope_mode, like the radio's scope:
        centre follows the dial; fixed stays put until the dial leaves the view."""
        s = self.state
        bw = (self.wf.bandwidth_khz if self.wf else 30000) * 1000
        zoom = zoom_for(s["scope_span"] or 25000, bw)
        khz = s["freq"] / 1e3
        if s["scope_mode"] == "fixed":
            half = bw / 2 ** zoom / 2e3
            if self._fixed_cf is None or abs(khz - self._fixed_cf) > half * 0.9:
                self._fixed_cf = khz
            khz = self._fixed_cf
        if self.wf:
            self.wf.view(zoom, khz)

    def _worked(self):
        return self._first_audio is not None and time.time() - self._first_audio > WORKED

    def _samples(self, x, rssi):
        # some receivers stream ~10 s and then send too_busy: only a longer session clears a refusal
        if self._first_audio is None:
            self._first_audio = time.time()
        elif self.rx["url"] in REFUSED and self._worked():
            REFUSED.pop(self.rx["url"])
        if not self.state["connected"]:
            self._update(connected=True)
            self._status(f"listening on {self.rx.get('name') or self.rx['url']}")
        self._rssi.append(rssi)
        if np.iscomplexobj(x):          # skimming: hand on the I/Q, demodulate the audio here
            if self.on_iq:
                self.on_iq(x, self._iq_fc)
            x = (self._iq_demod or kiwi.IQToUSB())(x)
        self._buf = np.concatenate([self._buf, x])
        while len(self._buf) >= BLOCK:
            blk, self._buf = self._buf[:BLOCK], self._buf[BLOCK:]
            if self.on_audio:
                self.on_audio((np.clip(blk, -1, 1) * 32767).astype("<i2").tobytes())
            self._update(smeter=smeter_icom(float(np.mean(self._rssi))))
            self._rssi = []

    def _retune(self):
        s = self.state
        mode, lo, hi, khz, w = demod(s["mode"], s["filter"], s["freq"], s["cw_pitch"])
        if s["skim"] and s["mode"] in ("CW", "CW-R"):
            flip = s["mode"] == "CW-R"
            key = (s["cw_pitch"], abs(lo), abs(hi), flip)
            if key != self._iq_key:     # a new passband or pitch; a QSY keeps the filter state
                p_lo, p_hi = sorted((abs(lo), abs(hi)))
                self._iq_demod, self._iq_key = kiwi.IQDemod(s["cw_pitch"], p_lo, p_hi, flip), key
            self._iq_fc = s["freq"]
            mode, lo, hi, khz = "iq", kiwi.IQ_PASSBAND[0], kiwi.IQ_PASSBAND[1], s["freq"] / 1e3
        if self.client:
            self.client.tune(mode, lo, hi, khz)
        self._update(if_width=w)

    def set_skim(self, on):
        """Skimmer on/off: in CW the connection switches to I/Q (and back) mid-stream."""
        self._update(skim=bool(on))
        self._retune()

    def _apply_agc(self):
        if self.client:
            self.client.set_agc(True, AGC_DECAY.get(self.state["agc"], 1000))
            if self.state["nb"]:
                self.client.set_nb(True)

    # ---- controls (from any thread) ---------------------------------------
    def set_freq(self, hz):
        hz = int(hz)
        if not (10_000 <= hz <= 30_000_000):
            return
        self._update(freq=hz)
        self._retune()
        self._view()

    def set_mode(self, mode, filt=None, data=None):
        if mode not in ("LSB", "USB", "CW", "CW-R", "RTTY", "RTTY-R", "AM", "FM"):
            return
        ch = {"mode": mode, "filter": int(filt or self.state["filter"] or 1)}
        if data is not None:
            ch["data"] = bool(data)
        self._update(**ch)
        self._retune()

    def set_level(self, name, value):
        self._update(**{name: max(0, min(255, int(value)))})

    def set_agc(self, name):
        if name in AGC_DECAY:
            self._update(agc=name)
            self._apply_agc()

    def set_nb(self, on):
        self._update(nb=bool(on))
        if self.client:
            self.client.set_nb(on)

    def set_preamp(self, v):
        raise RuntimeError("no preamp on a KiwiSDR")

    def set_att(self, on):
        raise RuntimeError("no attenuator on a KiwiSDR")

    def set_nr(self, on):
        raise RuntimeError("no noise reduction on a KiwiSDR here")

    def set_ptt(self, on):
        if on:
            raise RuntimeError(RX_ONLY)

    def tune(self):
        raise RuntimeError(RX_ONLY)

    def send_cw(self, text):
        raise RuntimeError(RX_ONLY)

    def set_break_in(self, mode):
        raise RuntimeError(RX_ONLY)

    def stop_cw(self):
        pass

    def poll_tx(self):
        pass

    def set_scope_span(self, half_span_hz):
        self._update(scope_span=int(half_span_hz))
        self._view()

    def set_scope_mode(self, mode):
        if mode in ("center", "fixed"):
            self._fixed_cf = None
            self._update(scope_mode=mode)
            self._view()

    def set_scope_speed(self, speed):
        if speed in WF_SPEEDS:
            self._update(scope_speed=speed)
            if self.wf:
                self.wf.speed(WF_SPEEDS[speed])

    # ---- used by the beacon monitor -----------------------------------
    def cw_narrow(self):
        self.set_mode("CW", 2, data=False)

    def snapshot(self):
        return {k: self.state.get(k) for k in ("freq", "mode", "filter", "data")}

    def restore(self, s):
        if s.get("freq"):
            self.set_freq(s["freq"])
        if s.get("mode"):
            self.set_mode(s["mode"], s.get("filter"), s.get("data"))
