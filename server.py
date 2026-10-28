#!/usr/bin/env python3
"""IC-7300 web control: CAT, panadapter, audio waterfall, CW/RTTY/FT8 decode.

Usage: python3 server.py [--port 8073] [--serial /dev/ttyUSB0] [--audio NODE]
Then open http://localhost:8073
"""
import argparse
import asyncio
import json
import os
import queue
import shutil
import struct
import subprocess
import threading
import time

import numpy as np
from aiohttp import web, WSMsgType

from cwparse import OverReader
from decoders import FS, Spectrum, CWDecoder, PitchTracker, RTTYDecoder, FT8Decoder
from cwhmm import CWHmm
from radio import IC7300
import rig
import clock
from robot import CWRobot
from skimmer import MultiSkimmer
from spots import Spots
from pathcheck import PathCheck
from beacons import BeaconMonitor
import geo
import kiwi
import kiwisdr

HERE = os.path.dirname(os.path.abspath(__file__))
MSG_SCOPE, MSG_AUDIO, MSG_PCM = 1, 2, 3
BEACON_LOCKED = {"freq", "mode", "ptt", "tune", "send_cw"}   # the beacon monitor owns the radio


class App:
    def __init__(self, args):
        self.args = args
        self.loop = None
        self.clients = set()
        # the last used radio if it is plugged in, else whichever is plugged in
        detected = [r["name"] for r in rig.available() if r["connected"] and not r.get("network")]
        last = rig.load_settings().get("radio")
        self.radio_name = (args.radio or (last if last in detected else None)
                           or (detected[0] if detected else None) or last or "IC-7300")
        self.radio = self.make_radio(self.radio_name, startup=True)
        self.audio = None
        self.clock = clock.status()     # PC clock vs NTP, rechecked every minute
        self.spectrum = Spectrum()
        self.cw = CWDecoder(600)   # IC-7300 default CW pitch
        self.cw2 = None            # second CW decoder on the same audio ("both"), follows cw's pitch
        self.cw_engine = "standard"
        self.set_cw_engine(rig.load_settings().get("cw_engine", "standard"))
        self.cw_afc = bool(rig.load_settings().get("cw_afc", True))
        self.afc = PitchTracker()
        self.afc_sent, self.afc_shown = 0.0, 0.0    # last AFC update sent to the browser
        self.overs = OverReader()   # the CW copy read over by over, for the tile's Parsed view
        # KiwiSDR skimmer: I/Q blocks go to a worker thread, so decoding dozens of
        # channels never holds up the event loop (websockets, audio)
        self.skimmer = MultiSkimmer()
        self.skim_q = queue.Queue(maxsize=50)
        self.skim_cq, self.skim_cq_t = [], 0.0  # stations calling CQ, from the skimmer (Hunt answers these)
        threading.Thread(target=self.skim_worker, daemon=True).start()
        self.cw_anchor = self.cw.freq   # where the decoder was put (pitch or a click): AFC searches near it
        self.rtty = RTTYDecoder(2210)
        self.ft8 = FT8Decoder()
        self.enabled = {"cw": True, "rtty": False, "ft8": False}
        self.cw_warning = ""
        self.audio_ok = False
        self.ft8_busy = False
        self.listeners = set()      # websockets that want the receive audio
        self.robot = CWRobot(self)
        self.spots = Spots()
        self.pathcheck = PathCheck(self)
        self.beacons = BeaconMonitor(self)
        self.last_audio = 0.0
        self.kiwi_lists = {}        # region -> (time, rows): kiwi.find takes up to a minute

    # ---- broadcast helpers (thread-safe) -----------------------------
    def _send_all(self, data):
        for ws in list(self.clients):
            if ws.closed:
                self.clients.discard(ws)
                continue
            coro = ws.send_bytes(data) if isinstance(data, bytes) else ws.send_str(data)
            asyncio.ensure_future(coro)

    def broadcast(self, data):
        if self.loop:
            self.loop.call_soon_threadsafe(self._send_all, data)

    def broadcast_json(self, **msg):
        self.broadcast(json.dumps(msg))

    # ---- radio callbacks (reader thread) -----------------------------
    def on_state(self, changed):
        self.broadcast_json(type="state", state=changed)
        if "cw_pitch" in changed and self.loop:
            # keep the CW decoder on the radio's sidetone pitch (where a zero-beat signal lands)
            self.loop.call_soon_threadsafe(self.follow_pitch, changed["cw_pitch"])
        if "freq" in changed:
            self.afc.reset()        # a QSY: the last seconds of audio are another station

    def on_scope(self, lo, hi, px):
        self.broadcast(struct.pack("<Bii", MSG_SCOPE, lo, hi) + px)

    def decoder_status(self):
        return {"cw_freq": self.cw.freq, "cw_wpm": round(self.cw.wpm, 1), "cw_engine": self.cw_engine,
                "cw_squelch": self.cw.squelch, "cw_afc": self.cw_afc, "cw_warning": self.cw_warning, "rtty_freq": self.rtty.center, "rtty_mark_high": self.rtty.mark_high,
                "rtty_shift": self.rtty.shift, "rtty_baud": self.rtty.baud,
                "ft4": self.ft8.ft4, "enabled": self.enabled, "audio": self.audio_ok}

    # ---- radio selection ----------------------------------------------
    def make_radio(self, name, startup=False):
        a = self.args
        if name in rig.NETWORK:
            return rig.make(name)
        port = a.serial if startup and a.serial else rig.find_port(name)
        if name == "IC-7300":
            return IC7300(port, a.baud, int(a.civ, 16))
        return rig.make(name, port)

    def radios_info(self, avail=None):
        return {"type": "radios", "current": self.radio_name, "radios": avail or rig.available(), "caps": self.radio.caps,
                "kiwi": {"receiver": (rig.load_settings().get("kiwi") or {}).get("url"),
                         "regions": list(kiwi.REGIONS), "region": self.kiwi_region()}}

    def kiwi_region(self):
        """Region for the receiver list: the last one used, else the station's own, else Europe."""
        r = rig.load_settings().get("kiwi_region")
        if r in kiwi.REGIONS:
            return r
        loc = self.my_location()
        for name, (la0, la1, lo0, lo1) in kiwi.REGIONS.items():
            if loc and la0 <= loc[0] <= la1 and lo0 <= loc[1] <= lo1:
                return name
        return "europe"

    async def kiwi_list(self, region, refresh=False):
        """Public KiwiSDRs in a region, best first; the first one is the recommended one."""
        if region not in kiwi.REGIONS:
            raise RuntimeError(f"unknown region {region}")
        rig.save_settings(kiwi_region=region)
        t, rows = self.kiwi_lists.get(region, (0, None))
        if refresh or rows is None or time.time() - t > 600:
            self.broadcast_json(type="kiwi_list", region=region, loading=True)
            rows = await self.loop.run_in_executor(None, kiwi.find, region, 25)
            self.kiwi_lists[region] = (time.time(), rows)
        me = self.my_location()
        out = []
        for r in rows:
            t, why = kiwisdr.REFUSED.get(r["url"], (0, ""))
            out.append(dict(r, km=round(geo.dist_bearing(me, (r["lat"], r["lon"]))[0]) if me else None,
                            refused=why if time.time() - t < 1800 else ""))
        rec = next((r["url"] for r in out if not r["refused"]), None)
        self.broadcast_json(type="kiwi_list", region=region, receivers=out, recommended=rec,
                            age=round(time.time() - self.kiwi_lists[region][0]))

    def background(self, coro):
        """Run a slow command without holding up this client's websocket; errors go to everyone."""
        async def run():
            try:
                await coro
            except Exception as e:
                self.broadcast_json(type="error", msg=str(e))
        asyncio.ensure_future(run())

    async def kiwi_select(self, url):
        rx = next((r for _, rows in self.kiwi_lists.values() for r in rows if r["url"] == url), None)
        if rx is None:
            rx = await self.loop.run_in_executor(None, lambda: kiwi.summarize(url, kiwi.status(url) or {}))
            if not rx:
                raise RuntimeError(f"{url}: not reachable or not usable")
        rig.save_settings(kiwi=rx)
        if self.radio_name == "KiwiSDR":
            self.radio.set_receiver(rx)
        else:
            await self.select_radio("KiwiSDR")
        self.broadcast_json(**self.radios_info())

    def clock_msg(self):
        return {"type": "clock", "clock": self.clock, "text": clock.describe(self.clock)}

    async def clock_watch(self):
        """The beacon monitor and FT8 depend on the PC clock: recheck NTP sync every minute."""
        while True:
            try:
                s = await self.loop.run_in_executor(None, clock.status)
                if s.get("ok") != self.clock.get("ok"):
                    print(clock.describe(s))
                    self.broadcast_json(type="notice", msg=clock.describe(s))
                self.clock = s
                self.broadcast_json(**self.clock_msg())
            except Exception as e:
                print("clock check:", e)
            await asyncio.sleep(60)

    async def radio_watch(self):
        """Every 2 s: which radios are plugged in. Updates the dropdown, follows a radio that
        comes back on another port, and switches to a plugged-in radio when the one in use
        is unplugged (not while the robot or beacon monitor runs)."""
        last = None
        while True:
            await asyncio.sleep(2)
            try:
                avail = await self.loop.run_in_executor(None, rig.available)
            except Exception as e:
                print("radio detection:", e)
                continue
            key = [(r["name"], r["connected"], r["port"]) for r in avail]
            if key == last:
                continue
            if last is not None:
                was = {n: c for n, c, _ in last}
                for n, c, _ in key:
                    if c != was.get(n):
                        self.broadcast_json(type="notice", msg=f"{n} {'detected' if c else 'disconnected'}")
            last = key
            cur = next(r for r in avail if r["name"] == self.radio_name)
            if not cur.get("network") and cur["connected"] and cur["port"] != self.radio.port:
                print(f"{self.radio_name} is now on {cur['port']}")
                self.radio.port = cur["port"]          # the driver reopens it on its next attempt
            other = next((r["name"] for r in avail if r["connected"] and not r.get("network")
                          and r["name"] != self.radio_name), None)
            if not cur["connected"] and other:
                if self.robot.active or self.beacons.active:
                    self.broadcast_json(type="notice", msg=f"{self.radio_name} is not connected; {other} is - "
                                                           "stop the robot / beacon monitor to switch")
                else:
                    await self.select_radio(other)
                    self.broadcast_json(type="notice", msg=f"switched to {other} ({cur['name']} not connected)")
                    continue
            self.broadcast_json(**self.radios_info(avail))

    def audio_node(self):
        if getattr(self.radio, "has_audio", False):
            return None
        return self.args.audio or rig.audio_node_for(self.radio.port)

    def attach_radio(self):
        self.radio.on_state = self.on_state
        self.radio.on_scope = self.on_scope
        if hasattr(self.radio, "set_skim"):
            self.radio.on_iq = self.on_iq
            if rig.load_settings().get("skimmer"):
                self.radio.set_skim(True)
        self.skim_q.put(None)               # a new radio: drop the old channels
        self.radio.start()
        if self.audio:
            self.audio.cancel()
        self.audio_ok = False
        self.audio = asyncio.ensure_future(self.audio_task())

    async def select_radio(self, name):
        if name not in rig.RADIOS:
            raise RuntimeError(f"unknown radio {name}")
        if name == self.radio_name:
            return
        if self.robot.active or self.beacons.active:
            raise RuntimeError("stop the CW robot / beacon monitor before switching radios")
        old = self.radio
        await self.loop.run_in_executor(None, old.stop)
        self.radio_name, self.radio = name, self.make_radio(name)
        rig.save_settings(radio=name)
        self.attach_radio()
        self.broadcast_json(**self.radios_info())
        self.broadcast_json(type="state", state=self.radio.state)
        self.broadcast_json(type="decoder", status=self.decoder_status())

    # ---- audio --------------------------------------------------------
    async def audio_task(self):
        if getattr(self.radio, "has_audio", False):
            return await self.network_audio()
        node = self.audio_node()
        while True:
            if shutil.which("pw-record") and node:
                cmd = ["pw-record", "--target", node, "--rate", str(FS), "--channels", "1",
                       "--format", "s16", "--latency", "50ms", "--raw", "-"]
            else:
                cmd = ["arecord", "-q", "-D", self.args.alsa, "-f", "S16_LE", "-r", str(FS),
                       "-c", "1", "-t", "raw"]
            print("audio:", " ".join(cmd))
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL)
            chunk = FS // 10 * 2  # 100 ms
            try:
                while True:
                    self.feed_pcm(await proc.stdout.readexactly(chunk))
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                if proc.returncode is None:
                    proc.kill()
            self.audio_ok = False
            self.broadcast_json(type="decoder", status=self.decoder_status())
            print("audio stream ended; restarting in 2 s")
            await asyncio.sleep(2)
            node = self.audio_node()

    def feed_pcm(self, data):
        """100 ms of receive audio, 12 kHz s16le mono, from the sound card or the radio itself."""
        self.last_audio = time.time()
        if not self.audio_ok:
            self.audio_ok = True
            self.broadcast_json(type="decoder", status=self.decoder_status())
        x = np.frombuffer(data, np.int16).astype(np.float32) / 32768
        self.process_audio(x, time.time())
        self.stream_audio(data)

    async def network_audio(self):
        """A radio that brings its own audio (KiwiSDR): it calls feed_pcm; flag gaps."""
        self.radio.on_audio = self.feed_pcm
        try:
            while True:
                await asyncio.sleep(1)
                if self.audio_ok and time.time() - self.last_audio > 3:
                    self.audio_ok = False
                    self.broadcast_json(type="decoder", status=self.decoder_status())
        finally:
            self.radio.on_audio = None

    def stream_audio(self, pcm):
        if self.listeners:
            msg = bytes([MSG_PCM]) + pcm
            for ws in list(self.listeners):
                if ws.closed:
                    self.listeners.discard(ws)
                else:
                    asyncio.ensure_future(ws.send_bytes(msg))

    def spot_hints(self):
        """Calls spotted within 1 kHz of the dial in the last 30 min: extra votes when the
        Parsed view repairs a call (cwparse.correct_calls). Refreshed every 10 s."""
        now = time.time()
        if now - getattr(self, "_hints_t", 0) < 10:
            return
        self._hints_t = now
        khz = (self.radio.state.get("freq") or 0) / 1000
        self.overs.hints = self.spotted_near(khz, 1)

    def spotted_near(self, khz, width_khz):
        """{call: 1} spotted within width_khz of khz in the last 30 min."""
        now = time.time()
        return {s["call"]: 1 for s in self.spots.dxs + self.spots.rbn.spots
                if abs(s["khz"] - khz) <= width_khz and now - s["ts"] < 1800}

    # ---- KiwiSDR skimmer (worker thread) ---------------------------------
    def on_iq(self, iq, fc, rate):
        try:
            self.skim_q.put_nowait((iq, fc, rate))
        except queue.Full:
            pass                            # the worker is behind: skip a block rather than lag

    def skim_worker(self):
        sent = hints_t = 0.0
        while True:
            item = self.skim_q.get()
            now = time.time()
            if item is None:
                self.skimmer.clear()
                sent = 0.0
            else:
                iq, fc, rate = item
                if now - hints_t > 10:
                    hints_t = now
                    self.skimmer.hints = self.spotted_near(fc / 1000, self.skimmer.span / 1000)
                    self.skimmer.mycall = self.robot.cfg["mycall"]
                try:
                    self.skimmer.feed(iq, fc, rate, now)
                except Exception as e:      # never let one bad block stop the thread
                    print("skimmer:", e)
            if now - sent >= 1 or item is None:
                sent = now
                self.skim_cq = self.skimmer.cqs(now) if self.radio.state.get("skim") else []
                self.skim_cq_t = now
                self.broadcast_json(type="skimmer", **self.skim_status(now))

    def skim_status(self, now=None):
        on = bool(self.radio.state.get("skim"))
        return {"on": on, "active": on and str(self.radio.state.get("mode")) in ("CW", "CW-R"),
                "fc": self.skimmer.fc, "span": self.skimmer.span, "wide": self.skimmer.wide,
                "channels": len(self.skimmer.channels),
                "rows": self.skimmer.rows(now) if on else []}

    def mode_family(self):
        """cw / data / voice, as the page shows its tiles: data is RTTY or any mode with the
        data flag (USB-D...). Only the decoders for the current family run."""
        st = self.radio.state
        mode = str(st.get("mode") or "")
        if mode.startswith("CW"):
            return "cw"
        if mode.startswith("RTTY") or st.get("data"):
            return "data"
        return "voice" if mode else "cw"

    def process_audio(self, x, t_end):
        self.beacons.feed(x, t_end)
        for row in self.spectrum.feed(x):
            self._send_all(bytes([MSG_AUDIO]) + row)
        fam = self.mode_family()
        if self.enabled["cw"] and fam == "cw":
            if self.cw_afc:
                self.track_pitch(x)
            txt = self.cw.feed(x)
            mycall = self.robot.cfg["mycall"]
            self.spot_hints()
            overs = self.overs.feed(txt, time.time(), mycall) if txt else self.overs.tick(time.time(), mycall)
            if txt:
                self.broadcast_json(type="cw", text=txt, wpm=round(self.cw.wpm, 1))
                self.robot.on_rx(txt)
            for o in overs:
                self.broadcast_json(type="cw_over", over=o)
            if self.cw2:
                if self.cw2.freq != self.cw.freq:
                    self.cw2.retune(self.cw.freq)
                txt = self.cw2.feed(x)
                if txt:
                    self.broadcast_json(type="cw2", text=txt, wpm=round(self.cw2.wpm, 1))
        if self.enabled["rtty"] and fam == "data":
            txt = self.rtty.feed(x)
            if txt:
                self.broadcast_json(type="rtty", text=txt)
        if self.enabled["ft8"] and fam == "data":
            done = self.ft8.feed(x, t_end)
            if done and not self.ft8_busy:
                asyncio.ensure_future(self.run_ft8(*done))

    async def run_ft8(self, slot_start, buf_start, audio):
        self.ft8_busy = True
        try:
            res = await self.loop.run_in_executor(None, self.ft8.decode, slot_start, buf_start, audio)
            self.broadcast_json(type="ft8", slot=time.strftime("%H:%M:%S", time.gmtime(slot_start)),
                                decodes=res, ft4=self.ft8.ft4)
        finally:
            self.ft8_busy = False

    # ---- websocket ----------------------------------------------------
    async def ws_handler(self, request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        self.clients.add(ws)
        await ws.send_str(json.dumps(self.radios_info()))
        await ws.send_str(json.dumps(self.clock_msg()))
        await ws.send_str(json.dumps({"type": "state", "state": self.radio.state}))
        await ws.send_str(json.dumps({"type": "decoder", "status": self.decoder_status()}))
        await ws.send_str(json.dumps({"type": "robot", "status": self.robot.status()}))
        await ws.send_str(json.dumps({"type": "pathcheck", "result": self.pathcheck.public()}))
        await ws.send_str(json.dumps({"type": "beacons", "data": self.beacons.public()}))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        m = json.loads(msg.data)
                        if m.get("cmd") == "listen":
                            (self.listeners.add if m.get("on") else self.listeners.discard)(ws)
                            continue
                        await self.handle_cmd(m)
                    except Exception as e:
                        await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))
        finally:
            self.clients.discard(ws)
            self.listeners.discard(ws)
        return ws

    async def handle_cmd(self, m):
        r = self.radio
        c = m.get("cmd")
        if c in BEACON_LOCKED and self.beacons.active:
            raise RuntimeError("NCDXF beacon monitor is running: stop it to tune, change mode or transmit")
        run = lambda f, *a: self.loop.run_in_executor(None, f, *a)
        if c == "freq":
            await run(r.set_freq, m["hz"])
        elif c == "mode":
            await run(r.set_mode, m["mode"], m.get("filter"), m.get("data"))
        elif c == "level":
            await run(r.set_level, m["name"], m["value"])
        elif c == "preamp":
            await run(r.set_preamp, m["value"])
        elif c == "att":
            await run(r.set_att, m["value"])
        elif c == "nb":
            await run(r.set_nb, m["value"])
        elif c == "nr":
            await run(r.set_nr, m["value"])
        elif c == "agc":
            await run(r.set_agc, m["value"])
        elif c == "ptt":
            await run(r.set_ptt, bool(m["value"]))
        elif c == "tune":
            await run(r.tune)
        elif c == "scope_span":
            await run(r.set_scope_span, m["value"])
        elif c == "scope_mode":
            await run(r.set_scope_mode, m["value"])
        elif c == "scope_speed":
            await run(r.set_scope_speed, m["value"])
        elif c == "select_radio":
            await self.select_radio(m["name"])
        elif c == "radios":
            self.broadcast_json(**self.radios_info())
        elif c == "kiwi_list":
            self.background(self.kiwi_list(m.get("region") or self.kiwi_region(), bool(m.get("refresh"))))
        elif c == "kiwi_select":
            await self.kiwi_select(m["url"])
        elif c == "skimmer":
            if not hasattr(r, "set_skim"):
                raise RuntimeError("the skimmer needs a KiwiSDR (it decodes the receiver's I/Q)")
            r.set_skim(bool(m["on"]))
            rig.save_settings(skimmer=bool(m["on"]))
            if not m["on"]:
                self.skim_q.put(None)
        elif c == "send_cw":
            await run(r.send_cw, m["text"])
        elif c == "stop_cw":
            await run(r.stop_cw)
        elif c == "robot_config":
            self.robot.configure(m.get("cfg", {}))
            self.spots.set_call(self.robot.cfg["mycall"])
            self.broadcast_json(type="robot", status=self.robot.status())
            self.beacons.broadcast()       # distances use the station's grid
        elif c == "robot_start":
            self.robot.configure(m.get("cfg", {}))
            self.spots.set_call(self.robot.cfg["mycall"])
            self.robot.start(m["mode"], m.get("target"), m.get("other"))
        elif c == "robot_stop":
            await self.robot.stop()
        elif c == "beacon_start":
            self.beacons.start(m.get("cfg", {}))
            self.beacons.broadcast()
        elif c == "beacon_stop":
            await self.beacons.stop()
        elif c == "qso_edit":
            self.robot.edit_qso(m["field"], m.get("value", ""))
        elif c == "robot_inject":
            self.robot.inject(m["text"])
        elif c == "spots":
            if m.get("call"):
                self.spots.set_call(m["call"])
            stations = await self.spots.active(m.get("band"), int(m.get("max_age", 900)),
                                               self.my_location(), self.pathcheck)
            self.broadcast_json(type="spots", band=m.get("band"), stations=stations, status=self.spots.status())
        elif c == "decoder":
            self.set_decoder(m)
            self.broadcast_json(type="decoder", status=self.decoder_status())

    def track_pitch(self, x):
        """AFC (decoders.PitchTracker): find the keyed tone within ~2 s, then follow it
        to about a hertz. In CW mode it searches the radio's filter passband; otherwise
        +-150 Hz around where the decoder was put."""
        st = self.radio.state
        pitch, width = st.get("cw_pitch") or 0, st.get("if_width") or 0
        if str(st.get("mode", "")).startswith("CW") and pitch and width:
            lo, hi = pitch - width / 2 - 25, pitch + width / 2 + 25
        else:
            lo, hi = self.cw_anchor - 150, self.cw_anchor + 150
        hz = self.afc.feed(x, self.cw, max(200, lo), min(3000, hi))
        if hz is None:
            return
        jump = abs(hz - self.cw.freq) > 50
        self.cw.retune(hz) if not jump else self.cw.set_freq(hz)
        # the loop nudges up to 10x a second: tell the browser on a jump, else 4x a second
        now = time.time()
        if jump or (now - self.afc_sent > 0.25 and abs(hz - self.afc_shown) >= 1):
            self.afc_sent, self.afc_shown = now, hz
            self.broadcast_json(type="decoder", status=self.decoder_status())

    def follow_pitch(self, pitch):
        self.cw.set_freq(pitch)
        self.cw_anchor = pitch
        self.afc.reset()
        self.cw_warning = ""
        self._send_all(json.dumps({"type": "decoder", "status": self.decoder_status()}))

    def outside_passband(self, f):
        """In CW mode the radio's IF filter passes only cw_pitch +- if_width/2: a decoder
        pitch outside it hears filter skirts and noise (copy like 'EE I EE'). Returns
        the reason to refuse f, or ""."""
        st = self.radio.state
        pitch, width = st.get("cw_pitch") or 0, st.get("if_width") or 0
        if not str(st.get("mode", "")).startswith("CW") or not pitch or not width:
            return ""
        half = max(width / 2, 100) + 50        # a little slack for an off-zero-beat signal
        if abs(f - pitch) <= half:
            return ""
        return f"{f:.0f} Hz is outside the CW filter ({pitch - width / 2:.0f}-{pitch + width / 2:.0f} Hz)"

    def set_cw_engine(self, name):
        """standard / hmm: which decoder copies (and feeds the robot); both: standard,
        plus the HMM's copy shown alongside on the same audio for comparison."""
        if name not in ("standard", "hmm", "both"):
            return
        make = CWHmm if name == "hmm" else CWDecoder
        sq = self.cw.squelch                # one SQL slider for whichever decoders run
        if not isinstance(self.cw, make):
            self.cw = make(self.cw.freq, squelch=sq)
        self.cw2 = CWHmm(self.cw.freq, squelch=sq) if name == "both" else None
        self.cw_engine = name

    def set_decoder(self, m):
        if "cw_engine" in m:
            self.set_cw_engine(m["cw_engine"])
            rig.save_settings(cw_engine=self.cw_engine)
        if "enable" in m:
            for k, v in m["enable"].items():
                if k in self.enabled:
                    self.enabled[k] = bool(v)
        if "cw_freq" in m:
            f = float(m["cw_freq"])
            if m.get("snap"):
                f = self.spectrum.peak_near(f)
            self.cw_warning = self.outside_passband(f)
            if not self.cw_warning:
                self.cw.set_freq(f)
                self.cw_anchor = f
                self.afc.reset()
        if "cw_afc" in m:
            self.cw_afc = bool(m["cw_afc"])
            rig.save_settings(cw_afc=self.cw_afc)
        if "cw_squelch" in m:
            for d in (self.cw, self.cw2):
                if d:
                    d.squelch = float(m["cw_squelch"])
        if "rtty_freq" in m or "rtty_mark_high" in m:
            self.rtty.set_freq(float(m.get("rtty_freq", self.rtty.center)), m.get("rtty_mark_high"))
        if "ft4" in m:
            self.ft8.set_ft4(bool(m["ft4"]))

    async def spots_handler(self, request):
        q = request.query
        stations = await self.spots.active(q.get("band"), int(q.get("max_age", 900)),
                                           self.my_location(), self.pathcheck, q.get("mode"))
        return web.json_response({"stations": stations, "status": self.spots.status(),
                                  "pathcheck": self.pathcheck.public()})

    def my_location(self):
        """Our (lat, lon): the robot's grid locator, else the callsign's country centre."""
        loc = geo.grid_to_latlon(self.robot.cfg["grid"])
        e = None if loc else geo.lookup(self.robot.cfg["mycall"] or self.spots.rbn.call)
        return loc or ((e["lat"], e["lon"]) if e else None)

    # ---- lifecycle ----------------------------------------------------
    async def on_startup(self, app):
        self.loop = asyncio.get_running_loop()
        self.watch = asyncio.ensure_future(self.radio_watch())
        self.clockwatch = asyncio.ensure_future(self.clock_watch())
        print(clock.describe(self.clock))
        if self.args.rbn_call:
            self.spots.set_call(self.args.rbn_call)
        self.attach_radio()

    async def on_cleanup(self, app):
        await self.robot.stop("server shutdown")
        await self.beacons.stop()
        self.watch.cancel()
        self.clockwatch.cancel()
        self.audio.cancel()
        self.radio.stop()


@web.middleware
async def no_cache(request, handler):
    """Make browsers revalidate the UI files so edits show up without a hard reload."""
    resp = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8073)
    ap.add_argument("--radio", choices=list(rig.RADIOS), help="radio to start with (default: last used)")
    ap.add_argument("--settings", default=None, help="settings file (default settings.json)")
    ap.add_argument("--serial", default=None, help="CAT serial port of the starting radio (auto-detect)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--civ", default="94", help="radio CI-V address, hex")
    ap.add_argument("--audio", default=None, help="PipeWire source node name (auto-detect)")
    ap.add_argument("--rbn-call", default=None, help="callsign for the Reverse Beacon Network login (CW spots)")
    ap.add_argument("--alsa", default="plughw:CODEC,0", help="ALSA device if pw-record missing")
    args = ap.parse_args()
    if args.settings:
        rig.SETTINGS = args.settings

    a = App(args)
    app = web.Application(middlewares=[no_cache])
    app.on_startup.append(a.on_startup)
    app.on_cleanup.append(a.on_cleanup)
    app.router.add_get("/ws", a.ws_handler)
    app.router.add_get("/api/spots", a.spots_handler)
    app.router.add_get("/", lambda r: web.FileResponse(os.path.join(HERE, "static", "index.html")))
    app.router.add_static("/static", os.path.join(HERE, "static"))
    print(f"Open http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
