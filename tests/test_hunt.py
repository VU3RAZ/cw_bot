"""Hunt with the skimmer: the skimmer lists who is calling CQ, the robot answers the
strongest one it may answer (not a CQ EU from Asia), tuning the radio onto it, and moves
on to the next when that one doesn't come back."""
import asyncio
import time

import robot
from cwparse import OverReader
from skimmer import Skimmer
from test_skimmer import STATIONS, band, run


class Radio:
    def __init__(self):
        self.state = {"smeter": 100, "mode": "CW", "cw_pitch": 600, "skim": True, "freq": 14_030_000}
        self.tuned = []

    def set_freq(self, hz):
        self.tuned.append(hz)
        self.state["freq"] = hz


class App:
    def __init__(self, cqs):
        self.radio, self.overs, self.events = Radio(), OverReader(), []
        self.skim_cq, self.skim_cq_t = cqs, time.time()
        self.pitch = None

    def follow_pitch(self, p):
        self.pitch = p

    def broadcast_json(self, **m):
        if m.get("type") == "robot_event":
            self.events.append((m["kind"], m["text"]))


CQS = [{"hz": 14_024_020, "call": "DL1ABC", "snr": 40, "wpm": 24, "age": 2, "done": True, "text": "CQ EU DE DL1ABC DL1ABC K"},
       {"hz": 14_027_000, "call": "IS5HHH", "snr": 30, "wpm": 21, "age": 2, "done": True, "text": "CQ CQ DE IS5HHH IS5HHH K"},
       {"hz": 14_033_100, "call": "R3ZBI", "snr": 22, "wpm": 22, "age": 3, "done": True, "text": "CQ DE R3ZBI R3ZBI K"}]


def make(replies):
    app = App(CQS)
    r = robot.CWRobot(app)
    r.cfg.update(mycall="VU2XYZ", name="JOE", qth="DELHI")
    r.sent, r.worked = [], []

    async def tx(text):
        r.sent.append(text)

    async def wait_over(timeout):
        return replies.pop(0) if replies else None

    async def qso_loop():
        r.worked.append(r.qso["call"])

    async def handle_over(over):
        pass

    r._tx, r._wait_over, r._qso_loop, r._handle_over = tx, wait_over, qso_loop, handle_over
    return r, app


def test_skimmer_lists_the_cq_callers():
    sk = Skimmer()
    run(sk, band())
    cqs = sk.cqs(now=45)
    assert {c["call"] for c in cqs} == {"DL3YEE", "OK1RR", "VU2TMP", "EA6NB", "JA1ABC"}
    for c in cqs:
        assert abs(c["hz"] - 7_025_000 - next(o for o, t, _, _ in STATIONS if c["call"] in t)) <= 15


def test_answers_the_strongest_it_may_and_tunes_onto_it():
    r, app = make(["VU2XYZ DE IS5HHH GM UR 599 K"])
    assert asyncio.run(r._hunt_skimmer({}))
    assert [c["call"] for c in r.cands] == ["IS5HHH", "R3ZBI", "DL1ABC"]   # answerable by strength, then the rest
    assert "CQ EU" in r.cands[-1]["why"]
    assert app.radio.tuned == [14_027_000] and app.pitch == 600
    assert r.sent == ["IS5HHH DE VU2XYZ VU2XYZ K"] and r.worked == ["IS5HHH"]


def test_no_answer_moves_on_to_the_next():
    r, app = make([])
    skipped = {}
    assert not asyncio.run(r._hunt_skimmer(skipped))
    assert asyncio.run(r._hunt_skimmer(skipped)) is False
    assert app.radio.tuned == [14_027_000, 14_033_100]
    assert set(skipped) == {"IS5HHH", "R3ZBI"}
    assert not asyncio.run(r._hunt_skimmer(skipped))      # nobody left to answer: listens on
    assert app.radio.tuned == [14_027_000, 14_033_100]
    why = {c["call"]: c["why"] for c in r.cands}
    assert why["IS5HHH"] == why["R3ZBI"] == "tried, no answer" and "CQ EU" in why["DL1ABC"]


def test_a_cq_that_turned_into_a_contact_is_no_longer_offered():
    from skimmer import Channel
    sk, ch = Skimmer(), Channel(500, 0)
    sk.channels, ch.seen = [ch], 10
    sk._over(ch, {"id": 1, "done": True, "text": "CQ CQ DE W0EAS W0EAS K", "call": "W0EAS", "calls": ["W0EAS"] * 2, "cq": True}, 10)
    assert [c["call"] for c in sk.cqs(now=11)] == ["W0EAS"]
    sk._over(ch, {"id": 2, "done": True, "text": "SP5GB DE W0EAS GE UR 599 K", "call": "W0EAS", "calls": ["W0EAS"], "cq": False}, 11)
    assert sk.cqs(now=12) == []


def test_still_calling_answered_when_the_skimmer_sees_the_cq_end():
    r, app = make([])
    app.skim_cq = [dict(CQS[1], done=False, age=0.5)]
    async def wait_over(timeout):          # the main decoder joins at the tail of the CQ
        app.skim_cq = [dict(CQS[1], done=True, age=1)]
        return "HHH PSE K" if not r.sent else None
    r._wait_over = wait_over
    asyncio.run(r._hunt_skimmer({}))
    assert r.sent[0] == "IS5HHH DE VU2XYZ VU2XYZ K"
