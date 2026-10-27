# CW decoders

The web app has two CW decoders. Pick one with the selector in the CW decoder header:

| Selector | What copies | What the robot reads |
|---|---|---|
| **Std** | `decoders.CWDecoder` | Std |
| **HMM** | `cwhmm.CWHmm` | HMM |
| **Both** | Std, with the HMM's copy in a second box underneath | Std |

In **Both** the two decoders get the same audio blocks. The HMM follows the standard decoder's pitch, so waterfall
clicks, AFC and the beacon monitor move both. The choice is saved as `cw_engine` in `settings.json`
(websocket: `{"cmd": "decoder", "cw_engine": "standard" | "hmm" | "both"}`).

Both decoders share the front end. A complex mix moves the tone to baseband, a 4th-order Butterworth low-pass filters
it (100 Hz wide), and its magnitude is averaged into a 200 Hz envelope (one sample per 5 ms).

## Standard decoder (`decoders.py`)

A threshold decoder that adapts to the sender:

- **Levels:** a fast-attack/slow-decay peak, and a noise floor tracked while key-up. The key goes down above 55 % of the
  way from noise to peak and up below 40 % (hysteresis).
- **Smoothing:** the envelope is averaged over 0.3 dot, which lifts weak signals without blurring timing.
- **Speed:** the dot length follows the marks: a dot directly, a dash as a third.
- **Noise rejection:** marks shorter than half a 45 WPM dot are ignored, and so is a sub-half-dot blip before a word.
- **Character end:** after `CHAR_GAP` dots of silence, **or sooner after 1.6 of the sender's own element gaps**. Operators
  who leave short gaps inside characters usually leave short gaps between them too, and a fixed dot multiple merged
  those characters into `*`. On the real-audio set this rule took readable copy from 0.77 to about 2.0 words per minute
  and cut junk by about a third.
- **Word end:** the last 30 character/word gaps are split into two clusters, and the threshold goes midway between them
  (3.4-6 dots). Many senders use 4-5 dot word spaces, not 7.
- `cwtune.py` fits the tunable values to recorded KiwiSDR audio (see the README).

## HMM decoder (`cwhmm.py`)

An explicit-duration hidden Markov model, decoded with fixed-lag Viterbi.

- **Hidden state:** speed (13 steps, 10-45 WPM) × element (dot, dash, element gap, character gap, word gap, idle) ×
  time already spent in that element.
- **Durations:** each element's length is log-normal around its nominal dot multiple (1, 3, 1, 3, 6), with a speed
  step allowed at character starts.
- **Soft decisions:** each 5 ms sample is scored for how well it fits key-down or key-up, instead of being thresholded.
  The levels come from the last 3 s: noise from the quietest 40 %, signal from the 97th percentile. The Viterbi runs
  0.5 s behind them, so a station's first element is judged against its own level.
- **Output:** every 0.25 s the best path is traced back. Elements older than 1.5 s are committed, so text arrives
  about 2 s after the audio.
- The trace-back is bounded by the 12 s back-pointer window, and its time index falls on every step. An earlier
  version could step forward in time after a long idle and grew without limit (62 GB). `tests/test_cwhmm.py` guards this.

### Squelch scale

On band noise alone the HMM's raw ratio (97th percentile over the quietest 40 %) reads 3.2-4.0. `level()` divides it
by 2 (`LEVEL_SCALE`), which puts it on the standard decoder's scale. Measured on the same audio:

| Audio | Std `level()` | HMM `level()` |
|---|---|---|
| synthetic noise | 1.2-2.0 | 1.6-2.0 |
| live band noise (IC-7300, 30 s) | 1.2-2.2 | 1.7-2.2 |
| CW at 3 dB SNR | 1.4-4.1 | 2.4-3.3 |
| CW at 6 dB SNR | 2.9-5.8 | 2.9-4.3 |

So one SQL slider drives both decoders, and a new decoder takes the current slider value. The HMM's own default is 2.3.
Its synthetic results at two settings:

| HMM SQL | Junk on 2 min of noise | Copy at 0 / 3 / 6 / 9 dB |
|---|---|---|
| 2.3 | 1-2 characters | 37 / 99 / 100 / 100 % |
| 3.0 | none | 4 / 41 / 97 / 100 % |

## Comparison

The real-audio set is 65.7 minutes of KiwiSDR recordings, scored with `cwtune.py`'s readability measure: QSO words
and valid calls count, `*` and merged words cost. The synthetic tests use `cwsynth.py`.

| | Std | HMM (before the squelch fix) |
|---|---|---|
| Real audio, readable words/min | 2.2 | -5.0 (junk between transmissions) |
| Real audio, good / junk | 510 / 351 | 667 / 2646 |
| Weak synthetic, 0 / 3 / 6 / 9 dB | 0 / 12 / 99 / 100 % | 41 / 96 / 100 / 100 % |
| Fast synthetic, 35 / 40 / 44 WPM (words of 23) | 19 / 19 / 19 | 23 / 23 / 23 |

The HMM copies weak and fast signals far better, but it was measured with the squelch that never closed (see above),
so its junk count isn't valid. Its real-audio figures still have to be re-measured with the fixed squelch. Meanwhile,
**Both** shows the two decoders side by side on live signals.

## Testing

`python3 -m pytest -q tests` covers both decoders: word spacing, weak signals, noise rejection, fast senders, HMM copy,
the bounded trace-back and the HMM's squelch on noise. Run experiments under a memory cap, for example
`systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 python3 ...`, so a runaway can't take down the desktop.
