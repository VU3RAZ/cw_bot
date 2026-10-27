"""Rule-based reader for one received CW over.

Extracts the same fields the LLM returned (for_us, call, rst, name, qth, asks_repeat,
sent_73, answer) from imperfect decoder copy, plus a `confidence`:
  "high"  the over follows the usual QSO patterns; act on it without the LLM
  "low"   unusual content (a question, unparsed words): ask the LLM
CW operators send key items twice, so repeated words are used to vote out decoder
errors, and merged doubles ("IGORIGOR") are split first."""
import re

import geo

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


def parse(over, mycall, partner="", my_name="", my_qth=""):
    words = tokens(over)
    used = set()
    info = {"for_us": False, "call": "", "rst": "", "name": "", "qth": "", "asks_repeat": False,
            "sent_73": False, "answer": "", "confidence": "high", "questions": []}
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
