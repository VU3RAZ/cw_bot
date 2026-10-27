"""Robot over reading: the rule-based reader answers usual overs without the LLM, the LLM
is asked only when the reader is unsure, and the reader's result stands in if the LLM fails."""
import asyncio

import robot


class Radio:
    state = {"smeter": 100, "mode": "CW", "cw_pitch": 700}


class App:
    radio = Radio()

    def __init__(self):
        self.events = []

    def broadcast_json(self, **m):
        if m.get("type") == "robot_event":
            self.events.append((m["kind"], m["text"]))


def make(llm=None):
    app = App()
    r = robot.CWRobot(app)
    r.cfg.update(mycall="N0CALL", name="JOE", qth="TESTVILLE")
    r.sent, r.llm_calls = [], []

    async def tx(text):
        r.sent.append(text)

    async def fake_llm(user, n_predict=200):
        r.llm_calls.append(user)
        if llm is None:
            raise ConnectionError("LLM down")
        return llm

    r._tx, r._llm = tx, fake_llm
    r._new_qso("", "cq")
    return r, app


def run(r, over):
    return asyncio.run(r._handle_over(over))


def test_usual_over_skips_llm():
    r, app = make()
    assert run(r, "N0CALL DE DL1ABC DL1ABC K")
    assert r.llm_calls == []
    assert r.qso["call"] == "DL1ABC"
    assert r.sent and r.sent[-1].startswith("DL1ABC DE N0CALL")
    assert any(k == "parse" for k, _ in app.events)


def test_report_over_fills_fields_without_llm():
    r, _ = make()
    r.qso.update(call="UA7N", phase="in qso", rst_sent="599", sent_report=True)
    run(r, "EN0CALL DE UA7NUA7N TNX UR 5NN 5NN NAME IGORIGOR QTH ROSTOV ROSTOV BK")
    assert r.llm_calls == []
    assert (r.qso["rst_rcvd"], r.qso["name"], r.qso["qth"]) == ("599", "IGOR", "ROSTOV")


def test_question_asks_llm():
    answer = {"for_us": True, "call": "G4ABC", "rst": "", "name": "", "qth": "", "asks_repeat": False,
              "sent_73": False, "answer": "RIG IC7300"}
    r, _ = make(llm=answer)
    r.qso.update(call="G4ABC", phase="in qso")
    run(r, "N0CALL DE G4ABC FB JOE RIG? ANT? K")
    assert len(r.llm_calls) == 1


def test_llm_down_uses_rule_based_reading():
    r, app = make(llm=None)
    r.qso.update(call="G4ABC", phase="in qso")
    run(r, "N0CALL DE G4ABC UR RST 579 579 NAME BOB BOB RIG? K")
    assert r.llm_calls                              # it was asked (twice), and failed
    assert r.qso["rst_rcvd"] == "579" and r.qso["name"] == "BOB"
    assert not any("PSE AGN" in t for t in r.sent)
