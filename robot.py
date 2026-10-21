"""CW robot: runs a complete Morse QSO using a local LLM (gpt-oss on llama.cpp).

Split of work:
  * the LLM reads each received over (imperfect decoder copy) and reports who is
    calling, their report/name/QTH, and whether they asked for a repeat or sent 73;
  * the code owns the QSO: calling CQ, turn-taking, which message goes next (built
    from standard templates), timeouts, transmit safety limits and the log.
"""
import asyncio
import datetime as dt
import json
import os
import re
import time

import aiohttp

from decoders import MORSE
from spots import band_of
import geo

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")

MORSE_REV = {v: k for k, v in MORSE.items()}
QSO_FIELDS = ("call", "rst_sent", "rst_rcvd", "name", "qth")
ALLOWED = re.compile(r"<(SK|KN|AR|BT)>|[A-Z0-9/?.,= ]")
CALLSIGN = re.compile(r"^(?=.*\d)[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}[A-Z](/[A-Z0-9]{1,4})?$")

SYSTEM_PROMPT = """You read Morse code (CW) copy for amateur radio station {mycall} during a contact.
The copy comes from an automatic decoder and is imperfect: letters may be wrong or missing,
stray E/T/I/S characters are noise, words may be merged or split. Use the transcript and
ham radio conventions to work out what the other station most likely sent.

Return what the LATEST received over contains:
- for_us: true if it is addressed to {mycall} or answers our CQ / continues our contact.
  false for someone else's CQ, another contact, or pure noise.
- call: the other station's callsign if it appears (its call usually follows "DE").
  Repeated calls help you correct decoding errors. "" if not sent or unreadable.
- rst: the signal report they gave us (3 digits like 579; 5NN = 599, 4NN = 499, T = 0). "" if none.
- name, qth: their operator name / location if sent ("NAME HANS HANS" -> "HANS";
  "QTH BERLIN" -> "BERLIN"). "" if none. Never invent anything. Only report what is in
  latest_rx; our own name/QTH ({facts}) are never theirs.
- asks_repeat: true if they ask us to repeat (AGN, ?, PSE RPT, QRZ? after our call).
- sent_73: true if they are signing off (73, <SK>, TU, GL at the end of the contact).
- answer: if they asked us a direct question (e.g. RIG? ANT? WX? AGE?), a very short CW
  answer using only these facts: {facts}. Otherwise "".

JSON only."""

SCHEMA = {
    "type": "object",
    "properties": {
        "for_us": {"type": "boolean"}, "call": {"type": "string"}, "rst": {"type": "string"},
        "name": {"type": "string"}, "qth": {"type": "string"},
        "asks_repeat": {"type": "boolean"}, "sent_73": {"type": "boolean"}, "answer": {"type": "string"},
    },
    "required": ["for_us", "call", "rst", "name", "qth", "asks_repeat", "sent_73", "answer"],
}


def harmony_prompt(system, user):
    """gpt-oss chat template with the assistant turn pre-opened on the final channel.

    Skipping the analysis (reasoning) channel cuts reply time from ~20 s to a few
    seconds on CPU; the model's job is only extraction, the decisions are in code."""
    return ("<|start|>system<|message|>You are ChatGPT, a large language model trained by OpenAI.\n"
            "Reasoning: low\n\n# Valid channels: analysis, commentary, final. "
            "Channel must be included for every message.<|end|>"
            f"<|start|>developer<|message|># Instructions\n\n{system}<|end|>"
            f"<|start|>user<|message|>{user}<|end|>"
            "<|start|>assistant<|channel|>final<|message|>")


def morse_seconds(text, wpm):
    """Transmission time with PARIS timing (dot = 1.2 / wpm seconds)."""
    units = 0
    for word in text.split():
        for tok in re.findall(r"<[A-Z]{2}>|.", word):
            letters = tok.strip("<>") if tok.startswith("<") else tok
            code = "".join(MORSE_REV.get(c, "") for c in letters)
            units += sum(1 if e == "." else 3 for e in code) + len(code) - 1 + 3
        units += 4
    return units * 1.2 / max(wpm, 5)


def clean_text(text):
    text = (text or "").upper().replace("<BT>", "=")
    return re.sub(r"\s+", " ", "".join(m.group(0) for m in ALLOWED.finditer(text))).strip()


def meaningful(text):
    """True if the copy is more than decoder noise (stray E/T/I/S)."""
    for tok in text.split():
        t = tok.strip("<>")
        if any(c.isdigit() for c in t) or (len(t) >= 3 and not set(t) <= set("EITSH")):
            return True
        if t in ("K", "KN", "SK", "BK", "QRZ", "R", "TU", "73"):
            return True
    return False


def strip_noise_tail(text):
    """Drop trailing decoder noise (lone E/T/I/A...) after the real end of an over."""
    w = text.split()
    while w and len(w[-1]) <= 2 and set(w[-1]) <= set("ETIANMS"):
        w.pop()
    return " ".join(w)


def first_word_if_repeated(v):
    """'HANS HAN' -> 'HANS', 'BERLIN BERLTN' -> 'BERLIN'; keeps 'NEW YORK'."""
    w = v.split()
    return w[0] if len(w) >= 2 and w[0][:3] == w[1][:3] else v


def normalize_rst(v):
    v = clean_text(v).replace(" ", "").replace("N", "9").replace("T", "0")
    return v[:3] if re.fullmatch(r"[1-5][1-9][1-9]", v[:3]) else ""


def close_to(a, b):
    """Same length (+-1) and at most 2 differing characters: tolerates decoder errors."""
    if abs(len(a) - len(b)) > 1:
        return False
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b)) <= 2


def in_copy(value, over, fuzzy=False):
    """Guard against the model echoing data that was not actually received."""
    if not value:
        return False
    words = over.replace("N", "9").split() if value.isdigit() else over.split()
    if value in words or (" " in value and value in over):
        return True
    return fuzzy and any(close_to(value, w) for w in words if len(w) >= 3)


def heard_call(call, over):
    """True if `call` is in the copy, allowing decoder errors and merged words."""
    return call in over.replace(" ", "") or any(close_to(call, w) for w in over.split() if len(w) >= 4)


CQ_DIRECTED = re.compile(r"CQ ?(?:CQ ?)*(DX|EU|NA|SA|AF|OC|AS|JA)\b|\bPSE DX\b|\bDX (?:ONLY|K)\b")
CONTINENTS = {"EU", "NA", "SA", "AF", "OC", "AS"}


def cq_caller(over):
    """(call, directed) for a CQ in the copy, else (None, None). The call must be sent
    twice or follow DE, and must be a real prefix in cty.dat. The decoder often merges
    words ("CQVU2TMPVU2TMP"), so candidates are searched in the copy without spaces."""
    flat = over.replace(" ", "")
    if "CQ" not in flat:
        return None, None
    best = None
    for m in re.finditer(r"\d", flat):
        i = m.start()
        for a in range(max(0, i - 3), i):
            for b in range(i + 2, min(len(flat), i + 5) + 1):
                cand = flat[a:b]
                if not CALLSIGN.match(cand) or cand.startswith(("CQ", "DE")) or not geo.lookup(cand):
                    continue
                n, after_de = flat.count(cand), "DE" + cand in flat
                if n < 2 and not after_de:
                    continue
                score = n + after_de + over.split().count(cand)   # whole words beat merged guesses
                if best is None or (score, len(cand)) > best[:2]:
                    best = (score, len(cand), cand)
    if not best:
        return None, None
    d = CQ_DIRECTED.search(over)
    return best[2], (d.group(1) or "DX") if d else ""


def foreign_call(over, ours):
    """A callsign next to DE in the copy that is not one of `ours`, i.e. the over
    belongs to another contact on the frequency ("UA7N DE VU2TMP"). None if the
    copy names only our stations, or no stations at all."""
    w = over.split()
    for i, t in enumerate(w):
        if t != "DE":
            continue
        cands = w[i - 1:i] + w[i + 1:i + 2]
        if cands[-1:] and not CALLSIGN.match(cands[-1]):
            cands.append("".join(w[i + 1:i + 3]))                         # "VU 2TMP" -> VU2TMP
        for c in cands:
            if (CALLSIGN.match(c) and geo.lookup(c)
                    and not any(o and (c == o or close_to(c, o)) for o in ours)):
                return c
    return None


def utc():
    return dt.datetime.now(dt.timezone.utc)


class CWRobot:
    def __init__(self, app):
        self.app = app             # server.App: radio, cw decoder, spectrum, broadcast_json
        self.task = None
        self.cfg = {"mycall": "", "name": "", "qth": "", "grid": "", "rig": "IC7300", "wpm": 20,
                    "dry_run": True, "max_cq": 6, "llm_url": "http://127.0.0.1:8081", "afc": True,
                    "hunt": "all"}         # hunt: "all" stations or "dx" (other countries) only
        self.state = "idle"
        self.rx = ""               # text received in the current over
        self.rx_last = 0.0         # time of the last received character
        self.ignore_until = 0.0    # mute the decoder during our own transmission
        self.qso = None
        self.pre = []              # lines heard/sent before a QSO object exists
        self.anchor = None         # AFC anchor pitch
        self.last_tx = ""
        self.llm_secs = 0

    # ---------------------------------------------------------- public API
    @property
    def active(self):
        return self.task is not None and not self.task.done()

    def status(self):
        q = self.qso or {}
        return {"state": self.state, "active": self.active, "cfg": self.cfg, "in_qso": bool(q),
                "qso": {k: q.get(k, "") for k in QSO_FIELDS}, "locked": sorted(q.get("locked", ()))}

    def configure(self, cfg):
        for k in ("mycall", "name", "qth", "rig", "grid"):
            if k in cfg:
                self.cfg[k] = clean_text(cfg[k])
        if "llm_url" in cfg:
            self.cfg["llm_url"] = str(cfg["llm_url"])
        for k, hi in (("wpm", 48), ("max_cq", 30)):
            if k in cfg:
                self.cfg[k] = max(1, min(hi, int(cfg[k])))
        if "hunt" in cfg:
            self.cfg["hunt"] = "dx" if str(cfg["hunt"]).lower() == "dx" else "all"
        for k in ("dry_run", "afc"):
            if k in cfg:
                self.cfg[k] = bool(cfg[k])

    def edit_qso(self, field, value):
        """Operator correction of the other station's details. A value typed by the
        operator is locked: later decoder/LLM readings no longer change it."""
        if field not in QSO_FIELDS or not self.qso:
            return
        value = clean_text(value)
        if field == "call":
            value = value.replace(" ", "")
        self.qso[field] = value
        (self.qso["locked"].add if value else self.qso["locked"].discard)(field)
        self._event("note", f"operator set {field} = {value or '(cleared)'}")
        self._broadcast_status()

    def start(self, mode, target=None, other=None):
        if self.active:
            raise RuntimeError("robot already running")
        if self.app.beacons.active:
            raise RuntimeError("stop the beacon monitor first - both need the radio")
        if not re.fullmatch(r"[A-Z0-9/]{3,12}", self.cfg["mycall"] or ""):
            raise RuntimeError("set your callsign first")
        if not self.cfg["name"] or not self.cfg["qth"]:
            raise RuntimeError("set your name and QTH first")
        target = clean_text(target or "")
        if mode == "answer" and not target:
            raise RuntimeError("enter the call of the station to answer")
        if mode == "pathcheck" and not band_of(self.app.radio.state.get("freq", 0) / 1000):
            raise RuntimeError("path check: tune into a band's CW segment first")
        # what the operator already knows about the station being answered
        prefill = {k: clean_text(v) for k, v in (other or {}).items() if k in QSO_FIELDS and k != "call" and v}
        self.task = asyncio.ensure_future(self._run(mode, target, prefill if mode == "answer" else {}))
        self.task.add_done_callback(lambda _: self._broadcast_status())

    async def stop(self, reason="stopped by operator"):
        if self.active:
            self.task.cancel()
            try:
                await self.task
            except BaseException:
                pass
        await self._unkey()
        if self.qso:
            self._finish(reason)
        self._set_state("idle")

    def on_rx(self, text):
        """Decoded CW characters from the audio decoder."""
        if not self.active or time.time() < self.ignore_until:
            return
        self.rx += text
        if text.strip():
            self.rx_last = time.time()

    def inject(self, text):
        """Testing aid: pretend this text was decoded off the air."""
        self.ignore_until = 0
        self.on_rx(" " + clean_text(text) + " ")

    # ---------------------------------------------------------- main loop
    async def _run(self, mode, target, prefill=None):
        try:
            await self._setup_radio()
            self.qso = None
            if mode == "cq":
                await self._cq_loop()
            elif mode == "pathcheck":
                await self._path_check()
            elif mode == "hunt":
                await self._hunt()
            else:
                self._new_qso(target, "answering")
                self.qso.update(prefill or {})
                self.qso["locked"] |= {"call", *(prefill or {})}
                my = self.cfg["mycall"]
                await self._tx(f"{target} DE {my} {my} K")
                await self._qso_loop()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._event("error", str(e))
            await self._unkey()
            if self.qso:
                self._finish(f"error: {e}")
        finally:
            self._set_state("idle")

    async def _setup_radio(self):
        r = self.app.radio
        if not self.cfg["dry_run"]:
            if not r.state["connected"]:
                raise RuntimeError("radio not connected")
            if r.state["mode"] not in ("CW", "CW-R"):
                r.set_mode("CW", data=False)
            if not r.state.get("break_in"):
                r.set_break_in(1)
                self._event("note", "break-in set to SEMI")
            r.set_level("keyspeed", round((self.cfg["wpm"] - 6) * 255 / 42))
            await asyncio.sleep(0.5)
        asyncio.ensure_future(self._warm_llm())
        pitch = r.state.get("cw_pitch") or 600
        self.anchor = pitch
        self.app.cw.set_freq(pitch)
        self.app.enabled["cw"] = True
        self.app.broadcast_json(type="decoder", status=self.app.decoder_status())
        self._event("note", f"decoder on {pitch} Hz, keyer {self.cfg['wpm']} WPM"
                    + (" - DRY RUN, not transmitting" if self.cfg["dry_run"] else ""))

    async def _path_check(self, n_cq=3):
        """QRL?, then a few CQs while RBN / PSKReporter reports of our call are collected.
        If someone answers, the contact is worked as usual."""
        my = self.cfg["mycall"]
        self._set_state("checking frequency")
        await self._wait_clear(3.0)
        await self._tx("QRL?")
        if await self._wait_over(4) is not None:
            self._event("note", "frequency seems in use - path check aborted, move a little and retry")
            return
        pc = self.app.pathcheck
        pc.begin(my, self.cfg["grid"], self.app.radio.state.get("freq", 0) / 1000)
        if self.cfg["dry_run"]:
            pc.mark_dry_run()
        await self._cq_loop(n_cq)
        self._event("note", "path check: CQs done, collecting RBN / PSKReporter reports for a few minutes")

    async def _cq_loop(self, max_cq=None):
        my = self.cfg["mycall"]
        calls = 0
        while calls < (max_cq or self.cfg["max_cq"]):
            self._set_state("checking frequency")
            await self._wait_clear(3.0)
            calls += 1
            await self._tx(f"CQ CQ CQ DE {my} {my} K")
            deadline = time.time() + 12
            while time.time() < deadline:
                over = await self._wait_over(max(1.0, deadline - time.time()))
                if over is None:
                    break
                self._new_qso("", "cq")
                if await self._handle_over(over):
                    await self._qso_loop()
                    return
                self.qso = None      # not for us: keep listening after our CQ
        self._event("note", f"no answer after {calls} CQ calls")

    async def _hunt(self, minutes=30, calls_each=2):
        """Listen on the frequency, answer the first CQ we may answer, and work that one
        contact. Directed CQs that exclude us (CQ DX from our own country, CQ EU from
        Asia...) are skipped, and so is a station that did not come back to us."""
        my = self.cfg["mycall"]
        me = geo.lookup(my) or {}
        skipped = {}                        # call -> time we last decided not to call it
        end = time.time() + minutes * 60
        while time.time() < end:
            self._set_state("hunting: listening for CQ")
            over = await self._wait_over(60)
            if over is None:
                continue
            call, directed = cq_caller(over)
            if not call or call == my or time.time() - skipped.get(call, 0) < 300:
                continue
            them = geo.lookup(call) or {}
            if self.cfg["hunt"] == "dx" and them.get("country") == me.get("country"):
                why = f"{call} is in {me.get('country')} (hunting DX only)"
            elif directed == "DX" and them.get("country") == me.get("country"):
                why = f"{call} is calling CQ DX and is in {me.get('country')} too"
            elif directed in CONTINENTS and directed != me.get("cont"):
                why = f"{call} is calling CQ {directed} only"
            elif directed == "JA" and me.get("country") != "Japan":
                why = f"{call} is calling CQ JA only"
            else:
                why = ""
            if why:
                self._event("note", f"{why} - not answering, still listening")
                skipped[call] = time.time()
                continue
            self._event("note", f"heard {call} calling CQ ({them.get('country', '?')}) - answering")
            self._new_qso(call, "answering")
            for _ in range(calls_each):
                await self._tx(f"{call} DE {my} {my} K")
                reply = await self._wait_over(10)
                if reply and heard_call(my, reply):
                    await self._handle_over(reply)
                    await self._qso_loop()
                    return
                if reply and cq_caller(reply)[0] != call:
                    break                   # they are working someone else
            self._event("note", f"{call} did not come back to us - still listening")
            self.qso = None                 # not a contact: nothing to log
            skipped[call] = time.time()
        self._event("note", f"hunt: no answerable CQ in {minutes} min")

    async def _qso_loop(self):
        my = self.cfg["mycall"]
        retries = 0
        while self.qso:
            if time.time() - self.qso["start_ts"] > 15 * 60:
                self._event("note", "QSO time limit reached")
                self._finish("time limit")
                return
            over = await self._wait_ours(25)
            if over is None:
                if self.qso["sent_final"]:
                    self._finish("complete" if self._complete() else "ended")
                    return
                retries += 1
                if retries > 2:
                    self._finish("no reply")
                    return
                call = self.qso["call"]
                await self._tx(f"{call} DE {my} AGN? K" if call else f"QRZ? DE {my} K")
                continue
            retries = 0
            await self._handle_over(over)

    # ---------------------------------------------------------- QSO logic
    def _new_qso(self, call, phase):
        self.qso = {"call": call, "rst_sent": "", "rst_rcvd": "", "name": "", "qth": "", "locked": set(),
                    "phase": phase, "sent_report": False, "sent_final": False,
                    "start": utc(), "start_ts": time.time(),
                    "freq": self.app.radio.state.get("freq", 0), "transcript": list(self.pre)}
        self.pre = []

    def _complete(self):
        q = self.qso
        return bool(q["call"] and q["rst_sent"] and q["rst_rcvd"])

    async def _handle_over(self, over):
        """Read one received over and transmit the next message. Returns False if
        the over was not part of our contact."""
        q = self.qso
        my = self.cfg["mycall"]
        # Someone else's CQ while we are calling: never answer it.
        if q["phase"] == "cq" and re.match(r"(?:[ETIANMS]{1,2} )*(CQ|TEST)\b", over) and my not in over:
            self._event("note", "someone else's CQ - still listening")
            return False
        info = await self._read_over(over)
        if info is None:          # model unavailable: ask for a repeat rather than guess
            await self._tx(f"{q['call'] or 'QRZ?'} DE {my} PSE AGN K")
            return True
        if q["phase"] == "cq" and not (info["for_us"] or my in over):
            return False

        call = clean_text(info["call"]).replace(" ", "")
        if (CALLSIGN.match(call) and call != my and in_copy(call, over, fuzzy=True)
                and (not q["call"] or q["phase"] != "in qso") and "call" not in q["locked"]):
            q["call"] = call
        rst = normalize_rst(info["rst"])
        if rst and in_copy(rst, over) and "rst_rcvd" not in q["locked"]:
            q["rst_rcvd"] = rst
        for k in ("name", "qth"):
            v = first_word_if_repeated(clean_text(info[k]))
            if v and v != self.cfg[k] and in_copy(v, over, fuzzy=True) and k not in q["locked"]:
                q[k] = v
        self._broadcast_status()

        text = self._next_message(info)
        q["phase"] = "in qso"
        if text is None:
            self._finish("complete" if self._complete() else "ended")
            return True
        await self._tx(text)
        return True

    def _next_message(self, info):
        """Pick the next standard CW message. None means the contact is over."""
        q, c = self.qso, self.cfg
        my, call = c["mycall"], q["call"]
        if info["asks_repeat"] and self.last_tx and q["phase"] == "in qso":
            return self.last_tx
        if q["sent_final"] and (info["sent_73"] or q["rst_rcvd"]):
            return None
        if not call:
            return f"QRZ? DE {my} K" if q["phase"] == "cq" else f"PSE UR CALL? DE {my} K"
        extra = clean_text(info["answer"])[:60]
        extra = f" {extra}" if extra else ""
        rst = q["rst_sent"] or self._suggest_rst()
        op = q["name"] or "OM"
        if not q["sent_report"]:
            q["sent_report"], q["rst_sent"] = True, rst
            if q["rst_rcvd"]:     # they already gave us a report (we answered their CQ)
                q["sent_final"] = True
                return (f"{call} DE {my} R TNX {op} UR RST {rst} {rst} NAME {c['name']} {c['name']} "
                        f"QTH {c['qth']} {c['qth']}{extra} 73 GL {call} DE {my} <SK>")
            return (f"{call} DE {my} GM TNX FER CALL UR RST {rst} {rst} NAME {c['name']} {c['name']} "
                    f"QTH {c['qth']} {c['qth']}{extra} HW? {call} DE {my} <KN>")
        if not q["rst_rcvd"]:
            return f"{call} DE {my} PSE AGN UR RST?{extra} {call} DE {my} K"
        q["sent_final"] = True
        return f"{call} DE {my} R TNX FER RPT {op}{extra} 73 GL {call} DE {my} <SK>"

    async def _read_over(self, over):
        self._set_state("thinking")
        q = self.qso
        user = json.dumps({
            "we_are": self.cfg["mycall"],
            "contact_so_far": {k: q[k] for k in ("call", "rst_rcvd", "name", "qth")},
            "transcript": "\n".join(f"{who}: {txt}" for _, who, txt in q["transcript"][-10:]),
            "latest_rx": over,
        })
        for _ in range(2):
            try:
                info = await self._llm(user)
                self._event("llm", f"({self.llm_secs}s) " + json.dumps(info))
                return info
            except Exception as e:
                self._event("error", f"LLM: {e}")
        return None

    def _system_prompt(self):
        c = self.cfg
        facts = f"rig {c['rig']}, name {c['name']}, QTH {c['qth']}"
        return SYSTEM_PROMPT.format(mycall=c["mycall"], facts=facts)

    async def _llm(self, user, n_predict=200):
        t = time.time()
        body = {"prompt": harmony_prompt(self._system_prompt(), user), "n_predict": n_predict,
                "temperature": 0.1, "json_schema": SCHEMA, "cache_prompt": True}
        url = self.cfg["llm_url"].rstrip("/") + "/completion"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
            async with s.post(url, json=body) as r:
                r.raise_for_status()
                data = await r.json()
        self.llm_secs = round(time.time() - t, 1)
        return data if n_predict <= 1 else json.loads(data["content"])

    async def _warm_llm(self):
        """Process the fixed prompt once so the first real reply is quick."""
        try:
            await self._llm("{}", n_predict=1)
        except Exception as e:
            self._event("error", f"LLM not reachable at {self.cfg['llm_url']}: {e}")

    def _suggest_rst(self):
        s = self.app.radio.state.get("smeter", 0)
        return f"5{max(1, min(9, round(s / 120 * 9)))}9"

    # ---------------------------------------------------------- RX / TX timing
    async def _wait_clear(self, quiet):
        """Wait until nothing meaningful has been decoded for `quiet` seconds."""
        self.rx = ""
        t = time.time()
        while time.time() - t < quiet:
            await asyncio.sleep(0.5)
            if meaningful(clean_text(self.rx)):
                self.rx, t = "", time.time()

    async def _wait_over(self, timeout):
        """Return the other station's complete over, or None if nothing arrives."""
        self._set_state("listening")
        self.rx = ""
        t0 = time.time()
        core, core_t = "", t0
        while True:
            await asyncio.sleep(0.25)
            self._afc()
            now = time.time()
            txt = strip_noise_tail(clean_text(self.rx))
            if not meaningful(txt):
                if now - t0 > timeout:
                    return None
                continue
            if txt != core:                  # time of the last real progress
                core, core_t = txt, now
            ends = re.search(r"(\bK|\bKN|<KN>|<SK>|\bSK|\bBK|<AR>)$", txt)
            if now - core_t > (1.5 if ends else 3.5) or now - t0 > timeout + 60:
                self.rx = ""
                self._log_line("RX", txt)
                return txt

    async def _wait_ours(self, timeout):
        """Like _wait_over, but skips overs of another contact on the frequency."""
        my, deadline = self.cfg["mycall"], time.time() + timeout
        while time.time() < deadline:
            over = await self._wait_over(deadline - time.time())
            if over is None:
                return None
            other = None if heard_call(my, over) else foreign_call(over, (my, self.qso["call"]))
            if not other:
                return over
            self._event("note", f"over from another contact ({other}) - ignored, still waiting for "
                        f"{self.qso['call'] or 'our station'}")
        return None

    def _afc(self):
        """Keep the decoder on the station within +-150 Hz of the radio's CW pitch."""
        if not self.cfg["afc"] or self.anchor is None:
            return
        cw = self.app.cw
        if cw.level() < 8:
            return
        f = self.app.spectrum.peak_near(cw.freq, 40)
        if abs(f - self.anchor) <= 150 and abs(f - cw.freq) > 8:
            cw.retune(f)

    async def _tx(self, text):
        text = clean_text(text)[:150]
        my = self.cfg["mycall"]
        if not text:
            return
        if my not in text.split():
            text += f" DE {my}"              # always identify
        self.last_tx = text
        self._log_line("TX", text)
        if self.cfg["dry_run"]:
            self._set_state("dry run: not keyed")
            await asyncio.sleep(1.0)
            return
        self._set_state("transmitting")
        secs = morse_seconds(text, self.cfg["wpm"])
        self.ignore_until = time.time() + secs + 60
        r = self.app.radio
        r.send_cw(text)
        await asyncio.sleep(secs + 0.3)
        # wait for break-in to drop the transmitter (bounded)
        t = time.time()
        while time.time() - t < 6:
            r.poll_tx()
            await asyncio.sleep(0.3)
            if not r.state["tx"]:
                break
        else:
            await self._unkey()
            raise RuntimeError("transmitter did not release; unkeyed")
        self.ignore_until = time.time() + 0.6
        self.rx = ""

    async def _unkey(self):
        r = self.app.radio
        if self.cfg["dry_run"] and not r.state.get("tx"):
            return
        r.stop_cw()
        r.set_ptt(False)
        self.ignore_until = time.time() + 0.5

    # ---------------------------------------------------------- logging
    def _log_line(self, who, text):
        entry = (utc(), who, text)
        if self.qso is not None:
            self.qso["transcript"].append(entry)
        else:
            self.pre = (self.pre + [entry])[-4:]
        self._event(who.lower(), text)

    def _finish(self, status):
        q, self.qso = self.qso, None
        if not q or not (q["call"] or len(q["transcript"]) > 2):
            return
        os.makedirs(LOG_DIR, exist_ok=True)
        dry = self.cfg["dry_run"]
        end = utc()
        base = os.path.join(LOG_DIR, "cw_robot_dryrun" if dry else "cw_robot")
        with open(base + ".txt", "a") as f:
            f.write(f"===== {q['start']:%Y-%m-%d %H:%M}Z  {q['freq'] / 1000:.1f} kHz  CW"
                    f"{'  (DRY RUN)' if dry else ''} =====\n")
            f.write(f"Call: {q['call'] or '?'}   RST sent: {q['rst_sent'] or '-'}   "
                    f"RST rcvd: {q['rst_rcvd'] or '-'}\n")
            f.write(f"Name: {q['name'] or '-'}   QTH: {q['qth'] or '-'}\n")
            f.write(f"Time: {q['start']:%H:%M:%S}Z - {end:%H:%M:%S}Z   Status: {status}\n")
            f.write("Transcript:\n")
            for t, who, txt in q["transcript"]:
                f.write(f"  {t:%H:%M:%S} {who}: {txt}\n")
            f.write("\n")
        if q["call"] and q["rst_sent"] and q["rst_rcvd"]:
            self._adif(base + ".adi", q, end)
        self._event("log", f"logged {q['call'] or '?'} ({status}) to {os.path.relpath(base, HERE)}.txt")
        self._broadcast_status()

    def _adif(self, path, q, end):
        def fld(name, v):
            return f"<{name}:{len(v)}>{v} " if v else ""
        new = not os.path.exists(path)
        with open(path, "a") as f:
            if new:
                f.write("CW robot log <ADIF_VER:5>3.1.4 <PROGRAMID:10>IC7300-WEB <EOH>\n")
            f.write(fld("CALL", q["call"]) + fld("QSO_DATE", f"{q['start']:%Y%m%d}")
                    + fld("TIME_ON", f"{q['start']:%H%M%S}") + fld("TIME_OFF", f"{end:%H%M%S}")
                    + fld("FREQ", f"{q['freq'] / 1e6:.6f}") + fld("MODE", "CW")
                    + fld("RST_SENT", q["rst_sent"]) + fld("RST_RCVD", q["rst_rcvd"])
                    + fld("NAME", q["name"]) + fld("QTH", q["qth"])
                    + fld("STATION_CALLSIGN", self.cfg["mycall"]) + "<EOR>\n")

    # ---------------------------------------------------------- UI events
    def _set_state(self, s):
        if s != self.state:
            self.state = s
            self._broadcast_status()

    def _broadcast_status(self):
        self.app.broadcast_json(type="robot", status=self.status())

    def _event(self, kind, text):
        self.app.broadcast_json(type="robot_event", kind=kind, text=text,
                                t=utc().strftime("%H:%M:%S"))
