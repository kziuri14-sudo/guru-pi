"""Rezultatai iš LiveScore (futbolas, krepšinis, tenisas, ledo ritulys – visos lygos, ne tik didžiosios).

Viešas LiveScore svetainės duomenų šaltinis: vienas užklausimas = visos dienos rungtynės su rezultatais,
kėliniais, kėlinių/setų taškais. Naudojamas pirmiausia, ESPN – žaidėjų statistikai, AI – kai niekas neturi.
"""
import gzip
import json
import re, difflib
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta

HOST = "https://prod-cdn-public-api.livescore.com/v1/api/app/date/{sport}/{day}/{tz}?locale=en&MD=1"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36", "Accept": "application/json"}
SPORT_OF = {"soccer": "soccer", "football": "soccer", "futbolas": "soccer",
            "basketball": "basketball", "nba": "basketball", "krepšinis": "basketball",
            "tennis": "tennis", "tenisas": "tennis",
            "hockey": "hockey", "ice hockey": "hockey", "nhl": "hockey"}
NOT_LS = {"nfl", "american football", "amerikietiškas futbolas", "ncaaf", "mma", "ufc", "boxing", "handball",
          "volleyball", "formula1", "f1", "baseball", "esports", "darts", "snooker", "rugby", "cricket"}
def ls_sport(b):
    """Sporto pavadinimas LiveScore paieškai; False – LiveScore šio sporto neturi (NFL, MMA, rankinis...)."""
    sraw = (b.get("sport") or "").lower().strip()
    lg = (b.get("league") or "").lower()
    if re.search(r"\b(iihf|nhl|khl|shl|liiga|hockey|ledo)", lg):
        return "hockey"                                  # AI kartais suklysta dėl sporto, lyga patikimesnė
    if re.search(r"\b(nba|euroleague|eurocup|lkl|wnba|ncaab)\b", lg):
        return "basketball"
    if sraw in NOT_LS or re.search(r"\b(nfl|ncaaf|ufc)\b", lg):
        return False
    return sraw


ALL = ("soccer", "basketball", "tennis", "hockey")
DONE = {"FT", "AET", "AP", "AOT", "AFTER OT", "AFTER PEN.", "FINISHED"}
VOID = {"CANC.", "CANC", "POSTP.", "POSTP", "ABD.", "ABAN.", "ABANDONED", "CANCELLED", "POSTPONED"}
RETIRED = {"RET.", "RET", "WO", "W.O.", "AW.", "AWARDED"}
STOP = {"fc", "fk", "sc", "cf", "afc", "club", "the", "cd", "ac", "sk", "bk", "if", "de", "of", "sv", "kk", "bc",
        "united", "city", "real", "sporting", "athletic", "atletico", "rc", "as", "ss", "ssc", "us", "vs",
        "basket", "basketball", "team", "and"}
ALIAS = {"psg": "paris saint germain", "man utd": "manchester united", "man city": "manchester city",
         "spurs": "tottenham", "barca": "barcelona", "inter": "inter milan", "juve": "juventus",
         "bayern": "bayern munich", "bvb": "dortmund", "zalgiris": "zalgiris kaunas", "okc": "oklahoma city thunder", "lal": "los angeles lakers", "rytas": "rytas vilnius",
         "turkey": "turkiye", "czech republic": "czechia", "czech rep": "czechia", "usa": "united states",
         "south korea": "korea republic", "korea": "korea republic", "ivory coast": "cote d ivoire",
         "bosnia": "bosnia and herzegovina", "bosnia herzegovina": "bosnia and herzegovina",
         "holland": "netherlands", "republic of ireland": "ireland", "dr congo": "congo dr",
         "pao": "panathinaikos", "pana": "panathinaikos", "oly": "olympiacos", "olympiakos": "olympiacos",
         "efes": "anadolu efes", "fener": "fenerbahce", "fenerbahce beko": "fenerbahce", "gala": "galatasaray",
         "real": "real madrid", "rm": "real madrid", "atleti": "atletico madrid", "milan": "ac milan",
         "virtus": "virtus bologna", "partizan": "partizan belgrade", "crvena zvezda": "red star belgrade",
         "zvezda": "red star belgrade", "kzs": "zalgiris kaunas", "lietkabelis": "lietkabelis panevezys",
         "wolves": "wolverhampton", "nottm forest": "nottingham forest", "sheff utd": "sheffield united",
         "athletic": "athletic bilbao", "betis": "real betis", "sociedad": "real sociedad", "gladbach": "monchengladbach",
         "leverkusen": "bayer leverkusen", "napoli": "ssc napoli", "roma": "as roma", "lazio": "ss lazio",
         "sporting": "sporting cp", "benfica": "sl benfica", "porto": "fc porto", "ajax": "ajax amsterdam",
         "psv": "psv eindhoven", "celtic": "celtic glasgow", "rangers": "glasgow rangers",
         "uae": "united arab emirates", "north macedonia": "macedonia", "kiorfez": "korfez", "korfez basket": "korfez"}
_cache = {}
WEB = None          # Raspberry priskiria: paieška internete (Google), kai kiti šaltiniai neturi


_SPECIAL = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
                          "ı": "i", "þ": "th", "Þ": "Th", "œ": "oe", "Œ": "OE", "ð": "d"})


def _fold(s):
    s = unicodedata.normalize("NFKD", str(s or "").translate(_SPECIAL)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _toks(name):
    n = _fold(ALIAS.get(_fold(name), name))
    t = [w for w in n.split() if len(w) >= 3 and w not in STOP]
    return t or n.split()


def _skel(t):
    """Transliteracijos „skeletas“: Orobah ≈ Oruba ≈ Orubah (balsės ir galinė h nesvarbios)."""
    t = re.sub(r"([aeiouy])h$", r"\1", t)
    return t[:1] + re.sub(r"[aeiouyh]", "", t[1:])


def _tok_like(x, y):
    if x == y:
        return True
    if min(len(x), len(y)) < 4 or x[0] != y[0]:
        return False
    if difflib.SequenceMatcher(None, x, y).ratio() >= 0.8:     # Adalh ≈ Adalah (rašybos klaida)
        return True
    sx, sy = _skel(x), _skel(y)
    return len(sx) >= 3 and sx == sy


def fuzzy_same(sig_name, ev_name):
    """Atlaidus palyginimas (rašybos klaidos, arabiškų pavadinimų transliteracija). Naudojamas TIK kai sutampa
    abi komandos: bent vienas žodis panašus ir abiejose pusėse nelieka savų nesutampančių žodžių."""
    a, b = _ctoks(sig_name), _ctoks(ev_name)
    if not a or not b:
        return False
    ua = [t for t in a if not any(_tok_like(t, u) for u in b)]
    ub = [u for u in b if not any(_tok_like(t, u) for t in a)]
    return len(ua) < len(a) and not (ua and ub)


def same(sig_name, ev_name):
    """Ar signalo komanda/žaidėjas atitinka LiveScore pavadinimą."""
    if not sig_name:
        return False
    hay = " " + _fold(ev_name) + " "
    toks = _toks(sig_name)
    if any(f" {t} " in hay for t in toks if len(t) >= 4):
        return True
    full = _fold(sig_name)
    return bool(full) and (f" {full} " in hay or f" {_fold(ev_name)} " in f" {full} ")


GENERIC = STOP - {"united", "city", "real", "sporting", "athletic", "atletico"} | {"basketbol", "basquet", "women"}


def _ctoks(name):
    n = _fold(ALIAS.get(_fold(name), name))
    return [w for w in n.split() if len(w) >= 4 and w not in GENERIC]


def _pm(t, lst):
    return any(t[:4] == x[:4] for x in lst)


def marks(name):
    """Jaunimo / rezervų žymės: „U19“, „U-21“, „Res“, „II“, „B“, „Reserves“ – pagrindinė komanda ≠ jaunimas ≠ rezervai."""
    t = f" {_fold(name)} "
    m = {"u" + x for x in re.findall(r" (?:u|sub|under) ?(\d{2}) ", t)}
    if re.search(r" (?:youth|juniors?|jaunimas|primavera|academy) ", t):
        m.add("youth")
    if re.search(r" (?:res|reserves?|reserva|ii|b|2|jong) ", t):
        m.add("res")
    return frozenset(m)


def conflict(sig_name, ev_name):
    """Abu pavadinimai turi savų, nesutampančių žodžių (Slovan ≠ Inter Bratislava, United ≠ City) – ne ta komanda.
    Taip pat: skirtinga amžiaus grupė ar rezervai (Colchester U21 ≠ Colchester United)."""
    if sig_name and ev_name and marks(sig_name) != marks(ev_name):
        return True
    a, b = _ctoks(sig_name), _ctoks(ev_name)
    return any(not _pm(t, b) for t in a) and any(not _pm(t, a) for t in b)


def mscore(sig_name, ev_name):
    """Kiek signalo pavadinimo žodžių randama LiveScore pavadinime (Hapoel Tel Aviv – Hapoel, ne Maccabi)."""
    if not sig_name or not ev_name:
        return 0
    a, b = _ctoks(sig_name), _ctoks(ev_name)
    return sum(1 for t in a if _pm(t, b)) - (2 if conflict(sig_name, ev_name) else 0)


def _tz():
    off = datetime.now().astimezone().utcoffset() or timedelta(0)
    return int(off.total_seconds() // 3600)


def day(sport, d):
    key = (sport, d)
    hit = _cache.get(key)
    old = d < (datetime.now() - timedelta(days=2)).date() if hasattr(d, "year") else False
    if hit and time.time() - hit[0] < (6 * 3600 if old else 300):   # senų dienų rezultatai nesikeičia
        return hit[1]
    url = HOST.format(sport=sport, day=f"{d:%Y%m%d}", tz=_tz())
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))
    evs = []
    for st in data.get("Stages", []):
        for e in st.get("Events", []):
            try:
                e = dict(e)
                e["_t1"] = (e.get("T1") or [{}])[0].get("Nm", "")
                e["_t2"] = (e.get("T2") or [{}])[0].get("Nm", "")
                e["_league"] = f"{st.get('Cnm', '')}: {st.get('Snm', '')}".strip(": ")
                e["_start"] = datetime.strptime(str(e.get("Esd")), "%Y%m%d%H%M%S")
                e["_sport"] = sport
                evs.append(e)
            except Exception:
                pass
    _cache[key] = (time.time(), evs)
    while len(_cache) > 30:                       # Raspberry atmintis – laikom tik 30 dienų×sporto sąrašų
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
    return evs


def _penalty(b, e):
    """Moterų / jaunimo / dublerių komandos – tik jei signale taip ir parašyta."""
    sig = _fold(f"{b.get('home')} {b.get('away')} {b.get('league') or ''}")
    ev = _fold(f"{e['_t1']} {e['_t2']} {e['_league']}")
    p = 0
    for w in (" w ", "women", "u19", "u20", "u21", "u23", " ii ", " b ", "reserves", "youth", "doubles"):
        if w in f" {ev} " and w not in f" {sig} ":
            p += 6
    if "/" in e["_t1"] and "/" not in (b.get("home") or ""):     # tenisas – dvejetai
        p += 6
    return p


def find(b):
    """Grąžina LiveScore rungtynes, kurios atitinka statymą, arba None.
    Pirma ieškom ±30 val. nuo paskelbimo; neradus – iki 7 dienų į priekį (statymas gali būti iš anksto)."""
    e = _find(b)
    if (e is None or e.get("_ended")) and b.get("home"):
        e2 = _find(b, ahead_h=7 * 24)                   # ir kai rasta tik jau pasibaigusi (pvz. praeitos nakties) –
        if e2 is not None and not e2.get("_ended"):     # gal statymas būsimoms rungtynėms
            e = e2
    if e is None and b.get("home") and b.get("away"):
        e = _one_team(b)                                 # varžovas pavadintas kitaip (pvz. „Körfez“ ↔ „Manisa BB“)
    return e


def _one_team(b):
    """Viena komanda sutampa, kita – ne: imam tik jei tai VIENINTELĖS tos komandos rungtynės ±30 val. nuo paskelbimo
    ir prasidėjo ne anksčiau kaip 3 val. iki paskelbimo."""
    out = []
    for keep in ("home", "away"):
        nb = dict(b, away=None, home=b.get(keep))
        e = _find(nb)
        if e is None or e.get("_ended"):
            continue
        created = datetime.fromisoformat(b.get("created", b["kickoff"]))
        if e["_start"] < created - timedelta(hours=3):
            continue
        other = e["_t2"] if not e.get("_swapped") else e["_t1"]
        out.append((keep, e, other))
    if len(out) != 1:
        return None
    keep, e, other = out[0]
    sw = e.get("_swapped")
    if keep == "away":                                   # signalo „away“ rasta kaip e._swapped=False → ji namų pusėje
        sw = not sw
    return dict(e, _swapped=sw, _league=f"{e['_league']} · varžovas LiveScore'e: {other}")


def _find(b, ahead_h=30):
    home, away = b.get("home") or "", b.get("away") or ""
    if not home:
        return None
    ref = datetime.fromisoformat(b["kickoff"])
    created = datetime.fromisoformat(b.get("created", b["kickoff"]))
    sraw = ls_sport(b)
    if sraw is False:
        return None                                     # LiveScore šios sporto šakos neturi – kitaip „rastų“ ne tas rungtynes
    sp = SPORT_OF.get(sraw)
    if not sp and (b.get("league") or "").upper() in ("NBA", "EUROLEAGUE", "LKL"):
        sp = "basketball"
    sports = [sp] + [s for s in ALL if s != sp] if sp else list(ALL)
    if ahead_h > 30:
        if not sp:
            return None
        sports = [sp]                                    # tolimesnių dienų – tik savo sporto šaka
    best = None
    for sport in sports:
        first, last = (min(ref, created) - timedelta(hours=30)).date(), (created + timedelta(hours=ahead_h)).date()
        days = sorted({first + timedelta(days=i) for i in range((last - first).days + 1)},
                      key=lambda d: abs((d - ref.date()).days))
        for d in days:
            try:
                evs = day(sport, d)
            except Exception:
                continue
            for e in evs:
                a1, a2 = same(home, e["_t1"]), same(home, e["_t2"])
                fz = False
                if not (a1 or a2) and away:              # rašybos klaida / kita transliteracija (Al Adalh ↔ Al-Adalah)
                    if fuzzy_same(home, e["_t1"]) and fuzzy_same(away, e["_t2"]):
                        a1 = fz = True
                    elif fuzzy_same(home, e["_t2"]) and fuzzy_same(away, e["_t1"]):
                        a2 = fz = True
                if not (a1 or a2):
                    continue
                if away and not fz:
                    if not ((a1 and same(away, e["_t2"])) or (a2 and same(away, e["_t1"]))):
                        continue
                elif a1 and a2:
                    continue
                s11 = mscore(home, e["_t1"]) + mscore(away, e["_t2"])
                s12 = mscore(home, e["_t2"]) + mscore(away, e["_t1"])
                sw_ = a2 if fz else s12 > s11
                t_h, t_a = (e["_t2"], e["_t1"]) if sw_ else (e["_t1"], e["_t2"])
                if not fz and (conflict(home, t_h) or (away and conflict(away, t_a))):
                    continue                             # tik miesto ar bendras žodis sutapo – kitos komandos
                ev_w = bool(re.search(r"(?i)(\bW\b|women|female|ladies|\(w\))", f"{e['_t1']} {e['_t2']} {e['_league']}"))
                if b.get("women") is True and not ev_w or b.get("women") is False and ev_w:
                    continue                             # vyrų statymas ≠ moterų rungtynės (ir atvirkščiai)
                # ieškom ir anksčiau prasidėjusių – kad pagautume „prognozes“, paskelbtas jau po rungtynių
                if not (created - timedelta(hours=30) <= e["_start"] <= created + timedelta(hours=ahead_h)):
                    continue
                score = abs((e["_start"] - ref).total_seconds()) / 3600 + _penalty(b, e) + \
                    (0 if sport == sports[0] else 2) + (0 if away else 1) + (1 if fz else 0)
                if e["_start"] + timedelta(hours=_DUR.get(sport, 3.0)) < created:
                    score += 30                          # jau pasibaigusios iki paskelbimo – renkamės kitas, jei yra
                if best is None or score < best[0]:
                    best = (score, e, sw_)
        if best and best[0] < 6:
            break
    if not best:
        return None
    e, swapped = best[1], best[2]
    ended = e["_start"] + timedelta(hours=_DUR.get(e.get("_sport"), 3.0)) < created
    return dict(e, _swapped=swapped, _ended=ended)


# ---------------------------------------------------------------- rezultato skaičiavimas
def _i(x):
    try:
        return int(str(x).strip())
    except Exception:
        return None


def _scores(e, key1, key2):
    a, b = _i(e.get(key1)), _i(e.get(key2))
    return (a, b) if a is not None and b is not None else None


def state(e):
    s = str(e.get("Eps") or "").upper()
    if s in DONE:
        return "done"
    if s in VOID:
        return "void"
    if s in RETIRED:
        return "retired"
    if s in ("NS", "", "TBA", "DEL."):
        return "ns"
    return "live"


def _period_text(b):
    t = _fold(f"{b.get('pick')} {b.get('market') or ''}")
    sp = _fold(b.get("sport"))
    m = re.search(r"\b([1-4])(st|nd|rd|th)? ?(period\w*|kelin\w*)\b|\b(pirm|antr|trec|ketvirt)\w* (period\w*|kelin\w*)\b", t)
    if m and (sp in ("hockey", "basketball") or re.search(r"period", t)):
        n = m.group(1) or {"pirm": "1", "antr": "2", "trec": "3", "ketvirt": "4"}[m.group(4)]
        return f"q{n}"                                  # ledo ritulio periodas / krepšinio kėlinys (ketvirtis)
    if re.search(r"\b(1st half|first half|1h|1 half|half ?time|ht|pirm\w* kelin\w*|1 kelin\w*|i kelin\w*|"
                 r"pirm\w* puse\w*|1 puse\w*)\b", t):
        return "h1"
    if re.search(r"\b(2nd half|second half|2h|antr\w* kelin\w*|2 kelin\w*|ii kelin\w*|antr\w* puse\w*)\b", t):
        return "h2"
    m = re.search(r"\b([1-4])(st|nd|rd|th)? (quarter|q|kelinys|ketvirtis)\b|\bq([1-4])\b", t)
    if m:
        return f"q{m.group(1) or m.group(4)}"
    m = re.search(r"\b([1-5])(st|nd|rd|th)? set\b", t)
    if m:
        return f"s{m.group(1)}"
    return "ft"


def score_for(e, period):
    """(namų, svečių) taškai pagal periodą; futbolas – be pratęsimo, kiti – su pratęsimu."""
    sp = e["_sport"]
    if period == "ft":
        if sp == "soccer":
            return _scores(e, "Tr1OR", "Tr2OR") or _scores(e, "Tr1", "Tr2")
        return _scores(e, "Tr1", "Tr2")
    if sp == "soccer":
        if period == "h1":
            return _scores(e, "Trh1", "Trh2")
        if period == "h2":
            ft, h1 = score_for(e, "ft"), _scores(e, "Trh1", "Trh2")
            return (ft[0] - h1[0], ft[1] - h1[1]) if ft and h1 else None
    if sp == "basketball":
        q = {n: _scores(e, f"Tr1Q{n}", f"Tr2Q{n}") for n in range(1, 5)}
        if period == "h1" and q[1] and q[2]:
            return q[1][0] + q[2][0], q[1][1] + q[2][1]
        if period == "h2" and q[3] and q[4]:
            return q[3][0] + q[4][0], q[3][1] + q[4][1]
        if period.startswith("q"):
            return q.get(int(period[1]))
    if sp == "tennis" and period.startswith("s"):
        return _scores(e, f"Tr1S{period[1]}", f"Tr2S{period[1]}")
    if sp == "hockey" and period.startswith("q"):
        return _scores(e, f"Tr1Pe{period[1]}", f"Tr2Pe{period[1]}") or _scores(e, f"Tr1P{period[1]}", f"Tr2P{period[1]}")
    return None


def _tennis_sets(e):
    sets = []
    for n in range(1, 6):
        s = _scores(e, f"Tr1S{n}", f"Tr2S{n}")
        if s:
            sets.append(s)
    return sets


PLAYER_RE = re.compile(r"\b(assist\w*|rebound\w*|3 ?pt\w*|3 ?pointer\w*|three\w*|tritask\w*|atkovot\w*|perdavim\w*|"
                       r"rezultatyv\w*|blocks?|steals?|pra|player|zaidej\w*|taitykl\w*|shots? on|saves?|"
                       r"to score|pelnys|ivarti mus)\b")


BTTS_RE = re.compile(r"\bbtts\b|both teams? (to )?score|both to score|abi komandos (pelnys|imus|ives)|abi imus|\bgg\b|\bng\b")


NOTLOSE_RE = re.compile(r"nepralaim|not lose|won'?t lose|wont lose|nepralosi|draw or win|win or draw")


def team_subject(b):
    sj = str(b.get("subject") or "")
    toks = _fold(sj).split()
    if sj and any(_fold(b.get(k) or "") in toks or (_fold(b.get(k) or "") and _fold(b.get(k) or "") == _fold(sj))
                  for k in ("home", "away")):
        return True                                     # „PAO vs Barca total“ – trumpi pavadinimai
    return bool(sj) and (same(sj, b.get("home")) or same(sj, b.get("away")) or
                         bool(re.search(r"\bvs?\b|–|-", sj)) and (same(b.get("home"), sj) or same(b.get("away"), sj)))


def evaluate(b, e):
    """Grąžina (status, detail) arba (None, priežastis), jei šio statymo pagal LiveScore įvertinti negalima."""
    st = state(e)
    name = f"{e['_t1']} – {e['_t2']}"
    if st == "void":
        return "push", f"rungtynės atšauktos/atidėtos ({e.get('Eps')}) – statymas grąžinamas"
    if st == "retired":
        return None, f"{name}: rungtynės nebaigtos ({e.get('Eps')})"
    if st != "done":
        return None, f"{name}: dar vyksta arba neprasidėjo ({e.get('Eps') or 'NS'})"
    side, line = b.get("side"), b.get("line")
    try:
        line = float(line) if line not in (None, "") else None
    except Exception:
        line = None
    sw = e.get("_swapped")
    sp = e["_sport"]
    period = _period_text(b)
    sc = score_for(e, period)
    if not sc:
        return None, f"{name}: nėra {period} rezultato"
    hs, as_ = (sc[1], sc[0]) if sw else sc                 # hs – signalo „home“ komandos taškai
    pname = {"ft": "", "h1": " (1 kėlinys)", "h2": " (2 kėlinys)"}.get(period, f" ({period})")
    shown = f"{e['_t1']} {sc[0]}:{sc[1]} {e['_t2']}{pname}"
    extra = ""
    if sp == "soccer" and period == "ft" and str(e.get("Eps")).upper() in ("AET", "AP"):
        extra = f" (po pratęsimo {e.get('Tr1')}:{e.get('Tr2')} – statymas skaičiuojamas pagal 90 min.)"
    pick = _fold(b.get("pick"))
    team = b.get("team") or ""
    mw = re.search(r"(?<![A-Za-z0-9])[WP]\s?([12])(?![A-Za-z0-9])", str(b.get("pick") or ""))
    if side == "Win" and mw:                               # W1 = namų, W2 = svečių komanda
        team = (b.get("home") if mw.group(1) == "1" else b.get("away")) or team
    if b.get("subject") and team_subject(b):
        b = dict(b, subject=None)                      # „žaidėjas“ iš tikro komanda (pvz. „PAO vs Barca“)
    if BTTS_RE.search(pick):
        side = "BTTS"
        team = "no" if re.search(r"\b(no|ne)\b", pick) else "yes"
    elif b.get("subject") or PLAYER_RE.search(pick + " " + _fold(b.get("market"))):
        return None, "žaidėjo statistika – LiveScore neturi"
    raw = re.sub(r"[^a-z0-9&+.,]+", " ", unicodedata.normalize("NFKD", str(b.get("pick") or "")).encode("ascii", "ignore")
                 .decode().lower()).strip()
    mc = re.search(r"^(.*?)\s(?:&|and|ir|\+)\s.*?\b(over|under|daugiau|maziau)\s*(\d+(?:[.,]\d+)?)", raw)
    if mc and side not in ("BTTS",) and not re.search(r"\bbtts|abi|both", pick):
        tm = mc.group(1).strip()                       # „Edmonton & Over 5.5“ – pergalė IR totalas
        if same(tm, b.get("home")) or same(tm, b.get("away")):
            mh = mscore(tm, b.get("home")) >= mscore(tm, b.get("away"))
            if sp in ("hockey", "basketball") and period == "ft" and _i(e.get("Ewt")) in (1, 2):
                won = ((_i(e.get("Ewt")) == 1) != bool(sw)) == mh
            else:
                won = (hs > as_) if mh else (as_ > hs)
            ln = float(mc.group(3).replace(",", "."))
            tot = hs + as_
            over = mc.group(2) in ("over", "daugiau")
            ok_t = (tot > ln) if over else (tot < ln)
            return ("won" if won and ok_t else "lost"), \
                f"rezultatas {shown}{extra} → {tm} {'laimėjo' if won else 'nelaimėjo'}, total {tot} ({'over' if over else 'under'} {ln:g})"
    mine_home = None
    if team and _fold(team) not in ("draw", "x", "lygiosios", "total", "yes", "no", "taip", "ne", "over", "under"):
        sh, sa = mscore(team, b.get("home")), mscore(team, b.get("away"))
        if sh <= 0 and sa <= 0 and not same(team, b.get("home")) and not same(team, b.get("away")):
            if "|" not in str(team) and _ctoks(team) and side != "DC":                     # komanda nesutampa su nė viena – tai žaidėjas
                return None, f"„{team}“ – ne rungtynių komanda (žaidėjo statymas?)"
        else:
            mine_home = sh >= sa if sh != sa else not (same(team, b.get("away")) and not same(team, b.get("home")))
    elif side in ("Win", "Spread") and b.get("away"):          # komandos nėra – žiūrim, kuri minima statyme
        if same(b["away"], b.get("pick") or "") and not same(b.get("home") or "", b.get("pick") or ""):
            mine_home = False
    if re.search(r"\bdnb\b|draw no bet|lygiosios\W{0,3}grazin|be lygiuju|lygiuju atveju grazin", pick) and \
            side not in ("Over", "Under", "BTTS", "DC"):
        side, line = "Spread", 0.0                         # DNB = fora 0: lygiosios – statymas grąžinamas
    if side in ("Win", "DC") and NOTLOSE_RE.search(pick) and mine_home is not None:
        ok = (hs >= as_) if mine_home else (as_ >= hs)      # „nepralaimės“ = pergalė arba lygiosios (dviguba baigtis)
        return ("won" if ok else "lost"), f"rezultatas {shown}{extra} → nepralaimėjo: {'taip' if ok else 'ne'}"
    nil = re.search(r"win\w* to nil|laimes? sausai|laimes? be praleist|pergale sausai", pick)
    if nil and mine_home is None and b.get("home"):        # komanda nenurodyta atskirai – ieškom statymo tekste
        if same(b.get("home"), pick) and not same(b.get("away") or "", pick):
            mine_home = True
        elif b.get("away") and same(b["away"], pick) and not same(b["home"], pick):
            mine_home = False
    if nil and mine_home is not None:
        mine, oth = (hs, as_) if mine_home else (as_, hs)
        hit = mine > oth and oth == 0
        if re.search(r"(\bno\b|\bne\b)\s*$", pick):
            hit = not hit
        return ("won" if hit else "lost"), f"rezultatas {shown}{extra} → pergalė „sausai“: {'taip' if mine > oth and oth == 0 else 'ne'}"
    sets = _tennis_sets(e) if sp == "tennis" else []
    tennis_unit = sp == "tennis" and period == "ft"

    if side == "Win":
        if _fold(team) in ("draw", "x", "lygiosios"):
            res = "won" if hs == as_ else "lost"
        else:
            if mine_home is None:
                if b.get("home") and same(b["home"], b.get("pick") or "") and not same(b.get("away") or "", b.get("pick") or ""):
                    mine_home = True
                else:
                    return None, f"neaišku, už kurią komandą statyta („{team or b.get('pick')}“)"
            if tennis_unit or (sp in ("hockey", "basketball") and period == "ft"):
                w = _i(e.get("Ewt"))
                if w in (1, 2):
                    home_won = (w == 1) != bool(sw)
                    res = "won" if home_won == mine_home else "lost"
                else:
                    res = "won" if (hs > as_) == mine_home and hs != as_ else "lost"
            else:
                res = "won" if (hs > as_ if mine_home else as_ > hs) else "lost"
        return res, f"rezultatas {shown}{extra}"

    if side == "DC":
        ok = False
        for o in [x.strip() for x in str(team).split("|") if x.strip()]:
            if _fold(o) in ("draw", "x", "lygiosios"):
                ok = ok or hs == as_
            elif same(o, b.get("home")):
                ok = ok or hs > as_
            else:
                ok = ok or as_ > hs
        return ("won" if ok else "lost"), f"rezultatas {shown}{extra}"

    if side == "BTTS":
        both = hs > 0 and as_ > 0
        yes = _fold(team or "yes") in ("yes", "taip", "") or "no" not in pick.split()
        return ("won" if both == yes else "lost"), f"rezultatas {shown}{extra}"

    if side == "Spread" and line is not None:
        if mine_home is None:
            return None, "neaišku, kuriai komandai fora"
        if tennis_unit and re.search(r"\bset", pick):
            mine, other = (_i(e.get("Tr1")), _i(e.get("Tr2")))
            if sw:
                mine, other = other, mine
            if not mine_home:
                mine, other = other, mine
            unit = "setai"
        elif tennis_unit:                                    # fora geimams
            g1, g2 = sum(s[0] for s in sets), sum(s[1] for s in sets)
            if sw:
                g1, g2 = g2, g1
            mine, other = (g1, g2) if mine_home else (g2, g1)
            unit = "geimai"
        else:
            mine, other = (hs, as_) if mine_home else (as_, hs)
            unit = ""
        diff = mine + line - other
        res = "won" if diff > 0 else "lost" if diff < 0 else "push"
        return res, f"rezultatas {shown}{extra} → {team} {line:+g} = {mine + line:g}:{other} {unit}".strip()

    if side in ("Over", "Under") and line is not None:
        if b.get("subject"):
            return None, "žaidėjo statistika – LiveScore neturi"
        if re.search(r"corner|kampin|card|kortel|booking|shot|smug|foul|offside", pick + " " + _fold(b.get("market"))):
            return None, "kampinių/kortelių statistikos LiveScore neturi"
        if sp == "tennis" and period == "ft":
            if re.search(r"\bset", pick + " " + _fold(b.get("market"))):
                value, label = len(sets), "setų"
            else:
                value, label = sum(s[0] + s[1] for s in sets), "geimų"
        else:
            tm = None                                         # komandos totalas: „Team over 1.5“
            if team and _fold(team) not in ("", "total") and same(team, b.get("pick")):
                tm = mine_home if mine_home is not None else None
            if tm is None and sp == "basketball" and period == "ft" and 50 <= line < 110:
                if b.get("team") and mine_home is not None:
                    tm = mine_home                      # vienos komandos totalas
                elif not b.get("away"):
                    tm = True                           # nurodyta tik viena komanda – jos taškai
                    team = b.get("home")
            if sp == "basketball" and period == "ft" and line < (50 if tm is not None else 110):
                return None, f"linija {line:g} per maža krepšinio rezultatui – žaidėjo statymas"
            if tm is None:
                value = hs + as_
                label = "įvarčių" if sp in ("soccer", "hockey") else "taškų"
            else:
                value = hs if tm else as_
                label = f"{team} " + ("įvarčių" if sp in ("soccer", "hockey") else "taškų")
        res = "push" if value == line else ("won" if (value > line) == (side == "Over") else "lost")
        return res, f"{label}: {value:g} (linija {line:g}), {shown}{extra}"

    return None, "šios rinkos LiveScore neįvertina"


LATE = "paskelbta jau pasibaigus rungtynėms – ne prognozė, neįskaitoma"
_DUR = {"soccer": 2.25, "basketball": 2.75, "hockey": 2.75}   # kiek vidutiniškai trunka rungtynės (val.)


def late(b, e):
    """Pranešimas, jei statymas paskelbtas JAU PASIBAIGUS rungtynėms (LIVE statymai rungtynių metu – tinka).
    LiveScore laikas tikslus (mūsų laiko juosta); kitų šaltinių laiko juosta gali skirtis – jiems +2 val. atsargos."""
    try:
        created = datetime.fromisoformat(str(b.get("created") or b.get("kickoff"))[:16])
        st = e.get("_start")
        if st is None:
            return None
        dur = _DUR.get(str(e.get("_sport") or ls_sport(b) or "soccer"), 4.0)
        exact = "Esd" in e
        if created <= st + timedelta(hours=dur + (0 if exact else 2)):
            return None
        src = "LiveScore" if exact else str(e.get("_league") or "kitas šaltinis").split(",")[0][:30]
        return (f"paskelbta {created:%m-%d %H:%M}, o rungtynės prasidėjo {st:%m-%d %H:%M} ({src}) – "
                "jau pasibaigus, ne prognozė, neįskaitoma")
    except Exception:
        return None


def settle(b):
    """(status, detail, info) – status None, jei nepavyko (info paaiškina kodėl)."""
    res = _settle(b)
    try:                                            # LIVE statymai įskaitomi – tik pažymim
        if res[0] and b.get("created") and b.get("kickoff"):
            c = datetime.fromisoformat(str(b["created"])[:16])
            k = datetime.fromisoformat(str(b["kickoff"])[:16])
            if c > k + timedelta(minutes=5):
                res = (res[0], f"{res[1]} · LIVE statymas", res[2])
    except Exception:
        pass
    return res


SOURCES = [("ESPN/EuroLeague", "alt_event"), ("NHL.com", "nhl_event"), ("ESPN MMA", "mma_event"), ("UTMA (77.lt)", "utma_event"), ("UniqueTMA", "uniquetma_event"), ("API-Sports", "apisports_event"), ("SportScore", "sportscore_event"),
           ("FotMob", "fotmob_event"),
           ("365Scores", "s365_event"), ("SofaScore", "sofa_event")]


def alt_sources(b, tried):
    """Visi atsarginiai šaltiniai iš eilės; tried – kas bandyta ir kuo baigėsi (rodoma programėlėje)."""
    try:
        import props
    except Exception:
        return None
    src = SOURCES
    try:
        if props.is_esoccer(b):                           # e-futbolas – ne tikros rungtynės (Bayern ≠ „FC Bayern (Dzen)“)
            src = [("e-futbolas (bettrails)", "esoccer_event")]
    except Exception:
        pass
    for name, fn in src:
        f = getattr(props, fn, None)
        if not f:
            continue
        try:
            e = f(b)
        except Exception:
            tried.append(f"{name}: klaida")
            continue
        if e:
            tried.append(f"{name} ✓")
            return e
        why = ""
        if name == "API-Sports":
            last = str(getattr(props, "APIS_STATE", {}).get("last", ""))
            why = " (nemokamas planas – tik vakar–rytoj)" if "Free plans" in last else \
                " (nėra rakto)" if not (getattr(props, "CFG", {}) or {}).get("apisports_key") else ""
        elif name == "SofaScore" and "403" in str(getattr(props, "SOFA_STATE", {}).get("last", "")):
            why = " (neleidžia, 403)"
        tried.append(f"{name} ✗{why}")
    if WEB and src is SOURCES:                            # e-futbolui internetas grąžintų tikrų rungtynių rezultatą
        try:
            e = WEB(b)
        except Exception:
            e = None
        tried.append("paieška internete " + ("✓" if e else "✗"))
        return e
    return None


def _settle(b):
    e = find(b)
    if not e:
        tried = ["LiveScore ✗"]
        e2 = alt_sources(b, tried)
        if not e2:
            return None, None, "nerasta: " + ", ".join(tried)
        lt = late(b, e2)
        if lt:
            return None, None, lt
        if state(e2) != "done" and state(e2) != "void":
            st0 = e2.get("_start")
            if isinstance(st0, datetime) and st0 > datetime.now():
                return None, None, f"numatyta {st0:%m-%d %H:%M} – laukiama ({e2.get('_league')})"
            return None, None, f"{e2.get('_league') or 'internete'}: dar nesibaigė ar nevyko ({e2.get('Eps')})"
        res, detail = evaluate(b, e2)
        if res is None:
            return None, None, detail
        return res, f"{detail} ({e2['_league']})", "ok"
    lt = late(b, e)
    if lt:
        return None, None, lt
    if state(e) in ("ns", "live") and e.get("_start") and e["_start"] < datetime.now() - timedelta(hours=4):
        e2 = alt_sources(b, [])                     # LiveScore „įstrigo“ (pvz. OT, nors seniai baigėsi) – kitas šaltinis
        if e2 and state(e2) in ("done", "void"):
            res, detail = evaluate(b, e2)
            if res is not None:
                return res, f"{detail} ({e2['_league']}; LiveScore neatnaujino)", "ok"
    res, detail = evaluate(b, e)
    if res is None and re.search(r"nėra (q\d|h\d|s\d) rezultato", detail or ""):
        try:                                        # LiveScore neturi kėlinių / periodų – kiti šaltiniai (365Scores ir kt.)
            import props
            for name, fn in SOURCES:
                f = getattr(props, fn, None)
                try:
                    e2 = f(b) if f else None
                except Exception:
                    e2 = None
                if e2 and state(e2) == "done":
                    r2, d2 = evaluate(b, e2)
                    if r2 is not None:
                        return r2, f"{d2} ({e2['_league']})", "ok"
        except Exception:
            pass
    if res is None:
        return None, None, detail
    ok = None
    if res != "push" or "atšauktos" not in detail:
        try:
            import props                            # antras šaltinis: ESPN / EuroLeague
            ok = props.cross_check(b, e)
        except Exception:
            ok = None
    if ok is False and WEB:                         # šaltiniai nesutaria – sprendžia trečias (internetas)
        try:
            w = WEB(b)
        except Exception:
            w = None
        if w and state(w) == "done" and w.get("Tr1") is not None:
            s1, s2 = _scores(e, "Tr1", "Tr2")
            o1, o2 = (w["Tr2"], w["Tr1"]) if (mscore(e["_t1"], w["_t2"]) > mscore(e["_t1"], w["_t1"])) else (w["Tr1"], w["Tr2"])
            if (s1, s2) == (o1, o2):
                ok = True
    if ok is False:
        return None, None, "LiveScore ir ESPN/EuroLeague rezultatai nesutampa (ir internetas nepatvirtino) – tikrinsim vėliau"
    src = "LiveScore + " + ("patvirtinta antro šaltinio" if ok else "vienas šaltinis")
    return res, f"{detail} ({src}, {e['_league']})", "ok"
