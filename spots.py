"""Active CW stations from DX spotting networks.

Three sources, merged per callsign:
- DX Summit (http://www.dxsummit.fi) JSON API: human DX-cluster spots, no login.
- HamQTH (https://www.hamqth.com) DX-cluster CSV: human spots, no login.
- Reverse Beacon Network telnet (telnet.reversebeacon.net:7000): CW spotter spots
  with measured SNR and WPM. RBN asks for a callsign at login, so the feed only
  starts once ours is known (set_call).
"""
import asyncio
import calendar
import re
import time

import aiohttp

import geo

DXSUMMIT_URL = "http://www.dxsummit.fi/api/v1/spots"
HAMQTH_URL = "https://www.hamqth.com/dxc_csv.php"
RBN_HOST, RBN_PORT = "telnet.reversebeacon.net", 7000

# CW segments in kHz (band plan CW / narrow-band portions, below the digital calling freqs)
CW_SEGMENTS = {
    "160": (1800, 1838), "80": (3500, 3570), "60": (5351.5, 5354), "40": (7000, 7040),
    "30": (10100, 10130), "20": (14000, 14070), "17": (18068, 18095), "15": (21000, 21070),
    "12": (24890, 24915), "10": (28000, 28070), "6": (50000, 50100),
}
NOT_CW = re.compile(r"\b(FT8|FT4|RTTY|PSK\d*|JT65|JS8|SSB|USB|LSB|FM|AM|SSTV|MSK144|Q65|DIGI)\b", re.I)
CALL = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}[A-Z](?:/[A-Z0-9]{1,4})?$|^[A-Z0-9]{1,4}/[A-Z0-9]{3,8}$")
# DX de EA5WU-#:   14013.5  OH2BH          CW    17 dB  24 WPM  CQ      0519Z
RBN_LINE = re.compile(r"^DX de (\S+?):\s+([\d.]+)\s+(\S+)\s+(\w+)\s+(-?\d+) dB\s+(\d+) (?:WPM|BPS)\s+(\S+)")
MAX_AGE = 3600


def band_of(khz):
    for name, (lo, hi) in CW_SEGMENTS.items():
        if lo <= khz <= hi:
            return name
    return None


class RBNFeed:
    """Background telnet client that keeps the last hour of RBN CW spots."""

    def __init__(self):
        self.call = ""
        self.task = None
        self.spots = []
        self.connected = False
        self.error = ""

    def set_call(self, call):
        call = (call or "").upper().strip()
        if not CALL.match(call) or call == self.call:
            return
        self.call = call
        if self.task:
            self.task.cancel()
        self.task = asyncio.ensure_future(self._run())

    async def _run(self):
        while True:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(RBN_HOST, RBN_PORT), 20)
                try:
                    await asyncio.wait_for(reader.readuntil(b"call:"), 20)
                    writer.write(self.call.encode() + b"\r\n")
                    await writer.drain()
                    self.connected, self.error = True, ""
                    while True:
                        line = await asyncio.wait_for(reader.readline(), 300)
                        if not line:
                            break
                        self._parse(line.decode("ascii", "replace").strip())
                finally:
                    writer.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = str(e) or type(e).__name__
            self.connected = False
            await asyncio.sleep(30)

    def _parse(self, line):
        m = RBN_LINE.match(line)
        if not m or m.group(4) != "CW":
            return
        spotter, khz, call, _, snr, wpm, kind = m.groups()
        if kind in ("BEACON", "NCDXF"):
            return
        now = time.time()
        self.spots.append({"call": call, "khz": float(khz), "ts": now, "spotter": spotter.rstrip("-#"),
                           "snr": int(snr), "wpm": int(wpm), "info": kind, "source": "rbn"})
        if len(self.spots) > 20000 or self.spots[0]["ts"] < now - MAX_AGE:
            self.spots = [s for s in self.spots if s["ts"] >= now - MAX_AGE]

    def status(self):
        return {"call": self.call, "connected": self.connected, "error": self.error}


class Spots:
    def __init__(self):
        self.rbn = RBNFeed()
        self.dxs = []
        self.dxs_time = 0.0
        self.errors = {}

    def set_call(self, call):
        self.rbn.set_call(call)

    async def _cluster(self):
        """Refresh the web DX-cluster spots (DX Summit, HamQTH) at most once a minute."""
        if time.time() - self.dxs_time < 60:
            return
        self.dxs_time = time.time()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            results = await asyncio.gather(self._fetch(s, "dxsummit", self._dxsummit),
                                           self._fetch(s, "hamqth", self._hamqth))
        self.dxs = [x for spots in results for x in spots if not NOT_CW.search(x["info"])]

    async def _fetch(self, session, name, fn):
        try:
            spots = await fn(session)
            self.errors.pop(name, None)
            return spots
        except Exception as e:
            self.errors[name] = str(e) or type(e).__name__
            return []

    @staticmethod
    async def _dxsummit(session):
        async with session.get(DXSUMMIT_URL, params={"limit": 500, "include_modes": "CW"}) as r:
            data = await r.json(content_type=None)
        return [{"call": d["dx_call"], "khz": float(d["frequency"]),
                 "ts": calendar.timegm(time.strptime(d["time"], "%Y-%m-%dT%H:%M:%S")),
                 "spotter": d["de_call"].split("-")[0], "snr": None, "wpm": None,
                 "info": d.get("info") or "", "source": "dxsummit", "country": d.get("dx_country") or ""}
                for d in data]

    @staticmethod
    async def _hamqth(session):
        # spotter^freq^dx^comment^HHMM YYYY-MM-DD^lotw^eqsl^continent^band^country^dxcc
        async with session.get(HAMQTH_URL, params={"limit": 500}) as r:
            text = await r.text()
        spots = []
        for line in text.splitlines():
            f = line.split("^")
            if len(f) < 10:
                continue
            spots.append({"call": f[2], "khz": float(f[1]), "ts": calendar.timegm(time.strptime(f[4], "%H%M %Y-%m-%d")),
                          "spotter": f[0].split("-")[0], "snr": None, "wpm": None, "info": f[3],
                          "source": "hamqth", "country": f[9]})
        return spots

    async def active(self, band=None, max_age=900, me=None, pathcheck=None):
        """Stations spotted on CW within max_age seconds, newest first. One entry per
        call (per band): its latest frequency plus how many spotters heard it.
        me: our (lat, lon), adds km to each station. pathcheck: a PathCheck whose last
        result marks stations whose spotters are near a receiver that heard us."""
        await self._cluster()
        cutoff = time.time() - max_age
        by_call = {}
        for s in sorted(self.dxs + self.rbn.spots, key=lambda s: s["ts"]):
            b = band_of(s["khz"])
            if s["ts"] < cutoff or not b or (band and b != band) or s["call"] == self.rbn.call:
                continue
            e = by_call.setdefault((s["call"], b), {"call": s["call"], "band": b, "spotters": set(),
                                                    "sources": set(), "snr": None, "wpm": None,
                                                    "info": "", "country": ""})
            e["khz"], e["ts"] = s["khz"], s["ts"]          # latest spot wins
            e["spotters"].add(s["spotter"])
            e["sources"].add(s["source"])
            if s["snr"] is not None:
                e["snr"] = max(e["snr"] or -99, s["snr"])
            if s["wpm"]:
                e["wpm"] = s["wpm"]
            if s["source"] != "rbn" or not e["info"]:
                e["info"] = s["info"] or e["info"]
            e["country"] = s.get("country") or e["country"]
        out = []
        for e in sorted(by_call.values(), key=lambda e: -e["ts"]):
            if pathcheck:
                locs = [loc for loc in map(geo.locate, e["spotters"]) if loc]
                e["path"] = pathcheck.near_path(e["band"], locs)
            dx = geo.lookup(e["call"])
            e["km"] = round(geo.dist_bearing(me, (dx["lat"], dx["lon"]))[0]) if me and dx else None
            e["spotters"] = len(e["spotters"])
            e["sources"] = sorted(e["sources"])
            e["age"] = int(time.time() - e["ts"])
            out.append(e)
        return out

    def heard_near(self, loc, radius_km=800, bands=None, max_age=600):
        """CW stations that RBN spotters within radius_km of loc heard recently, loudest
        first: [{call, khz, band, snr, wpm, spotters, spotter_km, ts}]. A spotter near a
        receiver hearing a station well is measured evidence it is readable there, which
        worldwide spotter counts are not."""
        cutoff, best = time.time() - max_age, {}
        for s in self.rbn.spots:
            b = band_of(s["khz"])
            if s["ts"] < cutoff or not b or (bands and b not in bands):
                continue
            sl = geo.locate(s["spotter"])
            if not sl:
                continue
            km = geo.dist_bearing(loc, sl)[0]
            if km > radius_km:
                continue
            e = best.setdefault((s["call"], b), {"call": s["call"], "band": b, "snr": -99, "spotters": set()})
            e["spotters"].add(s["spotter"])
            if s["snr"] >= e["snr"]:
                e.update(khz=s["khz"], snr=s["snr"], wpm=s["wpm"], spotter_km=round(km), ts=s["ts"])
        out = sorted(best.values(), key=lambda e: -e["snr"])
        for e in out:
            e["spotters"] = len(e["spotters"])
        return out

    def status(self):
        return {"rbn": self.rbn.status(), "errors": self.errors}
