"""Path check: after we call CQ, find out who heard us.

Reports come from the Reverse Beacon Network (live, via spots.RBNFeed, which already
sees every CW spot including our own call) and from PSKReporter (queried once at the
end; its operators ask for at most one query per five minutes).
"""
import asyncio
import json
import os
import time
import xml.etree.ElementTree as ET

import aiohttp

import geo
from spots import band_of

PSK_URL = "https://retrieve.pskreporter.info/query"
PSK_MIN_INTERVAL = 300
NEAR_KM = 2000          # a spotter this close to one of our receivers counts as "same path"
RESULT_TTL = 3600       # how long a finished check is used to rank spots
SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "pathcheck_last.json")


class PathCheck:
    def __init__(self, app):
        self.app = app          # server.App: spots, broadcast_json
        self.task = None
        self.result = None      # {"call", "band", "start", "state", "reports": [...], ...}
        self.psk_last = 0.0
        self._load()

    def _load(self):
        """Last finished check (survives server restarts); re-learn its receivers' grids."""
        try:
            with open(SAVE_PATH) as f:
                self.result = json.load(f)
        except (OSError, ValueError):
            return
        for x in self.result["reports"]:
            if x.get("grid"):
                geo.learn_grid(x["receiver"], x["grid"])

    def _save(self):
        try:
            os.makedirs(os.path.dirname(SAVE_PATH), exist_ok=True)
            with open(SAVE_PATH, "w") as f:
                json.dump(self.result, f, indent=1)
        except OSError:
            pass

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    def begin(self, call, grid, khz, listen_secs=240):
        """Start collecting reports of `call` from now on (call before the first CQ)."""
        if self.active:
            self.task.cancel()
        me = geo.grid_to_latlon(grid)
        if me is None and geo.lookup(call):
            e = geo.lookup(call)
            me = (e["lat"], e["lon"])
        self.result = {"call": call, "band": band_of(khz), "khz": khz, "start": time.time(),
                       "end": time.time() + listen_secs, "me": me, "state": "collecting",
                       "reports": [], "psk": "pending", "dry_run": False}
        self.task = asyncio.ensure_future(self._run())
        self._broadcast()

    def mark_dry_run(self):
        if self.result:
            self.result["dry_run"] = True

    def cancel(self, reason="cancelled"):
        if self.active:
            self.task.cancel()
            self.result["state"] = reason
            self._broadcast()

    async def _run(self):
        r = self.result
        while time.time() < r["end"]:
            self._collect_rbn()
            self._broadcast()
            await asyncio.sleep(5)
        self._collect_rbn()
        if r["dry_run"]:
            r["psk"] = "skipped (dry run)"
        elif time.time() - self.psk_last < PSK_MIN_INTERVAL:
            r["psk"] = "skipped (queried less than 5 min ago)"
        else:
            await self._collect_psk()
        r["state"] = "done"
        self._save()
        self._broadcast()

    def _add(self, source, receiver, snr, khz, ts, grid=""):
        """One row per receiver; many receivers report to both RBN and PSKReporter."""
        r = self.result
        if grid:
            geo.learn_grid(receiver, grid)
        x = next((x for x in r["reports"] if x["receiver"] == receiver), None)
        if x is None:
            ent = geo.lookup(receiver) or {}
            x = {"receiver": receiver, "sources": [], "country": ent.get("country", ""),
                 "cont": ent.get("cont", ""), "grid": "", "snr": None, "khz": khz, "ts": ts}
            r["reports"].append(x)
        if source not in x["sources"]:
            x["sources"].append(source)
        if snr is not None and (x["snr"] is None or snr > x["snr"]):
            x["snr"] = snr
        x["ts"] = min(x["ts"], ts)
        x["grid"] = x["grid"] or grid
        # location: a locator (from this report or learned earlier) beats the country centre
        x["loc"] = geo.locate(receiver)
        km = brg = None
        if x["loc"] and r["me"]:
            km, brg = geo.dist_bearing(r["me"], x["loc"])
        x["km"] = round(km) if km is not None else None
        x["bearing"] = round(brg) if brg is not None else None

    def _collect_rbn(self):
        r = self.result
        for s in self.app.spots.rbn.spots:
            if s["call"] == r["call"] and s["ts"] >= r["start"] and band_of(s["khz"]) == r["band"]:
                self._add("rbn", s["spotter"], s["snr"], s["khz"], s["ts"])

    async def _collect_psk(self):
        r = self.result
        self.psk_last = time.time()
        params = {"senderCallsign": r["call"], "flowStartSeconds": -int(time.time() - r["start"] + 60),
                  "rronly": 1, "noactive": 1, "mode": "CW"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.get(PSK_URL, params=params, headers={"User-Agent": "cw_bot path check"}) as resp:
                    text = await resp.text()
            n = 0
            for e in ET.fromstring(text).iter("receptionReport"):
                khz = int(e.get("frequency", 0)) / 1000
                ts = int(e.get("flowStartSeconds", 0))
                if band_of(khz) != r["band"] or ts < r["start"] - 30:
                    continue
                snr = e.get("sNR")
                self._add("pskreporter", e.get("receiverCallsign", "?"), int(snr) if snr else None,
                          khz, ts, e.get("receiverLocator", ""))
                n += 1
            r["psk"] = f"{n} reports" + (" (receivers RBN already had are merged)" if n else "")
        except Exception as e:
            r["psk"] = f"error: {e or type(e).__name__}"

    def _broadcast(self):
        self.app.broadcast_json(type="pathcheck", result=self.public())

    def public(self):
        if not self.result:
            return None
        r = dict(self.result)
        r["reports"] = sorted(r["reports"], key=lambda x: -(x["snr"] if x["snr"] is not None else -99))
        r["remaining"] = max(0, int(r["end"] - time.time())) if r["state"] == "collecting" else 0
        return r

    def receivers(self, band):
        """Locations of receivers that heard us on `band` in a recent check."""
        r = self.result
        if not r or r["band"] != band or time.time() - r["start"] > RESULT_TTL:
            return []
        return [x for x in r["reports"] if x["loc"]]

    def near_path(self, band, spotter_locs):
        """Closest (km, receiver) between any of a station's spotters and our receivers."""
        best = None
        for x in self.receivers(band):
            for loc in spotter_locs:
                km, _ = geo.dist_bearing(loc, x["loc"])
                if km <= NEAR_KM and (best is None or km < best[0]):
                    best = (round(km), x["receiver"])
        return best
