"""Synthetic CW audio for decoder tests: chosen speed, spacing, timing jitter and noise."""
import difflib

import numpy as np

from decoders import FS, MORSE

CODE = {ch: code for code, ch in MORSE.items()}


def keying(text, wpm, char_gap=3.0, word_gap=7.0, jitter=0.0, rng=None):
    """List of (key_down, seconds) for `text`; gaps in dot units, jitter as a fraction."""
    rng = rng or np.random.default_rng(0)
    dot = 1.2 / wpm
    j = lambda u: max(0.3, u * (1 + jitter * rng.standard_normal())) * dot
    out = [(False, 0.5)]
    for wi, word in enumerate(text.split()):
        if wi:
            out.append((False, j(word_gap)))
        for ci, ch in enumerate(word):
            if ci:
                out.append((False, j(char_gap)))
            for ei, el in enumerate(CODE[ch]):
                if ei:
                    out.append((False, j(1)))
                out.append((True, j(1 if el == "." else 3)))
    out.append((False, 1.5))
    return out


def audio(segments, pitch=700.0, snr_db=20.0, rng=None):
    """Keyed tone plus white noise. snr_db is tone power over noise power in a 500 Hz band."""
    rng = rng or np.random.default_rng(1)
    key = np.concatenate([np.full(int(s * FS), 1.0 if k else 0.0) for k, s in segments])
    ramp = int(0.004 * FS)                            # 4 ms rise/fall like a real rig
    key = np.convolve(key, np.ones(ramp) / ramp, mode="same")
    t = np.arange(len(key)) / FS
    tone = 0.3 * key * np.sin(2 * np.pi * pitch * t)
    noise_pow = (0.3 ** 2 / 2) / 10 ** (snr_db / 10) * (FS / 2 / 500)
    return (tone + rng.normal(0, np.sqrt(noise_pow), len(t))).astype(np.float32)


def decode(dec, x, block=600):
    return "".join(dec.feed(x[i:i + block]) or "" for i in range(0, len(x), block))


def word_score(sent, got):
    """(words copied exactly, words sent) after aligning the two word lists."""
    a, b = sent.split(), got.split()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(m.size for m in sm.get_matching_blocks()), len(a)
