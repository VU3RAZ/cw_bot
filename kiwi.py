"""Public KiwiSDR receivers: find suitable ones and stream their audio.

The receiver list comes from ReceiverBook (receiverbook.de), cached for a day in
logs/kiwi_receivers.json; each candidate's /status page gives users, SNR, time
limits and its GPS position. Audio is streamed with kiwiclient (github.com/jks-prv/
kiwiclient, cloned into vendor/ on first use) as 12 kHz mono s16le.

A KiwiSDR accepts one connection per IP address: close its tab in the browser
before streaming from it here.
"""
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KIWICLIENT = os.path.join(HERE, "vendor", "kiwiclient")
RECORDER = os.path.join(KIWICLIENT, "kiwirecorder.py")
CACHE = os.path.join(HERE, "logs", "kiwi_receivers.json")
RECEIVERBOOK = "https://www.receiverbook.de/map?type=kiwisdr"
USER = "cw_bot"     # name shown in the receiver's user list
FS = 12000

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
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


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
    return {"url": url, "host": host, "port": int(port or 80), "free": users_max - users,
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
    with cf.ThreadPoolExecutor(16) as ex:
        for c, st in zip(cands, ex.map(lambda c: status(c[1]), cands)):
            r = st and summarize(c[1], st)
            if r:
                rows.append(r)
    return rank(rows)[:limit]


class Stream:
    """Audio from one KiwiSDR channel: USB with the dial at `dial_khz` and a 300-2700 Hz
    passband, so a CW signal at f kHz is heard at (f - dial_khz) * 1000 Hz."""

    def __init__(self, rx, dial_khz, seconds, lo=300, hi=2700):
        self.rx, self.dial_khz = rx, dial_khz
        self.p = subprocess.Popen(
            [sys.executable, ensure_kiwiclient(), "--nc", "-q", "-s", rx["host"], "-p", str(rx["port"]),
             "-u", USER, "-f", f"{dial_khz:.2f}", "-m", "usb", "-L", str(lo), "-H", str(hi),
             "-r", str(FS), "--tlimit", str(int(seconds))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def read(self, n=1200):
        """Up to n bytes of s16le audio; b"" when the stream has ended."""
        return self.p.stdout.read(n)

    def close(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait()
        err = self.p.stderr.read().decode(errors="replace").strip().splitlines()
        return err[-1] if err else ""


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="List usable public KiwiSDRs in a region.")
    ap.add_argument("region", nargs="?", default="europe", choices=sorted(REGIONS))
    ap.add_argument("-n", type=int, default=15)
    a = ap.parse_args()
    for r in find(a.region, a.n):
        print(f"snr {r['snr']:3} dB  free {r['free']:2}  {'limited ' if r['time_limited'] else 'no-limit'}  "
              f"{r['url']:45} {r['loc'][:35]} | {r['antenna'][:35]}")
