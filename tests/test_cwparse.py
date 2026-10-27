"""Rule-based over reader on hand-written overs (the LLM benchmark set)."""
import pytest

import cwparse

E = lambda **k: {"for_us": True, "call": "", "rst": "", "name": "", "qth": "",
                 "asks_repeat": False, "sent_73": False, **k}
CASES = [  # (partner so far, latest over, expected)
    ("", "N0CALL DE DL1ABC DL1ABC K", E(call="DL1ABC")),
    ("", "N0CALL DE DL1ABC GM TNX CALL UR RST 579 579 NAME HAMS HAMS QTH BERLIN BERLIN HW? N0CALL DE DL1ABC KN",
     E(call="DL1ABC", rst="579", name="HAMS", qth="BERLIN")),
    ("", "EN0CALL DE UA7NUA7N TNX UR 5NN 5NN NAME IGORIGOR QTH ROSTOV ROSTOV BK",
     E(call="UA7N", rst="599", name="IGOR", qth="ROSTOV")),
    ("JA1XYZ", "N0CALL DE JA1XYZ R TNX UR RST 449 449 OP TARO TARO HW? KN", E(call="JA1XYZ", rst="449", name="TARO")),
    ("", "CQ CQ DE W1AW W1AW K", E(for_us=False, call="W1AW")),
    ("", "CQ CQDEDL3YEE DL3YEE K", E(for_us=False, call="DL3YEE")),
    ("", "RRR UA7N DE VU 2TMP TU IGOR 73 SK", E(for_us=False, call="VU2TMP", sent_73=True)),
    ("DL1ABC", "R N0CALL DE DL1ABC TNX FER RPT JOE 73 GL N0CALL DE DL1ABC SK", E(call="DL1ABC", sent_73=True)),
    ("OK1RR", "N0CALL DE OK1RR PSE AGN UR NAME? NAME? K", E(call="OK1RR", asks_repeat=True)),
    ("G4ABC", "N0CALL DE G4ABC FB JOE RIG? ANT? HR IC7610 YAGI K", E(call="G4ABC")),
    ("", "E T EE I T", E(for_us=False)),
]


@pytest.mark.parametrize("partner,over,want", CASES)
def test_fields(partner, over, want):
    got = cwparse.parse(over, "N0CALL", partner, "JOE", "TESTVILLE")
    assert {k: got[k] for k in want} == want


def test_question_needs_llm():
    assert cwparse.parse("N0CALL DE G4ABC FB JOE RIG? ANT? K", "N0CALL", "G4ABC")["confidence"] == "low"
    assert cwparse.parse("N0CALL DE G4ABC UR 599 599 K", "N0CALL", "G4ABC")["confidence"] == "high"
