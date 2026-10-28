"""CW decoder AFC (PitchTracker): finds the keyed tone, ignores noise, keeps its station."""
import numpy as np

import cwsynth
from decoders import FS, CWDecoder, PitchTracker

TEXT = "CQ CQ DE DL3YEE DL3YEE K " * 4


def run(x, dec, lo, hi):
    tr, moves = PitchTracker(), []
    for k in range(0, len(x), 1200):
        hz = tr.feed(x[k:k + 1200], dec, lo, hi)
        if hz is not None:
            dec.retune(hz) if abs(hz - dec.freq) <= 50 else dec.set_freq(hz)
            moves.append((k / FS, hz))
    return moves


def test_locks_onto_an_off_pitch_station():
    x = cwsynth.audio(cwsynth.keying(TEXT, 22), 860, 12, np.random.default_rng(2))
    dec = CWDecoder(700)
    moves = run(x, dec, 450, 950)
    assert moves and moves[0][0] <= 2 and abs(dec.freq - 860) <= 3
    dec2 = CWDecoder(700)
    tr, txt = PitchTracker(), ""
    for k in range(0, len(x), 1200):
        hz = tr.feed(x[k:k + 1200], dec2, 450, 950)
        if hz is not None:
            dec2.retune(hz) if abs(hz - dec2.freq) <= 50 else dec2.set_freq(hz)
        txt += dec2.feed(x[k:k + 1200])
    assert txt.count("DL3YEE") >= 4


def test_noise_does_not_move_it():
    x = np.random.default_rng(3).normal(0, 0.1, FS * 30).astype(np.float32)
    dec = CWDecoder(700)
    assert run(x, dec, 450, 950) == [] and dec.freq == 700


def test_stays_on_its_station_next_to_a_louder_one():
    rng = np.random.default_rng(4)
    ours = cwsynth.audio(cwsynth.keying(TEXT, 20), 700, 15, rng)
    loud = cwsynth.audio(cwsynth.keying("TEST DE OK1RR OK1RR " * 6, 28), 850, 18, rng)
    n = min(len(ours), len(loud))
    dec = CWDecoder(700)
    run(ours[:n] + loud[:n], dec, 450, 950)
    assert abs(dec.freq - 700) <= 15


def test_follows_a_drifting_tone_to_a_few_hertz():
    rng = np.random.default_rng(5)
    segs = cwsynth.keying(TEXT, 22)
    key = np.concatenate([np.full(int(s * FS), 1.0 if k else 0.0) for k, s in segs])
    key = np.convolve(key, np.ones(48) / 48, mode="same")
    t = np.arange(len(key)) / FS
    pitch = 700 + 8 * t                          # 8 Hz/s: far faster than any real drift
    x = 0.3 * key * np.sin(2 * np.pi * np.cumsum(pitch) / FS) + rng.normal(0, 0.02, len(t))
    dec, tr, err, txt = CWDecoder(700), PitchTracker(), [], ""
    for k in range(0, len(x), 1200):
        b = x[k:k + 1200].astype(np.float32)
        hz = tr.feed(b, dec, 300, 1500)
        if hz is not None:
            dec.retune(hz) if abs(hz - dec.freq) <= 50 else dec.set_freq(hz)
        txt += dec.feed(b)
        if k / FS > 3:
            err.append(dec.freq - pitch[min(k + 1199, len(pitch) - 1)])
    assert np.percentile(np.abs(err), 90) < 8
    assert txt.count("DL3YEE") >= 6
