"""KiwiSDR ranking, nearby-spotter spot selection, tone finding and copy scoring."""
import time

import numpy as np

import cwsynth
import cwtune
import kiwi
from corpus import keyed_peak, readable
from spots import Spots

STATUS = {"status": "active", "offline": "no", "users": "2", "users_max": "8", "snr": "30,27",
          "gps": "(53.45, 10.22)", "bands": "0-30000000", "sdr_hw": "KiwiSDR 1 v1.902", "loc": "Hamburg"}


def test_summarize_and_rank():
    r = kiwi.summarize("http://rx.example:8073", STATUS)
    assert (r["host"], r["port"], r["free"], r["snr"], r["time_limited"]) == ("rx.example", 8073, 6, 27, False)
    assert kiwi.summarize("http://x:8073", dict(STATUS, offline="yes")) is None
    limited = dict(r, url="b", time_limited=True, snr=40)
    full = dict(r, url="c", free=1, snr=45)
    assert [x["url"] for x in kiwi.rank([full, limited, r])] == ["http://rx.example:8073", "b", "c"]


def test_heard_near_prefers_loud_nearby_spotters():
    s, now = Spots(), time.time()
    s.rbn.spots = [
        {"call": "LY2PX", "khz": 14026.0, "ts": now, "spotter": "VK4CT", "snr": 40, "wpm": 26},   # far away
        {"call": "DK4AC", "khz": 7029.0, "ts": now, "spotter": "DL8LAS", "snr": 30, "wpm": 12, "info": "CQ",
                    "source": "rbn"},
        {"call": "OK1RR", "khz": 7021.0, "ts": now, "spotter": "DL8LAS", "snr": 18, "wpm": 22},
        {"call": "OK1RR", "khz": 7021.1, "ts": now - 5000, "spotter": "DL8LAS", "snr": 35, "wpm": 22},  # stale
    ]
    near = s.heard_near((51.0, 10.0), 800)
    assert [e["call"] for e in near] == ["DK4AC", "OK1RR"]
    assert near[1]["snr"] == 18


def test_keyed_peak_finds_tone_not_noise():
    rng = np.random.default_rng(0)
    x = cwsynth.audio(cwsynth.keying("CQ CQ DE DL3YEE K", 22), 1850, 15, rng)
    hz, c = keyed_peak(x)
    assert abs(hz - 1850) <= 25 and c > 25
    assert keyed_peak(rng.normal(0, 0.1, 12000 * 6).astype(np.float32))[1] < 16


def test_scores_favour_clean_split_copy():
    assert cwtune.readability("CQ CQ DE DL3YEE DL3YEE K") > cwtune.readability("CQ CQDEDL3YEE DL3YEE K")
    assert cwtune.readability("E T E I * T") < 0
    assert readable("CQ DE DL3YEE DL3YEE UR 599 K") and not readable("E K T E DE")


def test_mode_of_comment_then_band_plan():
    from spots import mode_of
    assert mode_of(14074.9, "") == "FT8" and mode_of(7048.0, "") == "FT4"
    assert mode_of(14025.0, "") == "CW" and mode_of(14250.0, "") == "SSB"
    assert mode_of(7045.0, "") == "DIGI" and mode_of(3790.0, "tnx qso") == "SSB"
    assert mode_of(14200.0, "CW up 2") == "CW" and mode_of(14083.0, "RTTY test") == "RTTY"
    assert mode_of(28074.0, "FT8 +1 dB 1230 Hz CQ") == "FT8" and mode_of(14070.0, "PSK31") == "DIGI"


def test_active_keeps_all_modes_and_filters():
    import asyncio
    s, now = Spots(), time.time()
    s.dxs_time = now                       # no web fetch
    s.dxs = [{"call": "EA3AKP", "khz": 7160.0, "ts": now, "spotter": "AB3VK", "snr": None, "wpm": None,
              "info": "", "source": "dxsummit", "mode": "SSB"},
             {"call": "9K2YM", "khz": 28074.0, "ts": now, "spotter": "UA3ARC", "snr": None, "wpm": None,
              "info": "FT8", "source": "hamqth", "mode": "FT8"}]
    s.rbn.spots = [{"call": "DK4AC", "khz": 7029.0, "ts": now, "spotter": "DL8LAS", "snr": 30, "wpm": 12, "info": "CQ",
                    "source": "rbn"},
                   {"call": "DK4AC", "khz": 14083.0, "ts": now, "spotter": "DL8LAS", "snr": 20, "wpm": None,
                    "mode": "RTTY", "source": "rbn", "info": "CQ"}]
    every = asyncio.run(s.active())
    assert sorted((e["call"], e["mode"]) for e in every) == [
        ("9K2YM", "FT8"), ("DK4AC", "CW"), ("DK4AC", "RTTY"), ("EA3AKP", "SSB")]
    assert [e["call"] for e in asyncio.run(s.active(mode="CW"))] == ["DK4AC"]
    assert [e["band"] for e in asyncio.run(s.active("40"))].count("40") == 2
