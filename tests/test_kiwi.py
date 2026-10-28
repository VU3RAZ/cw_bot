"""KiwiSDR signal path, offline: I/Q demodulation, resampling, the receive-only radio's
dial/mode mapping, S-meter scale and waterfall-as-panadapter scaling."""
import numpy as np

import kiwi
from kiwisdr import demod, scope_px, smeter_icom, zoom_for

FS = kiwi.FS
t = np.arange(FS * 2) / FS


def test_iq_to_usb_keeps_upper_sideband_only():
    up = kiwi.IQToUSB()(np.exp(2j * np.pi * 1000 * t))[FS // 2:]
    image = kiwi.IQToUSB()(np.exp(-2j * np.pi * 1000 * t))[FS // 2:]
    assert abs(up.std() - 0.707) < 0.01
    assert 20 * np.log10(up.std() / image.std()) > 60


def test_iq_to_usb_blockwise_matches_one_shot():
    x = np.exp(2j * np.pi * 1500 * t) + 0.1 * np.random.default_rng(1).standard_normal(len(t))
    blocks = kiwi.IQToUSB()
    assert np.allclose(kiwi.IQToUSB()(x), np.concatenate([blocks(c) for c in np.array_split(x, 37)]))


def test_iq_samples_interleaved():
    data = np.array([16384, -16384, 0, 8192], "<i2").tobytes()
    assert np.allclose(kiwi.iq_samples(data), [0.5 - 0.5j, 0.25j])


def test_resampler_keeps_rate_and_pitch():
    fin = 11998.885                     # a real Kiwi's reported rate
    r = kiwi.Resampler(fin)
    x = np.sin(2 * np.pi * 700 * np.arange(512 * 235) / fin)
    y = np.concatenate([r(c) for c in np.split(x, 235)])
    assert abs(len(y) - len(x) * FS / fin) <= 1
    f = np.fft.rfftfreq(len(y), 1 / FS)[np.argmax(np.abs(np.fft.rfft(y * np.hanning(len(y)))))]
    assert abs(f - 700) < 0.5
    r = kiwi.Resampler(20250)           # wideband Kiwis
    assert len(np.concatenate([r(c) for c in np.split(np.ones(40500), 100)])) == 24000
    r = kiwi.Resampler(fin)
    assert np.iscomplexobj(r(np.ones(512, np.complex64)))


def test_cw_dial_is_the_signal_frequency():
    # CW: a signal on the dial is heard at the pitch, inside the passband
    mode, lo, hi, khz, w = demod("CW", 2, 7_030_000, 600)
    assert mode == "usb" and w == 500
    heard = 7_030_000 - khz * 1000
    assert heard == 600 and lo < heard < hi
    mode, lo, hi, khz, w = demod("CW-R", 2, 7_030_000, 600)
    assert mode == "lsb" and khz * 1000 - 7_030_000 == 600 and lo < -600 < hi


def test_rtty_mark_on_dial():
    mode, lo, hi, khz, _ = demod("RTTY", 1, 14_080_000)
    assert mode == "lsb" and khz * 1000 - 14_080_000 == 2125     # mark (dial) at 2125 Hz
    assert lo < -2295 and -2125 < hi                              # mark and space in the passband
    mode, lo, hi, khz, _ = demod("RTTY-R", 1, 14_080_000)
    assert mode == "usb" and 14_080_000 - khz * 1000 == 2295
    assert lo < 2125 and 2295 < hi


def test_ssb_passbands():
    assert demod("USB", 1, 14_200_000)[:4] == ("usb", 200, 3200, 14200.0)
    assert demod("LSB", 3, 7_150_000)[:4] == ("lsb", -2000, -200, 7150.0)


def test_smeter_scale():
    assert smeter_icom(-73) == 120          # S9
    assert smeter_icom(-127) == 0           # S0
    assert smeter_icom(-13) == 241          # S9+60
    assert smeter_icom(-200) == 0 and smeter_icom(0) == 241


def test_waterfall_zoom_covers_the_span():
    for half in (2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000):
        z = zoom_for(half)
        assert 30e6 / 2 ** z >= 2 * half > 30e6 / 2 ** (z + 1) or z == 0
    assert zoom_for(25000) == 9                  # +-25 kHz -> 58.6 kHz wide
    assert zoom_for(1) == 14                     # clamped to the Kiwi's deepest zoom


def test_scope_px_on_icom_scale():
    px = np.frombuffer(scope_px(np.array([-200, -130, -105, -30, 0], np.float32)), np.uint8)
    assert list(px) == [0, 0, 40, 160, 160]
