"""CW decoder word spacing: words must split for senders with short (4-5 dot) word
spaces as well as standard 7-dot ones, without splitting characters."""
import numpy as np
import pytest

from decoders import CWDecoder
from cwsynth import audio, decode, keying, word_score

TEXT = ("CQ CQ DE DL3YEE DL3YEE K N0CALL DE UA7N GM TNX CALL UR RST 579 579 NAME IGOR IGOR "
        "QTH ROSTOV ROSTOV HW KN")


@pytest.mark.parametrize("word_gap,min_ok", [(7.0, 0.95), (5.5, 0.9), (4.5, 0.85), (4.0, 0.6)])
@pytest.mark.parametrize("wpm", [18, 25])
def test_word_spacing(word_gap, min_ok, wpm):
    rng = np.random.default_rng(wpm)
    x = audio(keying(TEXT, wpm, 3.0, word_gap, 0.03, rng), 700, 30, rng)
    ok, total = word_score(TEXT, decode(CWDecoder(700), x))
    assert ok / total >= min_ok


def test_learnt_threshold_follows_sender():
    rng = np.random.default_rng(1)
    short = CWDecoder(700)
    decode(short, audio(keying(TEXT, 22, 3.0, 4.5, 0.0, rng), 700, 30, rng))
    standard = CWDecoder(700)
    decode(standard, audio(keying(TEXT, 22, 3.0, 7.0, 0.0, rng), 700, 30, rng))
    assert 3.4 <= short.word_gap < 4.2 < standard.word_gap <= 6.0
