"""CW skimmer on synthetic I/Q: every station gets one channel and its call, side lobes
and key clicks don't, a neighbour 70 Hz away is kept apart, and a QSY keeps the RF."""
import numpy as np

import cwsynth
import kiwi
from decoders import FS
from skimmer import Skimmer

STATIONS = [(-3000, "CQ CQ DE DL3YEE DL3YEE K", 22, 0.3), (-1200, "CQ TEST OK1RR OK1RR TEST", 28, 0.15),
            (150, "CQ CQ DE VU2TMP VU2TMP K", 18, 0.1), (220, "CQ DE EA6NB EA6NB K", 25, 0.2),
            (2500, "CQ DE JA1ABC JA1ABC K", 20, 0.05)]


def band(seconds=45, seed=7):
    rng = np.random.default_rng(seed)
    n = FS * seconds
    iq = (rng.normal(0, 0.02, n) + 1j * rng.normal(0, 0.02, n)).astype(np.complex64)
    t = np.arange(n) / FS
    for off, text, wpm, amp in STATIONS:
        key = np.concatenate([np.full(int(s * FS), 1.0 if k else 0.0)
                              for k, s in cwsynth.keying((text + " ") * 6, wpm, rng=rng)])[:n]
        key = np.convolve(np.pad(key, (0, n - len(key))), np.ones(48) / 48, "same")
        iq += (amp * key * np.exp(2j * np.pi * (off + rng.uniform(-8, 8)) * t)).astype(np.complex64)
    return iq


def run(sk, iq, fc=7_025_000, t0=0.0):
    for k in range(0, len(iq), 1200):
        sk.feed(iq[k:k + 1200], fc, now=t0 + k / FS)


def test_one_channel_per_station_with_its_call():
    sk = Skimmer()
    run(sk, band())
    rows = sk.rows(now=45)
    assert sorted(r["call"] for r in rows) == ["DL3YEE", "EA6NB", "JA1ABC", "OK1RR", "VU2TMP"]
    for (off, _, wpm, _), r in zip(sorted(STATIONS), sorted(rows, key=lambda r: r["offset"])):
        assert abs(r["offset"] - off) <= 15 and abs(r["wpm"] - wpm) <= 3
        assert r["hz"] == 7_025_000 + r["offset"]


def test_noise_makes_no_channels():
    rng = np.random.default_rng(1)
    iq = (rng.normal(0, 0.05, FS * 30) + 1j * rng.normal(0, 0.05, FS * 30)).astype(np.complex64)
    sk = Skimmer()
    run(sk, iq)
    assert sk.rows(now=30) == []


def test_qsy_keeps_channels_on_their_rf():
    sk = Skimmer()
    run(sk, band(30))
    before = [7_025_000 + ch.offset for ch in sk.channels]
    sk.retune(7_028_000)                         # dial up 3 kHz: the station 3 kHz below falls out of the span
    after = [7_028_000 + ch.offset for ch in sk.channels]
    assert after == [hz for hz in before if abs(hz - 7_028_000) <= 4800]
    assert len(after) == len(before) - 1


def test_iq_demod_cw_and_cw_r():
    t = np.arange(FS * 2) / FS
    def pk(y):
        y = y[FS // 2:]                          # after the filter has settled
        return np.fft.rfftfreq(len(y), 1 / FS)[np.argmax(np.abs(np.fft.rfft(y)))]
    assert abs(pk(kiwi.IQDemod(600, 350, 850)(np.exp(2j * np.pi * 100 * t))) - 700) < 1     # 100 Hz above the dial
    assert abs(pk(kiwi.IQDemod(600, 350, 850, flip=True)(np.exp(-2j * np.pi * 100 * t))) - 700) < 1
    y = kiwi.IQDemod(600, 350, 850)(np.exp(2j * np.pi * 100 * t))[FS // 2:]
    image = kiwi.IQDemod(600, 350, 850)(np.exp(-2j * np.pi * 1300 * t))[FS // 2:]
    assert 20 * np.log10(y.std() / image.std()) > 60
