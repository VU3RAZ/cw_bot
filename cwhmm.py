"""Explicit-duration HMM CW decoder (soft decisions, fixed-lag Viterbi).

Hidden state: speed s (WPM grid) x element e (dot, dash, element gap, char gap,
word gap, idle) x samples spent in the element. Each 5 ms envelope sample is scored
against key-down / key-up level models instead of being thresholded; element lengths
follow log-normal distributions around their nominal dot multiples. The most likely
path is traced back every STEP samples and elements older than LAG are committed.

Same interface as decoders.CWDecoder: feed(), set_freq(), retune(), freq, wpm,
squelch, level().
"""
from collections import deque

import numpy as np

from decoders import FS, MORSE, ToneFilter

DOT, DASH, EG, CG, WG, IDLE = range(6)
MARKS, GAPS = (DOT, DASH), (EG, CG, WG)
NEG = -1e30


class CWHmm:
    DECIM = 60                      # 200 Hz envelope
    R = FS / DECIM
    BANDWIDTH = 100.0
    SQUELCH = 2.3                   # level() needed to decode; band noise alone reads 1.6-2.0
    LEVEL_SCALE = 2.0               # noise alone gives sig/noise 3.2-4.0 here (97th percentile over the
                                    # quietest 40%); /2 puts level() on CWDecoder's scale, so one SQL works
    WPMS = (10, 12, 14, 16, 18, 20, 22, 25, 28, 32, 36, 40, 45)
    NOMINAL = {DOT: 1.0, DASH: 3.0, EG: 1.0, CG: 3.0, WG: 6.0}
    SPREAD = {DOT: 0.3, DASH: 0.25, EG: 0.35, CG: 0.3, WG: 0.35}   # log-normal sigma
    KMAX = {DOT: 2.2, DASH: 5.0, EG: 2.2, CG: 5.0, WG: 10.0}        # chain length, dots
    P_AFTER_MARK = {EG: 0.62, CG: 0.3, WG: 0.08}
    P_DASH = 0.45
    P_SPEED = 0.02                  # speed step at a character start
    WINDOW = 3.0                    # s of envelope for the level estimates
    MARK_SPREAD = 0.2               # extra key-down level spread (QSB), of signal level
    NOISE_INFL = 1.5                # noise sigma inflation (estimate from low part)
    CLIP = 10.0                     # log-likelihood floor per sample (impulse noise)
    LOOK = 0.5                      # s the Viterbi runs behind the level estimate
    LAG = 1.5                       # s before an element is committed
    STEP = 50                       # samples between trace-backs

    def __init__(self, freq=700.0, bandwidth=None, squelch=None, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.bandwidth = bandwidth or self.BANDWIDTH
        self.squelch = squelch or self.SQUELCH
        self.set_freq(freq)
        self.acc = np.zeros(0, np.float32)
        self.S = len(self.WPMS)
        self.L = np.array([1.2 / w * self.R for w in self.WPMS])      # dot, samples
        self._tables()
        self.delta = {e: np.full((self.S, self.K[e]), NEG) for e in self.K}
        self.idle = np.zeros(self.S) + np.log(1.0 / self.S)
        self.idle_start = np.zeros(self.S, int)     # when each speed's idle began
        self.wg_last = np.ceil(self.KMAX[WG] * self.L).astype(int) - 1
        self.t = 0
        self.bp = deque(maxlen=int(12 * self.R))   # per sample: {entry e: (prev e, prev s, prev k)}
        self.hist = deque(maxlen=int(self.WINDOW * self.R))
        self.look = deque()
        self.noise, self.sig, self.nsd, self.ok = 1.0, 0.0, 1.0, False
        self.done_t = 0
        self.sym, self.out_space = "", True
        self.best_s = self.WPMS.index(20)
        self.flushed_at = -1

    # ---------------------------------------------------------------- setup
    def set_freq(self, freq):
        self.freq = float(freq)
        self.filt = ToneFilter(self.freq, self.bandwidth / 2)

    def retune(self, freq):
        self.freq = float(freq)
        self.filt.freq = self.freq

    @property
    def wpm(self):
        return self.WPMS[self.best_s]

    def level(self):
        return float(self.sig / (self.noise * self.LEVEL_SCALE + 1e-12))

    def _tables(self):
        """Per element: log continue / log exit after duration d = k+1, per speed."""
        self.K, self.lcont, self.lexit = {}, {}, {}
        for e, n in self.NOMINAL.items():
            K = int(np.ceil(self.KMAX[e] * self.L.max())) + 1
            d = np.arange(1, K + 1)[None, :].astype(float)
            mu = np.log(n * self.L)[:, None]
            sg = self.SPREAD[e]
            pdf = np.exp(-0.5 * ((np.log(d) - mu) / sg) ** 2) / d
            kmax = np.ceil(self.KMAX[e] * self.L).astype(int)
            for i in range(self.S):
                pdf[i, kmax[i]:] = 0.0
            pdf /= pdf.sum(axis=1, keepdims=True)
            surv = np.cumsum(pdf[:, ::-1], axis=1)[:, ::-1]           # P(D >= d)
            with np.errstate(divide="ignore", invalid="ignore"):
                ex = np.log(pdf / surv)
                co = np.log(np.concatenate([surv[:, 1:], np.zeros((self.S, 1))], axis=1) / surv)
            ex[~np.isfinite(ex)] = NEG
            co[~np.isfinite(co)] = NEG
            self.K[e], self.lexit[e], self.lcont[e] = K, ex, co
        self.lmark = {DOT: np.log(1 - self.P_DASH), DASH: np.log(self.P_DASH)}
        self.lgap = {e: np.log(p) for e, p in self.P_AFTER_MARK.items()}
        self.lstay = np.log(1 - 2 * self.P_SPEED)
        self.lstep = np.log(self.P_SPEED)
        self.lidle_stay = np.log(0.995)
        self.lidle_exit = np.log(0.005)

    # ---------------------------------------------------------------- levels
    def _levels(self):
        h = np.fromiter(self.hist, float)
        lo, hi = np.percentile(h, [40, 97])
        low = h[h <= lo]
        self.noise = float(low.mean())
        self.nsd = float(low.std() * self.NOISE_INFL + 1e-9)
        self.sig = float(hi)
        self.ok = self.level() > self.squelch

    def _emis(self, v):
        """(log p(v | key-down), log p(v | key-up))."""
        if not self.ok:
            return -self.CLIP, 0.0
        sd_g = self.nsd
        sd_m = np.hypot(self.nsd, self.MARK_SPREAD * (self.sig - self.noise))
        lg = -0.5 * ((v - self.noise) / sd_g) ** 2 - np.log(sd_g)
        lm = -0.5 * ((v - self.sig) / sd_m) ** 2 - np.log(sd_m)
        if v > self.sig:
            lm = -np.log(sd_m)                   # louder than usual is still key-down
        top = max(lm, lg)
        return max(lm, top - self.CLIP), max(lg, top - self.CLIP)

    # ---------------------------------------------------------------- Viterbi step
    def _step(self, v):
        lm, lg = self._emis(v)
        d = self.delta
        # best exit score per element and speed, and its k
        ex, exk = {}, {}
        for e in d:
            sc = d[e] + self.lexit[e]
            exk[e] = sc.argmax(axis=1)
            ex[e] = sc[np.arange(self.S), exk[e]]
        bp = {}
        new = {}
        # gaps are entered from a mark at the same speed
        mk = np.where(ex[DASH] > ex[DOT], DASH, DOT)
        mval = np.maximum(ex[DOT], ex[DASH])
        mkk = np.where(mk == DASH, exk[DASH], exk[DOT])
        for g in GAPS:
            ent = mval + self.lgap[g]
            new[g] = ent
            bp[g] = (mk, np.arange(self.S), mkk)
        # marks: from an element gap (same speed), or a char/word gap / idle (speed may step)
        cg = np.maximum(ex[CG], ex[WG])
        cge = np.where(ex[WG] > ex[CG], WG, CG)
        cgk = np.where(cge == WG, exk[WG], exk[CG])
        idle_ex = self.idle + self.lidle_exit
        use_idle = idle_ex > cg
        cg = np.where(use_idle, idle_ex, cg)
        cge = np.where(use_idle, IDLE, cge)
        cgk = np.where(use_idle, 0, cgk)
        stay = cg + self.lstay
        up = np.concatenate([[NEG], cg[:-1]]) + self.lstep        # from slower neighbour
        dn = np.concatenate([cg[1:], [NEG]]) + self.lstep         # from faster neighbour
        src = np.argmax(np.stack([stay, up, dn]), axis=0)
        cval = np.max(np.stack([stay, up, dn]), axis=0)
        ps = np.arange(self.S) + np.array([0, -1, 1])[src]
        ps = np.clip(ps, 0, self.S - 1)
        egv = ex[EG]
        from_eg = egv > cval
        mval2 = np.where(from_eg, egv, cval)
        pe = np.where(from_eg, EG, cge[ps])
        pss = np.where(from_eg, np.arange(self.S), ps)
        pk = np.where(from_eg, exk[EG], cgk[ps])
        for m in MARKS:
            new[m] = mval2 + self.lmark[m]
            bp[m] = (pe, pss, pk)
        # idle: stays, or is entered when a word gap reaches the end of its chain
        last = d[WG][np.arange(self.S), self.wg_last]
        stay_idle = self.idle + self.lidle_stay
        idle_in = np.maximum(stay_idle, last)
        self.idle_start = np.where(last > stay_idle, self.t, self.idle_start)
        bp[IDLE] = self.idle_start.copy()     # an old idle's start, for the trace-back
        # advance chains and add emissions
        for e in d:
            x = np.empty_like(d[e])
            x[:, 0] = new[e]
            x[:, 1:] = d[e][:, :-1] + self.lcont[e][:, :-1]
            x += lm if e in MARKS else lg
            d[e] = x
        self.idle = idle_in + lg
        # normalise
        top = max(max(d[e].max() for e in d), self.idle.max())
        for e in d:
            d[e] -= top
        self.idle -= top
        self.bp.append(bp)
        self.t += 1

    # ---------------------------------------------------------------- trace-back
    def _best(self):
        best, arg = self.idle.max(), (IDLE, int(self.idle.argmax()), 0)
        for e, x in self.delta.items():
            i = np.unravel_index(x.argmax(), x.shape)
            if x[i] > best:
                best, arg = x[i], (e, int(i[0]), int(i[1]))
        return arg

    def _trace(self):
        """Elements on the best path, oldest first: [(start, end, e, s)]; the last is in progress."""
        e, s, k = self._best()
        self.best_s = s
        t = self.t - 1
        els = []
        n = len(self.bp)
        # t falls on every pass and stays inside the back-pointer window, so this ends
        # (an unbounded version once walked forward in time and filled 62 GB)
        while t >= 0 and self.t - t <= n:
            if e == IDLE:
                start = int(self.bp[t - (self.t - n)][IDLE][s])
                if start > t:           # never walk forward in time
                    break
                els.append((start, t, e, s))
                if start <= self.done_t or self.t - start > n:
                    break
                e, k, t = WG, int(self.wg_last[s]), start - 1
                continue
            start = t - k
            if start > t:
                break
            els.append((start, t, e, s))
            if start <= self.done_t or self.t - start >= n:
                break
            pe, ps, pk = (a[s] for a in self.bp[start - (self.t - n)][e])
            e, s, k, t = int(pe), int(ps), int(pk), start - 1
        return els[::-1]

    def _emit(self, e):
        if e in MARKS:
            self.sym += "." if e == DOT else "-"
            self.out_space = False
            return ""
        if e == EG:
            return ""
        out = ""
        if self.sym:
            out, self.sym = MORSE.get(self.sym, "*"), ""
        if e in (WG, IDLE) and not self.out_space:
            out += " "
            self.out_space = True
        return out

    def _commit(self):
        els = self._trace()
        lag = int(self.LAG * self.R)
        out = []
        for start, end, e, s in els[:-1]:
            if start < self.done_t or end >= self.t - lag:
                continue
            out.append(self._emit(e))
            self.done_t = end + 1
        start, end, e, s = els[-1]
        # an in-progress long silence: flush the pending character / word space
        if e in (CG, WG, IDLE) and start < self.t - lag and start >= self.done_t - 1:
            out.append(self._emit(CG if e == CG else WG))
        return "".join(out)

    # ---------------------------------------------------------------- feed
    def feed(self, x):
        self.acc = np.concatenate([self.acc, self.filt(x)])
        n = len(self.acc) // self.DECIM
        if n == 0:
            return ""
        env = self.acc[:n * self.DECIM].reshape(n, self.DECIM).mean(axis=1)
        self.acc = self.acc[n * self.DECIM:]
        out = []
        for v in env:
            # levels come from the newest audio, the Viterbi runs LOOK behind, so a
            # signal's first element is judged against its own level, not the noise's
            self.hist.append(float(v))
            self.look.append(float(v))
            if len(self.look) <= self.LOOK * self.R:
                continue
            if self.t % 25 == 0:
                self._levels()
            self._step(self.look.popleft())
            if self.t % self.STEP == 0:
                out.append(self._commit())
        return "".join(out)
