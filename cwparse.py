"""Rule-based reader for one received CW over.

Extracts the same fields the LLM returned (for_us, call, rst, name, qth, asks_repeat,
sent_73, answer) from imperfect decoder copy, plus a `confidence`:
  "high"  the over follows the usual QSO patterns; act on it without the LLM
  "low"   unusual content (a question, unparsed words): ask the LLM
CW operators send key items twice, so repeated words are used to vote out decoder
errors, and merged doubles ("IGORIGOR") are split first."""
import itertools
import re
from collections import Counter
from difflib import SequenceMatcher

import geo

ALLOWED = re.compile(r"<(SK|KN|AR|BT)>|[A-Z0-9/?.,= ]")
CALLSIGN = re.compile(r"^(?=.*\d)[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}[A-Z](/[A-Z0-9]{1,4})?$")
RST = re.compile(r"^[1-5][1-9N][1-9N]$")
NOISE = set("ETIANMS")
# Words of a standard QSO; anything else counts as "unparsed" and lowers confidence.
VOCAB = set("""CQ DE K KN BK SK AR R RR RRR TNX TKS TU TY FER FB GM GA GE GD OM YL UR RST NAME OP QTH HR ES IS
HW 73 GL CUL DR AGN PSE RPT CALL INFO = ? 5NN TEST QRZ QSL QSO VY NW SO OK CFM ALL HPE SRI QRM QSB QRN
NR MY THE AND FOR NICE QSO TO MEET U YR BEST GB GN GUD 599 SIGS NIL CPY FINE VERY HI 72 88 2 THX
QRL PSE ANT RIG PWR WX WATTS W TEMP C F SUNNY RAIN CLOUDY HV OB MNI AGE YRS LIC""".split())
TERMINALS = {"K", "KN", "BK", "SK", "AR", "<SK>", "<KN>", "<AR>"}
REPEAT_WORDS = {"AGN", "AGN?", "QRZ", "QRZ?", "?", "NAME?", "QTH?", "RST?", "CALL?", "NR?", "RPT?"}
SIGNOFF = {"73", "SK", "<SK>", "CUL", "GB", "72"}
QUESTIONS = {"RIG?", "ANT?", "PWR?", "WX?", "AGE?", "TEMP?", "WATTS?"}
FIELD_STOP = {"NAME", "OP", "QTH", "RST", "UR", "HW", "HW?", "ES", "ANT", "RIG", "WX", "PWR", "BK", "K",
              "KN", "=", "DE", "TNX", "TU", "73", "SK", "GL", "R", "SO", "AR", "<KN>", "<SK>", "<AR>"}


def clean_text(text):
    """Upper case, <BT> as '=', only characters CW can send ('*' and the like dropped)."""
    text = (text or "").upper().replace("<BT>", "=")
    return re.sub(r"\s+", " ", "".join(m.group(0) for m in ALLOWED.finditer(text))).strip()


def close_to(a, b):
    """Same length (+-1) and at most 2 differing characters: tolerates decoder errors."""
    if abs(len(a) - len(b)) > 1:
        return False
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b)) <= 2


def is_ours(w, mine):
    """Is this name/QTH word our own echoed back? Short words allow one decoder error,
    not two: with two, BOB would pass for JOE."""
    if len(max(w, mine, key=len)) > 5:
        return close_to(w, mine)
    return abs(len(w) - len(mine)) <= 1 and sum(x != y for x, y in zip(w, mine)) + abs(len(w) - len(mine)) <= 1


def is_call(w):
    return bool(CALLSIGN.match(w)) and not RST.match(w) and geo.lookup(w) is not None


def tokens(over):
    """Decoder copy -> list of words: noise dropped, merged doubles split, split calls joined."""
    raw = over.upper().replace("<BT>", "=").split()
    for i in range(len(raw) - 1):         # "D EDL3YEE": DE split by a long inner gap
        if raw[i] == "D" and raw[i + 1].startswith("E"):
            raw[i], raw[i + 1] = "", "D" + raw[i + 1]
    out = []
    for w in filter(None, raw):
        # DE run into its neighbours: "CQDEDL3YEE" -> CQ DE DL3YEE (operators' spacing varies)
        m = re.fullmatch(r"(CQ|[A-Z0-9/]*?)DE([A-Z0-9/]{3,})", w)
        if m and not is_call(w) and is_call(m.group(2)) and (not m.group(1) or m.group(1) == "CQ"
                                                              or is_call(m.group(1))):
            out += ([m.group(1)] if m.group(1) else []) + ["DE", m.group(2)]
            continue
        h = len(w) // 2
        if len(w) >= 4 and len(w) % 2 == 0 and w[:h] == w[h:]:
            out += [w[:h], w[:h]]                      # IGORIGOR -> IGOR IGOR, 5NN5NN -> 5NN 5NN
        else:
            out.append(w)
    words = []
    i = 0
    while i < len(out):
        w = out[i]
        nxt = out[i + 1] if i + 1 < len(out) else ""
        if (nxt and not is_call(w) and not is_call(nxt) and w not in VOCAB and is_call(w + nxt)
                and len(w) <= 3):
            words.append(w + nxt)                      # "VU 2TMP" -> VU2TMP
            i += 2
            continue
        if not (len(w) <= 2 and set(w) <= NOISE and w not in VOCAB):
            words.append(w)                            # stray E/T/I/A... is noise
        i += 1
    return words


# ---------------------------------------------------------------- callsign FEC
# CW operators send calls at least twice, and an over often names a station heard
# before (or spotted on this frequency). Every copy of a call is a vote: the copies are
# aligned character by character and each position takes the majority, so two
# different decoder errors in two copies cancel out ("DL3YFE DL3TEE DL3YEE" -> DL3YEE).
def near_call(a, b):
    """Close enough to be the same call misdecoded: 1 differing character up to 5
    letters, 2 from 6 (lengths +-1). Stricter than close_to, so OK1RR and OK1AB stay apart."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = sum(x != y for x, y in zip(a, b))
    else:
        m = SequenceMatcher(None, a, b, autojunk=False)
        diff = max(len(a), len(b)) - sum(bl.size for bl in m.get_matching_blocks())
    return diff <= (1 if max(len(a), len(b)) <= 5 else 2)


def consensus(votes, known=()):
    """Character majority over weighted spellings {spelling: weight} of one call.
    Ties are settled by which reading is a known call, then a valid one (cty.dat
    prefix); None if they still can't be."""
    votes = dict(votes)
    for v in list(votes):               # "EA6NBK": the K prosign run into the call
        if v.endswith("K") and v[:-1] in votes:
            votes[v[:-1]] += votes.pop(v)
    if any(a != b and a.startswith(b) for a in votes for b in votes):
        return None                     # OY1C vs OY1CT: a dropped last letter or an extra one, can't tell
    ref = max(votes, key=lambda v: (votes[v], v in known, is_call(v), len(v)))
    cols = [Counter() for _ in ref]
    for v, w in votes.items():
        if len(v) == len(ref):          # same length: position by position
            for k, ch in enumerate(v):
                cols[k][ch] += w
            continue
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, ref, v, autojunk=False).get_opcodes():
            if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
                for k in range(i2 - i1):
                    cols[i1 + k][v[j1 + k]] += w
            elif tag == "delete":
                for k in range(i1, i2):
                    cols[k][""] += w
    options = []
    for c, col in zip(ref, cols):
        top = max(col.values()) if col else 0
        options.append([x for x, n in col.items() if n == top] or [c])
    scored = sorted(((cand in known, is_call(cand)), cand) for cand in
                    ("".join(c) for c in itertools.islice(itertools.product(*options), 64)))
    best = [c for sc, c in scored if sc == scored[-1][0]]
    if len(best) > 1:
        # e.g. DL3YFE once, DL3YEE once, both valid, neither known: the error is
        # seen but can't be put right, so the copies are left as received
        return None
    out = best[0]
    total = sum(votes.values())
    if out not in votes and out not in known and not (
            total >= 3 and len({len(v) for v in votes}) == 1
            and all(max(col.values()) * 2 > total for col in cols)):
        out = ""                        # a spelling nobody sent needs a clear majority
    if not is_call(out) and out not in known:     # a majority that isn't a call: trust a whole copy
        valid = [v for v in votes if v in known or is_call(v)]
        top = max((votes[v], v in known) for v in valid) if valid else None
        valid = [v for v in valid if (votes[v], v in known) == top]
        return valid[0] if len(valid) == 1 else None
    return out


def correct_calls(words, known=None):
    """Repair the calls in an over by voting across their copies. known: {call: weight}
    of calls that count as extra votes (our call, the station we work, calls spotted on
    this frequency, calls read in earlier overs). Returns (words, {copy: corrected})."""
    known = known or {}
    def callish(w):
        return (len(w) >= 4 and w not in VOCAB and not RST.match(w) and not w.endswith("?")
                and not set(w) <= NOISE | {"H"})       # TTEE, EISH: noise, not a broken call
    counts = Counter(w for w in words if callish(w))
    # stations: valid calls sent twice, or known; two of them are never merged
    strong = [w for w in counts if is_call(w) and (counts[w] >= 2 or w in known)]
    strong += [k for k in known if k not in strong]
    groups = {a: Counter({a: known.get(a, 0)}) for a in strong}
    # call-shaped copies first, so a copy with a broken digit has a group to join
    for w, n in sorted(counts.items(), key=lambda wn: not (is_call(wn[0]) or any(c.isdigit() for c in wn[0]))):
        if w in groups:
            groups[w][w] += n
            continue
        homes = [a for a in groups if near_call(w, a)]
        if homes:
            groups[min(homes, key=lambda a: -groups[a][a])][w] += n
        elif is_call(w) or any(c.isdigit() for c in w):
            # a call sent once, or two copies that disagree: they vote with each other
            mates = [a for a in groups if not a in strong and near_call(w, a)]
            (groups[mates[0]] if mates else groups.setdefault(w, Counter()))[w] += n
    fixed = {}
    for g in groups.values():
        votes = {v: n for v, n in g.items() if n}
        if sum(votes.values()) < 2 or not any(v in counts for v in votes):
            continue
        right = consensus(votes, known)
        if right is None:
            continue
        fixed.update({v: right for v in votes if v in counts and v != right})
    return [fixed.get(w, w) for w in words], fixed


def best_of(cands):
    """Most frequent candidate, merging near-identical spellings (decoder errors)."""
    groups = []
    for c in cands:
        for g in groups:
            if close_to(c, g[0]):
                g.append(c)
                break
        else:
            groups.append([c])
    if not groups:
        return ""
    g = max(groups, key=len)
    return max(set(g), key=g.count)


def field_after(words, keys, used):
    """Value following NAME/OP/QTH ('NAME HR HANS HANS' -> HANS; 'QTH NEW YORK' -> NEW YORK)."""
    vals = []
    for i, w in enumerate(words):
        if w not in keys:
            continue
        j = i + 1
        while j < len(words) and words[j] in ("IS", "HR", "NR"):
            j += 1
        val = []
        while j < len(words) and words[j] not in FIELD_STOP and not words[j].endswith("?") and len(val) < 3:
            if val and close_to(words[j], val[0]):     # repeat of the first word: done
                vals.append(words[j])
                used.add(j)
                break
            val.append(words[j])
            used.add(j)
            j += 1
        if val:
            used.add(i)
            vals.append(" ".join(val))
    return best_of(vals)


def parse(over, mycall, partner="", my_name="", my_qth="", known=None):
    """known: {call: weight}, extra votes for correct_calls (spotted calls and the like)."""
    known = dict(known or {})
    for c in (mycall, partner):
        if c and c != "-":
            known[c] = max(known.get(c, 0), 2)
    words, _ = correct_calls(tokens(over), known)
    used = set()
    info = {"for_us": False, "call": "", "to": "", "cq": False, "rst": "", "name": "", "qth": "",
            "asks_repeat": False, "sent_73": False, "answer": "", "confidence": "high", "questions": []}
    if not words:
        info["confidence"] = "high"        # nothing but noise
        return info

    flat = "".join(words)
    my_heard = mycall in flat or any(close_to(mycall, w) for w in words if len(w) >= 4)

    # Calls: sender follows DE, addressee precedes it.
    sender, addressee, calls = "", "", []
    for i, w in enumerate(words):
        if is_call(w) or (len(w) >= 4 and (close_to(w, mycall) or (partner and close_to(w, partner)))):
            calls.append(w)
            used.add(i)
        if w == "DE":
            used.add(i)
            if i + 1 < len(words) and (is_call(words[i + 1]) or (partner and close_to(words[i + 1], partner))):
                sender = words[i + 1]
            if i > 0 and (is_call(words[i - 1]) or close_to(words[i - 1], mycall)):
                addressee = words[i - 1]
    if not sender:
        others = [c for c in calls if not close_to(c, mycall)]
        sender = best_of(others) if others else ""
    if partner and sender and close_to(sender, partner):
        sender = partner
    elif sender:
        sender = best_of([c for c in calls if close_to(c, sender)]) or sender
    info["call"] = sender if sender and not close_to(sender, mycall) else ""

    cq = words[0] in ("CQ", "TEST") or (len(words) > 1 and words[1] == "CQ")
    info["cq"] = cq
    info["to"] = addressee if addressee and addressee != info["call"] else ""
    to_other = addressee and not close_to(addressee, mycall)
    from_other = partner and info["call"] and info["call"] != partner
    if my_heard:
        info["for_us"] = True
    elif cq or to_other or from_other:
        info["for_us"] = False
    else:
        info["for_us"] = bool(partner)    # mid-contact overs often omit our call

    # Report: an RST-shaped word, preferring the ones after RST/UR and repeated ones.
    rsts = []
    for i, w in enumerate(words):
        if RST.match(w):
            rsts.append(w.replace("N", "9"))
            used.add(i)
    info["rst"] = best_of(rsts)

    name = field_after(words, ("NAME", "OP"), used)
    qth = field_after(words, ("QTH",), used)
    info["name"] = "" if is_ours(name, my_name or "-") else name
    info["qth"] = "" if is_ours(qth, my_qth or "-") else qth

    info["asks_repeat"] = any(w in REPEAT_WORDS for w in words) or "PSE RPT" in " ".join(words)
    tail = words[-1:]
    info["sent_73"] = any(w in SIGNOFF for w in words) or tail == ["TU"]
    info["questions"] = [w for w in words if w in QUESTIONS]

    # Confidence: questions need a composed answer; many unknown words mean we may misread.
    unknown = [w for i, w in enumerate(words) if i not in used and w not in VOCAB
               and w.rstrip("?") not in VOCAB and not RST.match(w)]
    if info["questions"] or (info["for_us"] and len(unknown) > max(2, len(words) // 4)):
        info["confidence"] = "low"
    info["unknown"] = unknown
    return info


class OverReader:
    """Splits the live decoder copy into overs and reads each one, for the CW tile's
    Parsed view. An over ends on a terminal prosign (K, KN, BK, SK, AR) or after QUIET
    seconds without copy. feed() and tick() return the overs whose reading changed:
    {id, done, text (cleaned words), calls, cq, call, to, rst, name, qth, sent_73,
    asks_repeat}; text is "" when an over turned out to be only noise."""
    QUIET = 5.0
    MAX_CHARS = 600         # a runaway over (no prosign, no pause) is cut here

    HISTORY = 900           # s: calls read in earlier overs vote this long

    def __init__(self):
        self.id, self.raw, self.last = 0, "", 0.0
        self.heard = {}             # call -> (copies, last time), from closed overs
        self.hints = {}             # call -> weight from outside (spots on the dial frequency)

    def known(self, now, mycall=""):
        # a call copied twice or more (one copy may be noise read as a call)
        k = {c: min(n, 2) for c, (n, t) in self.heard.items() if n >= 2 and now - t < self.HISTORY}
        for c, w in self.hints.items():
            k[c] = max(k.get(c, 0), w)
        if mycall:
            k[mycall] = 2
        return k

    def feed(self, text, now, mycall=""):
        self.raw += text
        self.last = now
        out = []
        while True:
            parts = self.raw.split(" ")
            whole = parts[:-1]          # the last part may be a word still arriving
            end = next((i for i, w in enumerate(whole) if clean_text(w) in TERMINALS), None)
            if end is None and len(self.raw) > self.MAX_CHARS:
                end = len(whole) - 1
            if end is None or end < 0:
                break
            out.append(self._close(" ".join(parts[:end + 1]), mycall, now))
            self.raw = " ".join(parts[end + 1:])
        if self.raw.strip():
            o = self.read(self.id, self.raw, False, mycall, self.known(now, mycall))
            if o["text"]:               # don't show an over that is only noise so far
                out.append(o)
        return out

    def tick(self, now, mycall=""):
        if self.raw.strip() and now - self.last > self.QUIET:
            o = self._close(self.raw, mycall, now)
            self.raw = ""
            return [o]
        return []

    def _close(self, over, mycall, now):
        o = self.read(self.id, over, True, mycall, self.known(now, mycall))
        self.id += 1
        for c in set(o["calls"]):
            if c != mycall:             # copies, so a call sent twice counts from its first over
                self.heard[c] = (self.heard.get(c, (0, 0))[0] + o["calls"].count(c), now)
        return o

    @staticmethod
    def read(oid, over, done, mycall="", known=None):
        words = tokens(clean_text(over))
        if not any(len(w) >= 2 or w in VOCAB or w.isdigit() for w in words):
            words = []                  # nothing but noise
        words, fixed = correct_calls(words, known)
        info = parse(" ".join(words), mycall or "-", known=known) if words else {}
        return {"id": oid, "done": done, "text": " ".join(words), "fixed": fixed,
                "calls": [w for w in words if is_call(w)],
                **{k: info.get(k, "") for k in ("call", "to", "rst", "name", "qth")},
                **{k: bool(info.get(k)) for k in ("cq", "sent_73", "asks_repeat")}}
