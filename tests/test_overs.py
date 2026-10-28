"""CW tile Parsed view: the live copy split into overs and read by cwparse."""
from cwparse import OverReader


def feed_all(chunks, mycall="VU3RAZ"):
    r, out = OverReader(), []
    for c in chunks:
        out += r.feed(c, 0, mycall)
    return r, out


def test_splits_on_prosigns_and_reads_fields():
    _, out = feed_all(["CQ CQ DE DL", "3YEE DL3YEE K ",
                       "E T VU3RAZ DE DL3YEE GM UR 5NN 5NN NAME HANS HANS QTH BERLIN BERLIN BK "])
    done = [o for o in out if o["done"]]
    assert [o["id"] for o in done] == [0, 1]
    assert done[0]["cq"] and done[0]["call"] == "DL3YEE" and done[0]["text"] == "CQ CQ DE DL3YEE DL3YEE K"
    o = done[1]
    assert (o["call"], o["to"], o["rst"], o["name"], o["qth"]) == ("DL3YEE", "VU3RAZ", "599", "HANS", "BERLIN")
    assert o["text"].startswith("VU3RAZ DE")              # the stray E T before it is dropped
    assert set(o["calls"]) == {"VU3RAZ", "DL3YEE"}


def test_live_over_updates_then_closes_on_silence():
    r, out = feed_all(["CQDEDL3YEE DL3Y"])
    assert out[-1] == dict(out[-1], id=0, done=False) and "DE DL3YEE" in out[-1]["text"]
    assert r.tick(3, "") == []                             # still within QUIET
    (o,) = r.tick(10, "")
    assert o["done"] and o["id"] == 0 and r.feed("TU ", 11)[-1]["id"] == 1


def test_noise_is_not_an_over():
    r, out = feed_all(["E T I E T "])
    assert out == []
    (o,) = r.tick(10, "")
    assert o["text"] == ""                                 # tells the page to drop any live line


def test_calls_are_repaired_by_voting_across_copies():
    from cwparse import correct_calls, tokens
    fix = lambda t, known=None: correct_calls(tokens(t), known)[1]
    assert fix("CQ CQ DE DL3YFE DL3TEE DL3YEE K") == {"DL3YFE": "DL3YEE", "DL3TEE": "DL3YEE"}   # majority per letter
    assert fix("CQ DE DLEYEE DL3YEE K") == {"DLEYEE": "DL3YEE"}            # a copy that can't be a call
    assert fix("VU3RAT DE DL3YEE DL3YEE", {"VU3RAZ": 2}) == {"VU3RAT": "VU3RAZ"}   # our own call
    assert fix("CQ DE DL3YFE DL3YEE K") == {}                              # 1 against 1: seen, not fixable
    assert fix("CQ DE DL3YFE DL3YEE K", {"DL3YEE": 1}) == {"DL3YFE": "DL3YEE"}     # ...unless spotted here
    assert fix("OK1RR DE OK1AB OK1AB K") == {}                             # two real stations stay apart
    assert fix("CQ DE OY1C OY1C OY1CT") == {}                              # dropped or extra last letter: left
    assert fix("CQ DE EA6NB EA6NBK") == {"EA6NBK": "EA6NB"}                # K run into the call
    assert fix("TTEE DE TT8EE TT8EE") == {}                                # noise letters aren't a broken call


def test_reader_remembers_calls_from_earlier_overs():
    r = OverReader()
    r.feed("CQ DE OY1R OY1R K ", 0)
    o = r.feed("UA3T DE OE1R K ", 5)[0]
    assert o["fixed"] == {"OE1R": "OY1R"} and "UA3T DE OY1R" in o["text"]
