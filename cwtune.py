"""Tune the CW decoder's parameters on real KiwiSDR audio recorded by corpus.py.

  python3 cwtune.py            search, report, and save decoder_params.json if better
  python3 cwtune.py --dry-run  search and report only
  python3 cwtune.py --score    score the current parameters only

The decoder is rule-based, so "learning" means fitting its thresholds (tone filter
width, squelch, envelope smoothing, glitch length, key hysteresis, dash/character/word
gap lengths) to real signals.
Without labels, decoded copy is scored for readability: known QSO words, valid
callsigns and reports score their length; '*' (unknown character) and unknown words
(often two merged words) cost. Overs typed in by hand in logs/corpus/labels.jsonl
({"audio": name.raw, "start_s", "end_s", "text"}, seconds into the recording) are scored by
exact word matches
and count most. A change is kept only if the synthetic test suite does not get worse.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import numpy as np

import cwparse
import cwsynth
from corpus import AUDIO, CORPUS, MIN_CONTRAST, keyed_peak
from decoders import CW_PARAMS, FS, CWDecoder

HERE = os.path.dirname(os.path.abspath(__file__))

WINDOW = 6.0                # seconds per pitch measurement
MAX_MINUTES = 60            # of recorded audio used per run (newest recordings first)
SEARCH = {                  # candidate values per parameter (coordinate descent)
    "BANDWIDTH": [60, 80, 100, 130, 160],
    "SQUELCH": [2.5, 3, 4, 5, 6],
    "SMOOTH": [0.2, 0.3, 0.4, 0.5],
    "GLITCH": [0.35, 0.5, 0.65],
    "KEY_ON": [0.45, 0.5, 0.55, 0.6, 0.65],
    "KEY_OFF": [0.3, 0.35, 0.4, 0.45],
    "DASH": [1.7, 1.85, 2.0, 2.2],
    "CHAR_GAP": [2.0, 2.2, 2.5, 2.8],
    "WORD_GAP_DEFAULT": [3.6, 4.0, 4.5, 5.0],
    "MIN_WORD_GAP": [3.0, 3.2, 3.4, 3.7],
}
SYNTH_TEXT = ("CQ CQ DE DL3YEE DL3YEE K N0CALL DE UA7N GM TNX CALL UR RST 579 579 NAME IGOR IGOR "
              "QTH ROSTOV ROSTOV HW KN R TNX FER RPT 73 GL SK")


def recordings(max_minutes=MAX_MINUTES):
    """[(name, samples, plan)] for recordings that contain a keyed signal, newest first.
    plan is [(sample index, pitch Hz)]: where the strongest keyed tone (contrast >=
    MIN_CONTRAST) moves, measured per WINDOW, like the live capture's retuning."""
    out, total = [], 0.0
    for raw in sorted(glob.glob(os.path.join(AUDIO, "*.raw")), reverse=True):
        x = np.fromfile(raw, "<i2").astype(np.float32) / 32768
        w = int(WINDOW * FS)
        plan = []
        for i in range(0, len(x) - w + 1, w):
            hz, c = keyed_peak(x[i:i + w])
            if c >= MIN_CONTRAST and (not plan or abs(hz - plan[-1][1]) > 15):
                plan.append((i, hz))
        if plan:
            out.append((os.path.basename(raw), x, plan))
            total += len(x) / FS / 60
        if total >= max_minutes:
            break
    return out


def replay(x, plan, block=600):
    """Decode a whole recording with one decoder that follows the plan's pitch.
    Returns [(seconds, text)] per block that printed something."""
    dec, out, k = CWDecoder(plan[0][1]), [], 1
    for i in range(0, len(x), block):
        while k < len(plan) and plan[k][0] <= i:
            hz = plan[k][1]
            dec.retune(hz) if abs(hz - dec.freq) <= 50 else dec.set_freq(hz)
            k += 1
        txt = dec.feed(x[i:i + block])
        if txt:
            out.append((i / FS, txt))
    return out


def labels():
    try:
        with open(os.path.join(CORPUS, "labels.jsonl")) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def readability(text):
    """Score of decoded copy: length of words a QSO reader understands, minus junk."""
    score = 0.0
    for w in text.split():
        core = w.rstrip("?")
        if core in cwparse.VOCAB and len(core) >= 2 or cwparse.RST.match(core) or cwparse.is_call(core):
            score += len(core)
        elif "*" in w:
            score -= 2 * w.count("*")
        elif len(w) <= 2 and set(w) <= cwparse.NOISE:
            score -= 0.5            # stray E/T/I: noise keyed through
        elif len(w) >= 6 and not re.search(r"\d", w):
            score -= 1              # long unknown word: usually two words merged
    return score


def decode(x, hz):
    return cwsynth.decode(CWDecoder(hz), x)


FLOOR = -5.0                # per-minute floor for one recording's readability


def evaluate(recs, labs):
    """(label word accuracy or None, mean readability per minute over recordings).
    Everything the decoder prints counts, so junk between signals costs as well as
    missed copy; each recording's rate is floored at FLOOR so one unreadable pileup
    can't outweigh the readable QSOs."""
    rates, ok, tot = [], 0, 0
    for name, x, plan in recs:
        chunks = replay(x, plan)
        r = readability("".join(t for _, t in chunks)) / (len(x) / FS / 60)
        rates.append(max(r, FLOOR))
        for lab in labs:
            if lab["audio"] == name:
                got = "".join(t for ts, t in chunks if lab["start_s"] <= ts <= lab["end_s"] + 1)
                o, t = cwsynth.word_score(lab["text"], got)
                ok, tot = ok + o, tot + t
    return (ok / tot if tot else None), (sum(rates) / len(rates) if rates else 0.0)


def synthetic():
    """Word accuracy on the synthetic suite (speeds, spacings, jitter, SNR)."""
    rng = np.random.default_rng(7)
    ok = tot = 0
    for wpm in (15, 22, 30):
        for wg in (4.0, 4.5, 5.5, 7.0):
            for jit in (0.0, 0.08, 0.15):
                for snr in (10, 15, 30):
                    x = cwsynth.audio(cwsynth.keying(SYNTH_TEXT, wpm, 3.0, wg, jit, rng), 700, snr, rng)
                    o, t = cwsynth.word_score(SYNTH_TEXT, decode(x, 700))
                    ok, tot = ok + o, tot + t
    return ok / tot


def objective(acc, read):
    return (acc if acc is not None else 0) * 1000 + read


def tests_pass(p):
    """Run the repo's test suite (tests/) with decoder parameters p."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"params": p}, f)
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", os.path.join(HERE, "tests")],
                           capture_output=True, text=True, env=dict(os.environ, CW_DECODER_PARAMS=f.name))
        return r.returncode == 0
    finally:
        os.remove(f.name)


def apply(p):
    for k, v in p.items():
        setattr(CWDecoder, k, float(v))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--minutes", type=float, default=MAX_MINUTES)
    a = ap.parse_args()
    t0 = time.time()
    segs, labs = recordings(a.minutes), labels()
    minutes = sum(len(x) for _, x, _ in segs) / FS / 60
    print(f"{len(segs)} recordings with keyed signals, {minutes:.1f} min of audio; {len(labs)} labelled overs")
    if minutes < 3:
        raise SystemExit("too little keyed audio: run  corpus.py capture  first")
    start = CWDecoder.params()
    acc0, read0 = evaluate(segs, labs)
    syn0 = synthetic()
    print(f"current: readability {read0:.1f}/min, labels {acc0 if acc0 is None else f'{acc0:.0%}'}, synthetic {syn0:.0%}")
    if a.score:
        return
    best, best_obj = dict(start), objective(acc0, read0)
    for rnd in range(2):
        improved = False
        for k, values in SEARCH.items():
            for v in values:
                if v == best[k]:
                    continue
                trial = dict(best, **{k: v})
                if trial["KEY_OFF"] >= trial["KEY_ON"] or trial["MIN_WORD_GAP"] > trial["WORD_GAP_DEFAULT"]:
                    continue
                apply(trial)
                o = objective(*evaluate(segs, labs))
                if o > best_obj + max(0.02, abs(best_obj) * 0.002):   # ignore noise-level gains
                    best, best_obj, improved = trial, o, True
                    print(f"  round {rnd + 1}: {k} = {v} -> objective {o:.1f}")
            apply(best)
        if not improved:
            break
    # The test suite (word spacing for short-spacing senders, weak signals, noise junk,
    # parser cases) must still pass: a corpus from a few operators can pull thresholds
    # too far. Greedily revert the change whose loss costs least until it does.
    while best != start and not tests_pass(best):
        options = []
        for k in [k for k in best if best[k] != start[k]]:
            trial = dict(best, **{k: start[k]})
            apply(trial)
            options.append((objective(*evaluate(segs, labs)), k, trial))
        o, k, best = max(options, key=lambda t: t[0])
        print(f"  fails tests/: reverting {k} -> objective {o:.1f}")
    apply(best)
    acc1, read1 = evaluate(segs, labs)
    syn1 = synthetic()
    changed = {k: (start[k], best[k]) for k in best if best[k] != start[k]}
    print(f"tuned:   readability {read1:.1f}/min, labels {acc1 if acc1 is None else f'{acc1:.0%}'}, synthetic {syn1:.0%}")
    print("changes:", ", ".join(f"{k} {a_}->{b_}" for k, (a_, b_) in changed.items()) or "none")
    keep = changed and syn1 >= syn0 - 0.01 and objective(acc1, read1) > objective(acc0, read0)
    if not keep:
        print("not saved:", "no improvement" if not changed or objective(acc1, read1) <= objective(acc0, read0)
              else f"synthetic suite got worse ({syn0:.0%} -> {syn1:.0%})")
    elif a.dry_run:
        print("dry run: not saved")
    else:
        with open(CW_PARAMS, "w") as f:
            json.dump({"params": best, "tuned_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       "corpus_minutes": round(minutes, 1), "labelled_overs": len(labs),
                       "readability_per_min": [round(read0, 1), round(read1, 1)],
                       "label_accuracy": [acc0, acc1], "synthetic_accuracy": [round(syn0, 3), round(syn1, 3)]},
                      f, indent=1)
        print(f"saved {os.path.basename(CW_PARAMS)}; restart the server to use it")
    print(f"({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
