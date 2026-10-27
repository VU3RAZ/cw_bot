"""CW decoder sensitivity: weak signals must copy, noise must not print, fast senders
must still be followed. These guard cwtune.py from trading one for another."""
import difflib

import numpy as np

from cwsynth import audio, decode, keying, word_score
from decoders import FS, CWDecoder

TEXT = "CQ CQ DE DL3YEE DL3YEE K N0CALL DE UA7N GM TNX CALL UR RST 579 579 NAME IGOR IGOR QTH ROSTOV HW KN"


def char_ratio(a, b):
    sm = difflib.SequenceMatcher(None, a, " ".join(b.split()), autojunk=False)
    return sum(m.size for m in sm.get_matching_blocks()) / len(a)


def test_weak_signal_copies():
    rng = np.random.default_rng(11)
    for wpm in (16, 25):
        x = audio(keying(TEXT, wpm, 3.0, 6.0, 0.05, rng), 700, 6, rng)      # 6 dB SNR in 500 Hz
        assert char_ratio(TEXT, decode(CWDecoder(700), x)) >= 0.9


def test_no_junk_on_white_noise():
    rng = np.random.default_rng(2)
    x = audio([(False, 120)], 700, 6, rng)
    assert len(decode(CWDecoder(700), x).replace(" ", "")) <= 4             # 2 minutes


def test_little_junk_on_impulsive_noise():
    """Band noise with QRN crashes (5-30 ms bursts) must not decode as a stream of dits."""
    rng = np.random.default_rng(3)
    x = audio([(False, 120)], 700, 6, rng)
    for t in rng.uniform(0, 119, 1200):                                     # 10 crashes a second
        i, n = int(t * FS), int(rng.uniform(0.005, 0.03) * FS)
        x[i:i + n] += rng.normal(0, 0.5, n).astype(np.float32)
    out = decode(CWDecoder(700), x)
    assert len(out.replace(" ", "")) / 2 <= 20     # chars/min; ~4 now, 47 without the glitch limits


def test_fast_senders():
    t = "CQ CQ DE DL3YEE DL3YEE K N0CALL DE UA7N 5NN TU"
    for wpm in (35, 44):
        rng = np.random.default_rng(5)
        ok, total = word_score(t, decode(CWDecoder(700), audio(keying(t, wpm, 3, 7, 0.03, rng), 700, 20, rng)))
        assert ok >= 6
