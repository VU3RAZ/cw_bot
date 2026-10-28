"""Record real CW traffic from public KiwiSDRs, for tuning the decoder (cwtune.py)
and testing the over reader (cwparse.py).

  python3 corpus.py capture --call N0CALL [--region europe] [--minutes 60] [--bands 40 30 20] [--iq]
  python3 corpus.py stats

capture picks the best KiwiSDR in the region (kiwi.find), then repeatedly chooses a
station that RBN spotters within 800 km of that receiver hear loudly (the RBN login
needs a callsign), tunes there and copies it. Without a nearby spotter spot it falls
back to DX-cluster spots within 3000 km. On each frequency it stays DWELL s, or
leaves after GIVE_UP s without readable copy; the decoder follows the strongest keyed
tone in the 300-2700 Hz passband.

Output in logs/corpus/:
  audio/<utc>_<khz>_<call>.raw   12 kHz mono s16le, one file per dwell
  audio/<same>.iq                with --iq: the raw I/Q as well, 12 kHz s16le interleaved I, Q,
                                 -5..+5 kHz around the dial (the .raw is demodulated from it)
  audio/<same>.json              receiver, dial, target spot and pitch track
  copy-YYYYMMDD.jsonl            one line per burst of decoded copy, with its audio offset
"""
import argparse
import asyncio
import datetime as dt
import glob
import json
import os
import threading
import time

import numpy as np

import geo
import kiwi
from decoders import FS, CWDecoder
from spots import Spots

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "logs", "corpus")
AUDIO = os.path.join(CORPUS, "audio")
DWELL, GIVE_UP, REVISIT = 480, 120, 1800        # seconds
GAP = 3.0                   # silence that ends a burst of copy
SPOTTER_KM, CLUSTER_KM = 800, 3000
MIN_CONTRAST = 20.0         # dB; noise measures ~12, a clean keyed signal 30-45
DIAL_OFFSET = 1.5           # kHz: dial this far below the spot puts it mid-passband


def keyed_peak(x, lo=300, hi=2700):
    """(Hz, dB): the audio tone with the biggest on/off contrast (p97 - p30 of its level)."""
    n = 480
    fr = x[:len(x) // n * n].reshape(-1, n) * np.hanning(n)
    S = 10 * np.log10(np.abs(np.fft.rfft(fr, axis=1)) ** 2 + 1e-12)
    f = np.fft.rfftfreq(n, 1 / FS)
    band = (f >= lo) & (f <= hi)
    c = np.percentile(S[:, band], 97, axis=0) - np.percentile(S[:, band], 30, axis=0)
    i = int(np.argmax(c))
    return float(f[band][i]), float(c[i])


def readable(text):
    """At least 3 real-looking words: a lone K or stray E/T/I noise doesn't count."""
    return sum(len(w) >= 3 and (any(c.isdigit() for c in w) or not set(w) <= set("EITSHANM*"))
               for w in text.split()) >= 3


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


class Capture:
    def __init__(self, call, region, bands, rx=None, iq=False):
        self.region, self.bands, self.fixed_rx, self.iq = region, bands, rx, iq
        self.visited, self.bad_rx = {}, set()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.spots = Spots()
        if call:
            self.loop.call_soon_threadsafe(self.spots.set_call, call)

    def receiver(self):
        if self.fixed_rx:
            return self.fixed_rx
        for r in kiwi.find(self.region, 20):
            if r["url"] not in self.bad_rx:
                return r
        return None

    def target(self, rx):
        """Next station to listen to: loudest near-spotter RBN spot, else a nearby cluster spot."""
        loc, now = (rx["lat"], rx["lon"]), time.time()
        fresh = lambda s: now - self.visited.get(round(s["khz"]), 0) > REVISIT
        near = [s for s in self.spots.heard_near(loc, SPOTTER_KM, self.bands) if fresh(s)]
        if near:
            return dict(near[0], why=f"RBN spotter {near[0]['spotter_km']} km from the receiver, "
                                     f"{near[0]['snr']} dB")
        allspots = asyncio.run_coroutine_threadsafe(self.spots.active(None, 600), self.loop).result(60)
        for s in sorted(allspots, key=lambda s: -(s.get("spotters") or 0)):
            dx = geo.locate(s["call"])
            if s["band"] in self.bands and fresh(s) and dx and geo.dist_bearing(loc, dx)[0] < CLUSTER_KM:
                return dict(s, why=f"cluster spot, {s.get('spotters')} spotters")
        return None

    def dwell(self, rx, spot):
        khz = spot["khz"]
        dial = khz - DIAL_OFFSET
        start = utcnow()
        name = f"{start:%Y%m%dT%H%M%SZ}_{khz:.1f}_{spot['call'].replace('/', '-')}"
        os.makedirs(AUDIO, exist_ok=True)
        meta = {"receiver": rx["url"], "receiver_loc": [rx["lat"], rx["lon"]], "dial_khz": dial,
                "mode": "usb", "passband_hz": [300, 2700], "target": {k: spot.get(k) for k in
                ("call", "khz", "band", "snr", "wpm", "why")}, "start_utc": start.isoformat(),
                "fs": FS, "format": "s16le", "pitch_track": []}
        if self.iq:
            meta.update(iq=name + ".iq", iq_format="s16le interleaved I, Q", iq_passband_hz=list(kiwi.IQ_PASSBAND))
        dec, hist, nbytes, copied, burst = CWDecoder(DIAL_OFFSET * 1000), np.zeros(0, np.float32), 0, "", None
        last_retune = 0.0
        st = kiwi.Stream(rx, dial, DWELL, iq=self.iq)
        usb = kiwi.IQToUSB() if self.iq else None
        iqf = open(os.path.join(AUDIO, name + ".iq"), "wb") if self.iq else None
        with open(os.path.join(AUDIO, name + ".raw"), "wb") as raw:
            while True:
                data = st.read(2400 if self.iq else 1200)      # 50 ms
                if not data:
                    break
                if self.iq:
                    iqf.write(data)
                    x = usb(kiwi.iq_samples(data))
                    data = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
                raw.write(data)
                nbytes += len(data)
                t = nbytes / 2 / FS
                x = np.frombuffer(data[:len(data) // 2 * 2], "<i2").astype(np.float32) / 32768
                hist = np.concatenate([hist, x])[-FS * 6:]
                # Retune between characters, never mid-word.
                if t - last_retune > 4 and len(hist) >= FS * 4 and (not burst or t - burst["t_last"] > 1):
                    last_retune = t
                    hz, c = keyed_peak(hist)
                    if c >= MIN_CONTRAST and abs(hz - dec.freq) > 15:
                        dec.set_freq(hz)
                        meta["pitch_track"].append([round(t, 2), hz, round(c, 1)])
                txt = dec.feed(x)
                copied += txt or ""
                if txt:
                    if burst is None:
                        burst = {"utc": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "audio": name + ".raw",
                                 "audio_s": round(t, 1), "freq": round(dial * 1000 + dec.freq),
                                 "pitch": round(dec.freq), "receiver": rx["url"], "target": spot["call"], "text": ""}
                    burst["text"] += txt
                    burst["wpm"], burst["t_last"] = round(dec.wpm, 1), t
                elif burst and t - burst["t_last"] > GAP:
                    self.save_burst(burst)
                    burst = None
                if t > GIVE_UP and not readable(copied):
                    meta["left_early"] = "nothing readable"
                    break
        if burst:
            self.save_burst(burst)
        if iqf:
            iqf.close()
        err = st.close()
        meta["seconds"] = round(nbytes / 2 / FS, 1)
        with open(os.path.join(AUDIO, name + ".json"), "w") as f:
            json.dump(meta, f, indent=1)
        return meta["seconds"], err

    @staticmethod
    def save_burst(b):
        b["text"] = " ".join(b["text"].split())
        b.pop("t_last", None)
        if b["text"]:
            with open(os.path.join(CORPUS, f"copy-{b['utc'][:10].replace('-', '')}.jsonl"), "a") as f:
                f.write(json.dumps(b) + "\n")

    def run(self, minutes):
        end = time.time() + minutes * 60
        time.sleep(20 if self.spots.rbn.call else 0)     # let the RBN feed fill
        while time.time() < end:
            rx = self.receiver()
            if not rx:
                print("no usable KiwiSDR in", self.region, flush=True)
                time.sleep(120)
                continue
            spot = self.target(rx)
            if not spot:
                print("no suitable spot yet", flush=True)
                time.sleep(60)
                continue
            self.visited[round(spot["khz"])] = time.time()
            print(f"{utcnow():%H:%M:%SZ} {rx['url']} -> {spot['khz']} {spot['call']} ({spot['why']})", flush=True)
            secs, err = self.dwell(rx, spot)
            print(f"  {secs:.0f} s of audio{'; ' + err if err else ''}", flush=True)
            if secs < 10:           # refused (full, one connection per IP, time limit) or dropped
                self.bad_rx.add(rx["url"])
                if self.fixed_rx:
                    time.sleep(120)


def stats():
    metas = [json.load(open(p)) for p in sorted(glob.glob(os.path.join(AUDIO, "*.json")))]
    bursts = [json.loads(l) for p in sorted(glob.glob(os.path.join(CORPUS, "copy-*.jsonl"))) for l in open(p)]
    secs = sum(m.get("seconds", 0) for m in metas)
    size = sum(os.path.getsize(p) for ext in ("raw", "iq") for p in glob.glob(os.path.join(AUDIO, "*." + ext)))
    print(f"{len(metas)} recordings, {secs / 60:.0f} min of audio ({size / 1e6:.0f} MB), {len(bursts)} bursts of copy")
    rx = {}
    for m in metas:
        rx[m["receiver"]] = rx.get(m["receiver"], 0) + m.get("seconds", 0)
    for k, v in sorted(rx.items(), key=lambda kv: -kv[1]):
        print(f"  {v / 60:5.0f} min  {k}")
    good = [b for b in bursts if readable(b["text"])]
    print(f"{len(good)} readable bursts, e.g.:")
    for b in good[-5:]:
        print(f"  {b['utc']} {b['freq'] / 1000:.1f} {b['text'][:100]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture", help="record traffic from a KiwiSDR")
    c.add_argument("--call", help="callsign for the RBN login (without it only DX-cluster spots are used)")
    c.add_argument("--region", default="europe", choices=sorted(kiwi.REGIONS))
    c.add_argument("--kiwi", help="host:port of a specific receiver instead of the best in the region")
    c.add_argument("--minutes", type=float, default=60)
    c.add_argument("--bands", nargs="+", default=["40", "30", "20"])
    c.add_argument("--iq", action="store_true", help="also keep the raw I/Q of every dwell (.iq, 48 kB/s)")
    sub.add_parser("stats", help="summarize the corpus")
    a = ap.parse_args()
    if a.cmd == "stats":
        return stats()
    rx = None
    if a.kiwi:
        host, _, port = a.kiwi.partition(":")
        url = f"http://{host}:{port or 8073}"
        rx = kiwi.summarize(url, kiwi.status(url) or {})
        if not rx:
            raise SystemExit(f"{a.kiwi}: not reachable or not usable")
    Capture(a.call, a.region, a.bands, rx, a.iq).run(a.minutes)


if __name__ == "__main__":
    main()
