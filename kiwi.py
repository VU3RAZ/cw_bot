"""Public KiwiSDR receivers: find suitable ones and stream their audio.

The receiver list comes from ReceiverBook (receiverbook.de), cached for a day in
logs/kiwi_receivers.json; each candidate's /status page gives users, SNR, time
limits and its GPS position.

Two ways to get samples:
  Stream  kiwiclient's kiwirecorder (github.com/jks-prv/kiwiclient, cloned into vendor/
          on first use) in a subprocess: 12 kHz s16le, USB audio (mono) or I/Q (stereo).
          Used by corpus.py.
  Client  the Kiwi websocket in-process (asyncio): retunes and changes mode or passband
          without reconnecting, gives the S-meter. Used by the web app (kiwisdr.py), with
  Waterfall  the receiver's waterfall (1024 bins, dBm) for the panadapter.

  python3 kiwi.py [region]                                  list usable receivers
  python3 kiwi.py record HOST[:PORT] KHZ SECONDS [--iq] [-o out.wav]

A KiwiSDR accepts one connection per IP address: close its tab in the browser
before streaming from it here.
"""
import asyncio
import concurrent.futures as cf
import json
import os
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import aiohttp
import numpy as np
from scipy.signal import firwin, lfilter

HERE = os.path.dirname(os.path.abspath(__file__))
KIWICLIENT = os.path.join(HERE, "vendor", "kiwiclient")
RECORDER = os.path.join(KIWICLIENT, "kiwirecorder.py")
CACHE = os.path.join(HERE, "logs", "kiwi_receivers.json")
RECEIVERBOOK = "https://www.receiverbook.de/map?type=kiwisdr"
USER = "cw_bot"     # name shown in the receiver's user list
FS = 12000
IQ_PASSBAND = (-5000, 5000)     # Hz around the dial in I/Q mode

# (lat_min, lat_max, lon_min, lon_max). Europe has the densest CW QSO traffic.
REGIONS = {
    "europe": (42, 60, -10, 30),
    "na-east": (30, 48, -90, -65),
    "japan": (30, 46, 128, 146),
    "india": (6, 32, 68, 92),
    "oceania": (-45, -10, 110, 180),
}


def ensure_kiwiclient():
    if not os.path.exists(RECORDER):
        subprocess.run(["git", "clone", "-q", "--depth", "1", "https://github.com/jks-prv/kiwiclient.git",
                        KIWICLIENT], check=True)
    return RECORDER


def receiver_list(max_age=86400):
    """[(label, url, lat, lon)] of every KiwiSDR ReceiverBook knows."""
    try:
        if time.time() - os.path.getmtime(CACHE) < max_age:
            with open(CACHE) as f:
                return [tuple(r) for r in json.load(f)]
    except (OSError, ValueError):
        pass
    req = urllib.request.Request(RECEIVERBOOK, headers={"User-Agent": "cw_bot"})
    html = urllib.request.urlopen(req, timeout=30).read().decode(errors="replace")
    i = html.index("var receivers =") + len("var receivers =")
    data, _ = json.JSONDecoder().raw_decode(html[i:].lstrip())
    out = []
    for loc in data:
        lon, lat = loc["location"]["coordinates"]
        for r in loc["receivers"]:
            if r.get("type") == "KiwiSDR":
                out.append((r["label"], r["url"].rstrip("/"), lat, lon))
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(out, f)
    return out


def status(url, timeout=6):
    """A receiver's /status page as a dict, or None if unreachable."""
    try:
        with urllib.request.urlopen(url + "/status", timeout=timeout) as r:
            text = r.read().decode(errors="replace")
    except Exception:
        return None
    return dict(line.replace('\\"', '"').split("=", 1) for line in text.splitlines() if "=" in line)


def summarize(url, st):
    """Rank-relevant facts from a /status dict, or None if the receiver can't be used."""
    try:
        if st.get("offline") != "no" or st.get("status") != "active" or st.get("ant_connected") == "0":
            return None
        users, users_max = int(st["users"]), int(st["users_max"])
        snr = [int(v) for v in st.get("snr", "0").split(",")]
        lat, lon = (float(v) for v in st["gps"].strip("()").split(","))
        lo, hi = (int(v) for v in st.get("bands", "0-30000000").split("-"))
    except (KeyError, ValueError):
        return None
    if lo > 3_500_000 or hi < 14_350_000:      # needs 80-20 m at least
        return None
    host_port = url.split("//", 1)[-1].split("/", 1)[0]
    host, _, port = host_port.partition(":")
    return {"url": url, "host": host, "port": int(port or 80), "name": st.get("name", "").strip(), "free": users_max - users,
            "snr": snr[-1], "time_limited": "Limits" in st.get("sdr_hw", ""), "lat": lat, "lon": lon,
            "loc": st.get("loc", ""), "antenna": st.get("antenna", "")}


def rank(rows):
    """Best first: a free slot to spare, no time limit, then HF SNR."""
    return sorted(rows, key=lambda r: (r["free"] < 2, r["time_limited"], -r["snr"]))


def find(region="europe", limit=10):
    """Usable KiwiSDRs in a region, best first."""
    la0, la1, lo0, lo1 = REGIONS[region]
    cands = [c for c in receiver_list() if la0 <= c[2] <= la1 and lo0 <= c[3] <= lo1]
    rows = []
    with cf.ThreadPoolExecutor(48) as ex:
        for c, st in zip(cands, ex.map(lambda c: status(c[1]), cands)):
            r = st and summarize(c[1], st)
            if r:
                rows.append(r)
    out, seen = [], set()
    for r in rank(rows):                # the same receiver is often listed under two URLs
        key = (r["name"], round(r["lat"], 2), round(r["lon"], 2))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out[:limit]


class Stream:
    """Samples from one KiwiSDR channel, dial at `dial_khz`.
    Audio: USB with a 300-2700 Hz passband, so a CW signal at f kHz is heard at
    (f - dial_khz) * 1000 Hz; 12 kHz mono s16le.
    iq=True: raw I/Q, 12 kHz s16le interleaved I, Q, IQ_PASSBAND around the dial; a
    signal at f kHz sits at +(f - dial_khz) * 1000 Hz. IQToUSB turns it into the same audio."""

    def __init__(self, rx, dial_khz, seconds, lo=300, hi=2700, iq=False):
        self.rx, self.dial_khz, self.iq = rx, dial_khz, iq
        if iq:
            lo, hi = IQ_PASSBAND
        self.p = subprocess.Popen(
            [sys.executable, ensure_kiwiclient(), "--nc", "-q", "-s", rx["host"], "-p", str(rx["port"]),
             "-u", USER, "-f", f"{dial_khz:.2f}", "-m", "iq" if iq else "usb", "-L", str(lo), "-H", str(hi),
             "-r", str(FS), "--tlimit", str(int(seconds))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def read(self, n=1200):
        """Up to n bytes of s16le audio (or interleaved I/Q); b"" when the stream has ended."""
        return self.p.stdout.read(n)

    def close(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait()
        err = self.p.stderr.read().decode(errors="replace").strip().splitlines()
        return err[-1] if err else ""


def iq_samples(data):
    """Interleaved s16le I/Q bytes -> complex64, full scale 1."""
    v = np.frombuffer(data[:len(data) // 4 * 4], "<i2").astype(np.float32) / 32768
    return v[0::2] + 1j * v[1::2]


class IQToUSB:
    """I/Q -> USB audio, as the receiver would demodulate it: keep lo..hi Hz above the
    dial (complex band-pass), take the real part. Keeps filter state across calls."""

    def __init__(self, lo=300, hi=2700, taps=255):
        n = np.arange(taps) - (taps - 1) / 2
        self.h = firwin(taps, (hi - lo) / 2, fs=FS) * np.exp(2j * np.pi * (lo + hi) / 2 * n / FS)
        self.zi = np.zeros(taps - 1, complex)

    def __call__(self, iq):
        y, self.zi = lfilter(self.h, 1, iq, zi=self.zi)
        return y.real.astype(np.float32)


class IQDemod:
    """I/Q centred on the dial -> the audio a USB (or, flip=True, LSB) receiver tuned
    shift_hz away would give: the I/Q is mirrored for LSB, moved up by shift_hz and
    band-passed lo..hi Hz. In CW: shift = the pitch, so a signal on the dial is heard
    at the pitch, as in the radio's own CW mode."""

    def __init__(self, shift_hz, lo, hi, flip=False):
        self.shift, self.flip, self.phase = float(shift_hz), flip, 0.0
        self.bp = IQToUSB(lo, hi)

    def __call__(self, iq):
        if self.flip:
            iq = np.conj(iq)
        w = 2 * np.pi * self.shift / FS
        y = iq * np.exp(1j * (self.phase + w * np.arange(len(iq))))
        self.phase = (self.phase + w * len(iq)) % (2 * np.pi)
        return self.bp(y)


class Resampler:
    """Stateful linear interpolation from the receiver's sample rate (e.g. 11998.9 or
    20250 Hz) to FS, real or complex. The passband is well below either Nyquist."""

    def __init__(self, rate_in, rate_out=FS):
        self.step, self.pos, self.prev = rate_in / rate_out, 0.0, None

    def __call__(self, x):
        if self.prev is not None:
            x = np.concatenate([self.prev, x])         # x[0] = last sample of the previous block
        t = np.arange(self.pos, len(x) - 1, self.step)
        self.pos = (t[-1] + self.step - (len(x) - 1)) if len(t) else self.pos - (len(x) - 1)
        self.prev = x[-1:]
        xp = np.arange(len(x))
        if np.iscomplexobj(x):
            return (np.interp(t, xp, x.real) + 1j * np.interp(t, xp, x.imag)).astype(np.complex64)
        return np.interp(t, xp, x).astype(np.float32)


class KiwiError(Exception):
    """The receiver refused or ended the connection; the message says why."""


class _Socket:
    """One of a Kiwi's websockets (the protocol its web UI uses, as in kiwiclient): login,
    keepalive, refusals. Audio (SND) and waterfall (W/F) sockets opened with the same
    timestamp `ts` count as one user, like a browser tab."""
    STREAM = ""

    def __init__(self, host, port, ts=None, user=USER):
        self.host, self.port, self.user = host, port, user
        self.ts = ts or int(time.time())
        self.ws = self.loop = None
        self.ready = False              # set up: commands go out at once from here on

    def _send(self, msg):
        if self.ws is not None and self.ready:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.ws.send_str(msg)))

    async def run(self):
        """Stream until the connection ends; raises KiwiError with the receiver's reason."""
        self.loop = asyncio.get_running_loop()
        url = f"ws://{self.host}:{self.port}/{self.ts}/{self.STREAM}"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.ws_connect(url, max_msg_size=0, headers={"User-Agent": "cw_bot"}) as ws:
                self.ws = ws
                try:
                    await ws.send_str("SET auth t=kiwi p=")
                    keepalive = 0.0
                    while True:
                        m = await ws.receive(timeout=20)
                        if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            raise KiwiError("the receiver closed the connection (time limit or kicked?)")
                        if m.type != aiohttp.WSMsgType.BINARY:
                            continue
                        if m.data[:3] == b"MSG":
                            await self._msg(m.data[4:].decode(errors="replace"))
                        elif self.ready:
                            self._data(m.data[:3], m.data[3:])
                        if self.ready and time.time() - keepalive > 2:
                            keepalive = time.time()
                            await ws.send_str("SET keepalive")
                finally:
                    self.ws, self.ready = None, False

    async def _msg(self, body):
        for kv in body.split(" "):
            k, _, v = kv.partition("=")
            if k == "too_busy":
                raise KiwiError(f"all {v} channels in use")
            if k == "badp" and v not in ("", "0"):
                raise KiwiError(f"connection refused (code {v}: password needed, or this IP already connected)")
            if k == "down":
                raise KiwiError("receiver is down")
            if k == "redirect":
                raise KiwiError(f"receiver redirects to {urllib.parse.unquote(v)}")
            await self._param(k, v)

    async def _param(self, k, v):
        pass

    def _data(self, tag, body):
        pass


class Client(_Socket):
    """Audio from one receiver channel, uncompressed. tune(), set_agc() and set_nb() apply
    at once, from any thread.
    on_samples(x, rssi_dbm): x at FS, float32 audio, or complex64 in mode "iq"."""
    STREAM = "SND"

    def __init__(self, host, port, on_samples, user=USER, ts=None):
        super().__init__(host, port, ts, user)
        self.on_samples = on_samples
        self.mod = ("usb", 300, 2700, 7030.0)        # mode, low cut, high cut (Hz), dial kHz
        self.agc = (True, 1000, 50)                  # on, decay ms, manual gain dB
        self.resample = None

    def tune(self, mode, lo, hi, khz):
        self.mod = (mode, int(lo), int(hi), float(khz))
        self._send("SET mod=%s low_cut=%d high_cut=%d freq=%.3f" % self.mod)

    def set_agc(self, on=True, decay=1000, gain=50):
        self.agc = (bool(on), int(decay), int(gain))
        self._send("SET agc=%d hang=0 thresh=-100 slope=6 decay=%d manGain=%d" % self.agc)

    def set_nb(self, on):
        for m in ("SET nb algo=1", "SET nb type=0 param=0 pval=100", "SET nb type=0 param=1 pval=50",
                  f"SET nb type=0 en={int(bool(on))}"):
            self._send(m)

    async def _param(self, k, v):
        if k == "audio_rate":
            await self.ws.send_str(f"SET AR OK in={v} out=44100")
        elif k == "sample_rate":
            for m in ("SET squelch=0 max=0", "SET genattn=0", "SET gen=0 mix=-1", f"SET ident_user={self.user}",
                      "SET mod=%s low_cut=%d high_cut=%d freq=%.3f" % self.mod,
                      "SET agc=%d hang=0 thresh=-100 slope=6 decay=%d manGain=%d" % self.agc,
                      "SET compression=0", "SET keepalive"):
                await self.ws.send_str(m)
            self.resample = Resampler(float(v))
            self.ready = True

    def _data(self, tag, b):
        if tag != b"SND":
            return
        flags = b[0]
        rssi = 0.1 * struct.unpack(">H", b[5:7])[0] - 127
        data = b[7:]
        if flags & 0x10:            # ADPCM: only the first block, before compression=0 takes
            return
        if flags & 0x08:            # stereo = I/Q, after 10 bytes of GPS timestamp
            v = np.frombuffer(data[10:], ">i2").astype(np.float32) / 32768
            x = v[0::2] + 1j * v[1::2]
        else:
            x = np.frombuffer(data, ">i2").astype(np.float32) / 32768
        self.on_samples(self.resample(x), rssi)


class Waterfall(_Socket):
    """The receiver's waterfall: 1024 bins across bandwidth / 2**zoom around a centre.
    on_frame(lo_hz, hi_hz, dbm): dbm = float32 per bin. The receiver may cap the zoom
    (zoom_cap, e.g. 11 = 14.6 kHz): lo/hi are what it actually sent. view() and speed()
    apply at once, from any thread."""
    STREAM = "W/F"
    BINS = 1024
    MAX_ZOOM = 14

    def __init__(self, host, port, on_frame, user=USER, ts=None):
        super().__init__(host, port, ts, user)
        self.on_frame = on_frame
        self.zoom, self.cf_khz, self.fps = 10, 7030.0, 4
        self.bandwidth_khz = 30000.0

    def view(self, zoom, cf_khz):
        self.zoom, self.cf_khz = max(0, min(self.MAX_ZOOM, int(zoom))), float(cf_khz)
        self._send(f"SET zoom={self.zoom} cf={self.cf_khz:.3f}")

    def speed(self, n):
        """1 = once a second ... 4 = fastest (about 9-23 frames/s)."""
        self.fps = int(n)
        self._send(f"SET wf_speed={self.fps}")

    async def _param(self, k, v):
        if k == "bandwidth":
            self.bandwidth_khz = float(v) / 1000
        elif k == "wf_setup":
            for m in (f"SET ident_user={self.user}", f"SET zoom={self.zoom} cf={self.cf_khz:.3f}",
                      "SET maxdb=-10 mindb=-110", f"SET wf_speed={self.fps}", "SET wf_comp=0", "SET keepalive"):
                await self.ws.send_str(m)
            self.ready = True

    def _data(self, tag, b):
        if tag != b"W/F" or len(b) < 13:
            return
        x_bin, flags_zoom, _ = struct.unpack("<III", b[1:13])
        zoom = flags_zoom & 0xFFFF
        full = self.bandwidth_khz * 1000
        lo = x_bin / (2 ** self.MAX_ZOOM * self.BINS) * full
        bins = np.frombuffer(b[13:], np.uint8)
        self.on_frame(round(lo), round(lo + full / 2 ** zoom), bins.astype(np.float32) - 255)


def record(rx, khz, seconds, iq, out):
    """Record to a WAV file: 12 kHz, mono USB audio or stereo I/Q (left I, right Q)."""
    import wave
    st = Stream(rx, khz, seconds, iq=iq)
    n = 0
    with wave.open(out, "wb") as w:
        w.setnchannels(2 if iq else 1)
        w.setsampwidth(2)
        w.setframerate(FS)
        while data := st.read(4800):
            w.writeframes(data)
            n += len(data)
    err = st.close()
    print(f"{out}: {n / (4 if iq else 2) / FS:.1f} s {'I/Q' if iq else 'USB audio'}{'; ' + err if err else ''}")


def main():
    import argparse
    if sys.argv[1:2] == ["record"]:
        ap = argparse.ArgumentParser(prog="kiwi.py record", description="Record a KiwiSDR to WAV.")
        ap.add_argument("receiver", help="host[:port]")
        ap.add_argument("khz", type=float, help="dial frequency, kHz (USB: CW at dial + 0.3..2.7 kHz)")
        ap.add_argument("seconds", type=float)
        ap.add_argument("--iq", action="store_true", help=f"raw I/Q, {IQ_PASSBAND[0]}..{IQ_PASSBAND[1]} Hz around the dial")
        ap.add_argument("-o", "--out", help="WAV file (default <utc>_<khz>[_iq].wav)")
        a = ap.parse_args(sys.argv[2:])
        host, _, port = a.receiver.partition(":")
        out = a.out or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"_{a.khz:.1f}{'_iq' if a.iq else ''}.wav"
        return record({"host": host, "port": int(port or 8073)}, a.khz, a.seconds, a.iq, out)
    ap = argparse.ArgumentParser(description="List usable public KiwiSDRs in a region (or: kiwi.py record -h).")
    ap.add_argument("region", nargs="?", default="europe", choices=sorted(REGIONS))
    ap.add_argument("-n", type=int, default=15)
    a = ap.parse_args()
    for r in find(a.region, a.n):
        print(f"snr {r['snr']:3} dB  free {r['free']:2}  {'limited ' if r['time_limited'] else 'no-limit'}  "
              f"{r['url']:45} {r['loc'][:35]} | {r['antenna'][:35]}")


if __name__ == "__main__":
    main()
