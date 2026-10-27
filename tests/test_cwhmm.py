"""HMM CW decoder: copies clean and weak signals, and its trace-back stays bounded across
long silences (an unbounded trace-back once filled 62 GB)."""
import difflib

import numpy as np

from cwhmm import CWHmm
from cwsynth import audio, decode, keying
from decoders import FS

TEXT = "CQ CQ DE DL3YEE K UR RST 599 NAME IGOR QTH ROSTOV 73"


def copy(x):
    d = CWHmm(700)
    return d, " ".join(decode(d, np.concatenate([x, np.zeros(3 * FS, x.dtype)])).split())   # ~2 s output lag


def char_ratio(a, b):
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(m.size for m in sm.get_matching_blocks()) / len(a)


def test_copies():
    rng = np.random.default_rng(1)
    for wpm, snr in ((20, 30), (20, 6), (30, 6)):
        x = audio(keying(TEXT, wpm, 3.0, 7.0, 0.05, rng), 700, snr, rng)
        assert char_ratio(TEXT, copy(x)[1]) >= 0.9, (wpm, snr)


def test_trace_bounded_over_long_silence():
    rng = np.random.default_rng(3)
    seg = keying("CQ DE DL3YEE K", 20, 3.0, 7.0, 0.05, rng)
    x = audio(seg + [(False, 20.0)] + seg + [(False, 20.0)] + seg, 700, 20, rng)
    longest = []
    trace = CWHmm._trace

    def watched(self):
        els = trace(self)
        longest.append(len(els))
        return els

    CWHmm._trace = watched
    try:
        d, out = copy(x)
    finally:
        CWHmm._trace = trace
    assert max(longest) <= len(d.bp)
    assert out.count("DL3YEE") == 3


def test_squelch_closes_on_noise():
    rng = np.random.default_rng(2)
    assert len(copy(audio([(False, 120)], 700, 6, rng))[1].replace(" ", "")) <= 4     # 2 minutes, default SQL
    x = audio([(False, 120)], 700, 6, rng)
    d = CWHmm(700, squelch=3.0)
    assert decode(d, x).strip() == ""
