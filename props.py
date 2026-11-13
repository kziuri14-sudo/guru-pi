"""Statymai, kurių LiveScore neįvertina: žaidėjų statistika ir futbolo kampiniai/kortelės/smūgiai.

Šaltiniai (vieši, nemokami, be prisijungimo):
  * EuroLeague / EuroCup – oficialus api-live.euroleague.net (kiekvieno žaidėjo taškai, atkovoti, perdavimai...)
  * NBA / WNBA / NCAA – ESPN (site.api.espn.com) rungtynių protokolas
  * futbolo kampiniai, kortelės, smūgiai – ESPN (didžiosios lygos ir taurės)
Jei rungtynių ar žaidėjo rasti nepavyksta, grąžinama None – toks statymas į statistiką neįskaitomas.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import results as R

EL = "https://api-live.euroleague.net"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"
HDR = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
       "Accept": "application/json"}
SOCCER = ["all", "uefa.champions", "uefa.europa", "uefa.europa.conf", "eng.1", "esp.1", "ita.1", "ger.1", "fra.1",
          "ned.1", "por.1", "bel.1", "tur.1", "sco.1", "eng.2", "usa.1", "bra.1", "arg.1", "ger.2", "esp.2", "ita.2",
          "fra.2", "aut.1", "sui.1", "den.1", "nor.1", "swe.1", "gre.1", "uefa.nations", "fifa.worldq.uefa",
          "eng.fa", "esp.copa_del_rey", "ita.coppa_italia", "ger.dfb_pokal", "eng.league_cup", "pol.1", "cze.1"]
_c = {}

CORNER_RE = re.compile(r"corner|kampin")
CARD_RE = re.compile(r"\bcard|kortel|booking")
SHOT_RE = re.compile(r"shots? on (goal|target)|smugi\w* i vartus|\bsot\b")
SHOTS_RE = re.compile(r"\bshots?\b|smugi")
PROP_RE = re.compile(r"assist|rebound|3 ?pt|three|tritask|atkovot|perdavim|rezultatyv|\bblocks?\b|\bsteals?\b|\bpra\b|"
                     r"\bpts\b|point|task|perimt|blokuot")


def _get(url, ttl=1800):
    hit = _c.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode("utf-8"))
    _c[url] = (time.time(), data)
    while len(_c) > 200:
        _c.pop(min(_c, key=lambda k: _c[k][0]), None)
    return data


def _when(leg):
    k = datetime.fromisoformat(str(leg.get("kickoff"))[:16])
    c = datetime.fromisoformat(str(leg.get("created") or leg.get("kickoff"))[:16])
    return k, c


def _team_ok(sig, names):
    names = [n for n in names if n]
    return bool(sig) and any(R.same(sig, n) for n in names) and not all(R.conflict(sig, n) for n in names)


def _pair(leg, n1, n2):
    """(atitinka, apversta) – ar signalo komandos sutampa su rungtynių komandomis."""
    h, a = leg.get("home"), leg.get("away")
    if _team_ok(h, n1) and (not a or _team_ok(a, n2)):
        return True, False
    if _team_ok(h, n2) and (not a or _team_ok(a, n1)):
        return True, True
    return False, False


# ---------------------------------------------------------------- žaidėjai
def _fold(s):
    return R._fold(s)


def _name_hits(text, first, last):
    """Ar tekste minimas žaidėjas (pavardė su galūnėmis: Valančiūno ≈ Valanciunas)."""
    toks = _fold(text).split()
    ok = False
    for s in _fold(last).split():
        if len(s) < 3:
            continue
        k = len(s) if len(s) <= 4 else max(4, len(s) - 2)
        if any(t[:k] == s[:k] and len(t) >= k for t in toks):
            ok = True
    if not ok:
        return 0
    f = _fold(first)
    return 2 if f and any(t == f or (len(t) == 1 and t == f[:1]) or (len(t) >= 3 and f.startswith(t)) for t in toks) else 1


def _stat_keys(text):
    t = _fold(text)
    if "%" in str(text) or re.search(r"\bproc|pataikym|percent", t):
        return None                                   # procentai – neskaičiuojam
    keys = []
    if re.search(r"\bpra\b|points? \+ ?rebounds? \+ ?assists?", t):
        return ["pts", "reb", "ast"]
    thr = r"tritask\w*|3 ?pt\w*|3 ?pointers?|threes?|3 tasku|3pm"
    if re.search(thr, t):
        keys.append("3pm")
        t = re.sub(thr, " ", t)
    for k, rx in (("reb", r"atkovot\w*|rebound\w*|\breb\b"), ("ast", r"perdavim\w*|assist\w*|\bast\b|rezultatyv\w*"),
                  ("stl", r"perimt\w*|steals?|\bstl\b"), ("blk", r"blokuot\w*|blocks?|\bblk\b"),
                  ("pts", r"task\w*|points?|\bpts\b")):
        if re.search(rx, t):
            keys.append(k)
    return keys


def _line(leg, text):
    t = _fold(text)
    side = leg.get("side")
    line = leg.get("line")
    try:
        line = float(line) if line not in (None, "") else None
    except Exception:
        line = None
    m = re.search(r"\bbent (\d+)\b|\b(\d+)\+", t)
    if line is None and m:
        line, side = int(m.group(1) or m.group(2)) - 0.5, "Over"
    elif line is not None and m and float(line).is_integer():
        line, side = line - 0.5, "Over"               # „bent 1“ = 1 ir daugiau (ne lygu – laimėta)
    if side not in ("Over", "Under"):
        side = "Under" if re.search(r"\b(maziau|under)\b", t) else "Over" if re.search(r"\b(daugiau|over|bent)\b", t) else None
    return line, side


def _players_el(stats):
    out = []
    for side in ("local", "road"):
        for p in (stats.get(side) or {}).get("players") or []:
            nm = (p.get("player") or {}).get("person", {}).get("name", "")
            last, _, first = nm.partition(",")
            s = p.get("stats") or {}
            out.append({"first": first.strip(), "last": last.strip(), "played": (s.get("timePlayed") or 0) > 0,
                        "v": {"pts": s.get("points"), "reb": s.get("totalRebounds"), "ast": s.get("assistances"),
                              "stl": s.get("steals"), "blk": s.get("blocksFavour"), "3pm": s.get("fieldGoalsMade3")}})
    return out


def _players_espn(summary):
    out = []
    for team in (summary.get("boxscore") or {}).get("players") or []:
        for grp in team.get("statistics") or []:
            labels = [x.upper() for x in grp.get("labels") or []]
            for a in grp.get("athletes") or []:
                nm = (a.get("athlete") or {}).get("displayName", "")
                parts = [x for x in nm.split() if x.lower().strip(".") not in ("jr", "sr", "ii", "iii", "iv")]
                st = a.get("stats") or []
                v = {}
                for k, lab in (("pts", "PTS"), ("reb", "REB"), ("ast", "AST"), ("stl", "STL"), ("blk", "BLK"),
                               ("3pm", "3PT")):
                    if lab in labels and len(st) > labels.index(lab):
                        raw = str(st[labels.index(lab)]).split("-")[0]
                        v[k] = float(raw) if raw.replace(".", "").isdigit() else None
                out.append({"first": " ".join(parts[:-1]), "last": parts[-1] if parts else "",
                            "played": bool(st) and not a.get("didNotPlay"), "v": v})
    return out


def _el_game(leg):
    k, c = _when(leg)
    lg = _fold(leg.get("league"))
    comps = ["U"] if "eurocup" in lg or "europos taur" in lg else ["E"] if re.search(r"euroleag|eurolyg", lg) \
        else ["E", "U"]
    y = k.year if k.month >= 7 else k.year - 1
    best = None
    for comp in comps:
        try:
            data = _get(f"{EL}/v2/competitions/{comp}/seasons/{comp}{y}/games", ttl=1800)
        except Exception:
            continue
        for g in data.get("data") or []:
            try:
                start = datetime.fromisoformat(str(g.get("date"))[:16])
            except Exception:
                continue
            if not (c - timedelta(hours=30) <= start <= c + timedelta(hours=36)):
                continue
            n1 = [(g["local"]["club"].get(x)) for x in ("name", "abbreviatedName", "editorialName")]
            n2 = [(g["road"]["club"].get(x)) for x in ("name", "abbreviatedName", "editorialName")]
            ok, _ = _pair(leg, n1, n2)
            if ok:
                gap = abs((start - k).total_seconds())
                if best is None or gap < best[0]:
                    best = (gap, (comp, y, g))
    return best[1] if best else None


def _espn_game(leg, paths):
    """Tinkamiausios ESPN rungtynės – iš kelių dienų imamos tos, kurių pradžia arčiausiai signalo laiko."""
    k, c = _when(leg)
    for path in paths:
        best = None
        for d in (k.date(), k.date() - timedelta(days=1), k.date() + timedelta(days=1)):
            extra = "&groups=50" if "college" in path else "&limit=1000" if path == "soccer/all" else ""
            old = d < (datetime.now() - timedelta(days=2)).date()
            try:
                data = _get(f"{ESPN}/{path}/scoreboard?dates={d:%Y%m%d}" + (extra or "&limit=400"),
                            ttl=86400 if old else 900)
            except Exception:
                continue
            for ev in data.get("events") or []:
                comps = ((ev.get("competitions") or [{}])[0]).get("competitors") or []
                if len(comps) != 2:
                    continue
                names = [[(cp.get("team") or {}).get(x) for x in ("displayName", "shortDisplayName", "name", "location",
                                                                   "abbreviation")] for cp in comps]
                ok, _ = _pair(leg, names[0], names[1])
                if not ok:
                    continue
                try:
                    st = datetime.fromisoformat(str(ev.get("date")).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                except Exception:
                    st = k
                if not (c - timedelta(hours=30) <= st <= c + timedelta(hours=36)):
                    continue                              # kitos dienos tų pačių komandų rungtynės
                gap = abs((st - k).total_seconds())
                if best is None or gap < best[0]:
                    best = (gap, ev)
        if best:
            return path, best[1]
    return None, None


def _player_bet(leg):
    text = " ".join(str(leg.get(x) or "") for x in ("subject", "pick", "team"))
    t = _fold(text)
    pk = _fold(leg.get("pick"))
    if re.search(r"\b(pergal\w*|laimes|win|winner)\b", pk) and re.search(r"\b(ir|and)\b", pk):
        return None, None, "kombinuotas (pergalė + žaidėjas) – neskaičiuojam"
    keys = _stat_keys(leg.get("pick") or "")
    line, side = _line(leg, leg.get("pick") or "")
    if keys is None:
        return None, None, "procentinė statistika – neskaičiuojam"
    if line is None or side is None:
        return None, None, "nėra linijos"
    if not keys:
        if line < 7.5:
            return None, None, "neaišku, kokia statistika"
        keys = ["pts"]
    sport = _fold(leg.get("sport"))
    lg = _fold(leg.get("league"))
    players, src, done = None, "", False
    if sport in ("basketball", "krepsinis", "") or "basket" in lg:
        if not re.search(r"\b(nba|wnba|ncaa)\b", lg):
            g = _el_game(leg)
            if g:
                comp, y, game = g
                done = bool(game.get("played"))
                if not done:
                    return None, None, "rungtynės dar nesibaigė"
                st = _get(f"{EL}/v3/competitions/{comp}/seasons/{comp}{y}/games/{game['gameCode']}/stats", ttl=86400)
                players, src = _players_el(st), ("EuroLeague" if comp == "E" else "EuroCup")
        if players is None:
            path, ev = _espn_game(leg, ["basketball/nba", "basketball/wnba", "basketball/mens-college-basketball"])
            if ev:
                state = ((ev.get("competitions") or [{}])[0].get("status") or {}).get("type", {}).get("state")
                if state != "post":
                    return None, None, "rungtynės dar nesibaigė"
                players, src = _players_espn(_get(f"{ESPN}/{path}/summary?event={ev['id']}", ttl=86400)), \
                    "ESPN " + path.split("/")[1].upper()
    if players is None:
        return None, None, "rungtynių su žaidėjų statistika nerasta"
    hits = [(p, _name_hits(text, p["first"], p["last"])) for p in players]
    hits = [x for x in hits if x[1]]
    if not hits:
        return None, None, "žaidėjas nerastas protokole"
    best = max(h for _, h in hits)
    chosen, seen = [], set()
    for p, h in hits:
        if h == best and (p["first"], p["last"]) not in seen:
            seen.add((p["first"], p["last"]))
            chosen.append(p)
    multi = re.search(r"\bbendr|sumin|combined|\+", t)
    if len(chosen) > 1 and not multi:
        return None, None, "keli žaidėjai su tokia pavarde"
    if any(not p["played"] for p in chosen):
        return "push", f"{chosen[0]['first']} {chosen[0]['last']} nežaidė – statymas grąžinamas ({src})", "ok"
    total = 0.0
    for p in chosen:
        for k in keys:
            v = p["v"].get(k)
            if v is None:
                return None, None, f"nėra {k} statistikos"
            total += float(v)
    lab = "+".join({"pts": "taškai", "reb": "atkovoti", "ast": "perdavimai", "stl": "perimti", "blk": "blokai",
                    "3pm": "tritaškiai"}[k] for k in keys)
    who = ", ".join(f"{p['first'].title()} {p['last'].title()}".strip() for p in chosen)
    res = "push" if total == line else ("won" if (total > line) == (side == "Over") else "lost")
    return res, f"{who}: {lab} {total:g} ({'daugiau' if side == 'Over' else 'mažiau'} {line:g}) – {src}", "ok"


# ---------------------------------------------------------------- futbolo kampiniai / kortelės / smūgiai
def _soccer_bet(leg):
    t = _fold(f"{leg.get('pick')} {leg.get('market') or ''}")
    if re.search(r"\b(1st|2nd|first|second|pirm\w*|antr\w*) (half|kelin\w*)|\b1h\b|\b2h\b|\bht\b", t):
        return None, None, "kėlinio statistika – neskaičiuojam"
    line, side = _line(leg, leg.get("pick") or "")
    if line is None or side is None:
        return None, None, "nėra linijos"
    path, ev = _espn_game(leg, [f"soccer/{x}" for x in SOCCER])
    if not ev:
        return None, None, "rungtynės ESPN nerastos"
    if ((ev.get("competitions") or [{}])[0].get("status") or {}).get("type", {}).get("state") != "post":
        return None, None, "rungtynės dar nesibaigė"
    su = _get(f"{ESPN}/{path}/summary?event={ev['id']}", ttl=86400)
    teams = (su.get("boxscore") or {}).get("teams") or []
    if len(teams) != 2:
        return None, None, "nėra komandų statistikos"
    team = leg.get("team")
    if team and _fold(team) not in ("total", "over", "under") and R.same(team, leg.get("pick") or ""):
        teams = [x for x in teams if _team_ok(team, [(x.get("team") or {}).get(k) for k in
                                                     ("displayName", "shortDisplayName", "name")])]
        if len(teams) != 1:
            return None, None, f"nerasta {team} statistika"
    if CORNER_RE.search(t):
        keys, lab = ["woncorners"], "kampinių"
    elif CARD_RE.search(t):
        keys, lab = ["yellowcards", "redcards"], "kortelių"
    elif SHOT_RE.search(t):
        keys, lab = ["shotsontarget"], "smūgių į vartus"
    elif SHOTS_RE.search(t):
        keys, lab = ["totalshots"], "smūgių"
    else:
        return None, None, "nežinoma rinka"
    total = 0.0
    for x in teams:
        st = {s.get("name", "").lower(): s.get("displayValue") for s in x.get("statistics") or []}
        for k in keys:
            if k not in st:
                return None, None, f"nėra {lab} statistikos"
            total += float(st[k] or 0)
    who = f"{team} " if len(teams) == 1 else ""
    res = "push" if total == line else ("won" if (total > line) == (side == "Over") else "lost")
    return res, f"{who}{lab}: {total:g} ({'daugiau' if side == 'Over' else 'mažiau'} {line:g}) – ESPN", "ok"


def is_prop(leg):
    t = _fold(f"{leg.get('pick')} {leg.get('market') or ''}")
    if leg.get("subject"):
        return True
    if CORNER_RE.search(t) or CARD_RE.search(t) or SHOTS_RE.search(t):
        return True
    return bool(R.PLAYER_RE.search(t))


WEB_STAT = None                                   # Raspberry priskiria: statistika iš interneto (Google)
LAB = {"pts": "taškai", "reb": "atkovoti kamuoliai", "ast": "rezultatyvūs perdavimai", "stl": "perimti kamuoliai",
       "blk": "blokai", "3pm": "pataikyti tritaškiai"}


def _web_prop(leg):
    t = _fold(f"{leg.get('pick')} {leg.get('market') or ''}")
    line, side = _line(leg, leg.get("pick") or "")
    if line is None or side is None:
        return None
    if CORNER_RE.search(t) or CARD_RE.search(t) or SHOTS_RE.search(t):
        lab = "kampinių" if CORNER_RE.search(t) else "kortelių (geltonos + raudonos)" if CARD_RE.search(t) else \
            "smūgių į vartus" if SHOT_RE.search(t) else "smūgių"
        team = leg.get("team")
        who = f"komandos {team}" if team and _fold(team) not in ("total", "over", "under") else "abiejų komandų bendras"
        what = f"{who} {lab} skaičius per visas rungtynes (be pratęsimo)"
    else:
        keys = _stat_keys(leg.get("pick") or "")
        if keys is None:
            return None
        keys = keys or (["pts"] if line >= 7.5 else None)
        if not keys:
            return None
        what = f"žaidėjo iš statymo „{leg.get('subject') or leg.get('pick')}“: " + " + ".join(LAB[k] for k in keys) + \
            " (jei kelios – sudėk į vieną skaičių)"
    r = WEB_STAT(leg, what)
    if not r:
        return None
    if r.get("played") is False:
        return "push", f"žaidėjas nežaidė – grąžinama (internetas: {str(r.get('source'))[:60]})", "ok"
    try:
        v = float(r["value"])
    except Exception:
        return None
    res = "push" if v == line else ("won" if (v > line) == (side == "Over") else "lost")
    return res, f"{what.split(':')[0]}: {v:g} ({'daugiau' if side == 'Over' else 'mažiau'} {line:g}) – internetas: " \
        f"{str(r.get('source'))[:60]}", "ok"


def settle(leg):
    res = _settle(leg)
    if res[0] is None and WEB_STAT and re.search(r"nerast|nerado|nepasiekiamas", res[2] or ""):
        try:
            w = _web_prop(leg)
        except Exception:
            w = None
        if w:
            return w
    return res


def _settle(leg):
    """(status, detail, info) kaip results.settle; status None – nepavyko."""
    t = _fold(f"{leg.get('pick')} {leg.get('market') or ''}")
    try:
        if (CORNER_RE.search(t) or CARD_RE.search(t) or SHOTS_RE.search(t)) and not leg.get("subject"):
            return _soccer_bet(leg)
        return _player_bet(leg)
    except Exception as e:
        return None, None, f"statistikos šaltinis nepasiekiamas: {str(e)[:80]}"


# ---------------------------------------------------------------- antras šaltinis rezultatui patvirtinti
XPATHS = {"soccer": ["soccer/all"], "hockey": ["hockey/nhl"], "baseball": ["baseball/mlb"],
          "american_football": ["football/nfl", "football/college-football"],
          "basketball": ["basketball/nba", "basketball/wnba", "basketball/mens-college-basketball"]}


def cross_check(leg, e):
    """Ar kitas šaltinis (ESPN / EuroLeague) rodo tą patį rezultatą kaip LiveScore.
    True – sutampa, False – nesutampa (neįskaitom), None – antro šaltinio šioms rungtynėms nėra."""
    try:
        s1, s2 = int(float(e.get("Tr1"))), int(float(e.get("Tr2")))
    except Exception:
        return None
    sp = e.get("_sport")
    other = None
    try:
        if sp == "basketball":
            g = _el_game(leg)
            if g and g[2].get("played"):
                gm = g[2]
                other = (gm["local"]["club"].get("name"), int(gm["local"].get("score") or 0),
                         gm["road"]["club"].get("name"), int(gm["road"].get("score") or 0))
        if other is None and sp in XPATHS:
            path, ev = _espn_game(leg, XPATHS[sp])
            if ev:
                comp = (ev.get("competitions") or [{}])[0]
                if ((comp.get("status") or {}).get("type") or {}).get("state") != "post":
                    return None
                c = comp.get("competitors") or []
                hm = next((x for x in c if x.get("homeAway") == "home"), c[0])
                aw = next((x for x in c if x is not hm), c[-1])
                other = ((hm.get("team") or {}).get("displayName"), int(float(hm.get("score") or 0)),
                         (aw.get("team") or {}).get("displayName"), int(float(aw.get("score") or 0)))
    except Exception:
        return None
    if other is None:
        return None
    n1, o1, n2, o2 = other
    if R.mscore(e["_t1"], n2) + R.mscore(e["_t2"], n1) > R.mscore(e["_t1"], n1) + R.mscore(e["_t2"], n2):
        o1, o2 = o2, o1
    return (o1, o2) == (s1, s2)


# ---------------------------------------------------------------- atsarginis šaltinis, kai LiveScore rungtynių neranda
def _sp(leg):
    t = _fold(f"{leg.get('sport')} {leg.get('league')}")
    for k, rx in (("basketball", r"basket|krepsin|nba|euroleag|eurocup|ncaab"), ("hockey", r"hockey|ledo|nhl|khl"),
                  ("american_football", r"american|nfl|ncaaf"), ("baseball", r"baseball|mlb|beisbol"),
                  ("soccer", r"soccer|football|futbol")):
        if re.search(rx, t):
            return k
    return None


def alt_event(leg):
    """ESPN / EuroLeague rungtynės LiveScore formatu (results.evaluate jas įvertina taip pat)."""
    sp = _sp(leg)
    k, _ = _when(leg)
    tries = [sp] if sp else ["soccer", "basketball", "hockey"]
    for s in tries:
        if s == "basketball":
            g = _el_game(leg)
            if g:
                gm = g[2]
                if not gm.get("played"):
                    return None
                t1, t2 = gm["local"]["club"].get("name"), gm["road"]["club"].get("name")
                h, a = int(gm["local"].get("score") or 0), int(gm["road"].get("score") or 0)
                e = {"_t1": t1, "_t2": t2, "Tr1": h, "Tr2": a, "Eps": "FT", "_sport": "basketball",
                     "_league": "EuroLeague" if g[0] == "E" else "EuroCup", "Ewt": 1 if h > a else 2,
                     "_start": datetime.fromisoformat(str(gm.get("date"))[:16])}
                break
        if s not in XPATHS:
            continue
        path, ev = _espn_game(leg, XPATHS[s])
        if not ev:
            continue
        comp = (ev.get("competitions") or [{}])[0]
        st = (comp.get("status") or {}).get("type") or {}
        name = str(st.get("name") or "").upper()
        if "POSTPONED" in name or "CANCELED" in name or "CANCELLED" in name:
            eps = "POSTP."
        elif st.get("state") == "post" and st.get("completed", True):
            if s == "soccer" and re.search(r"AET|PEN|EXTRA", name):
                return None                               # futbolas su pratęsimu – 90 min. rezultato ESPN neduoda
            eps = "FT"
        else:
            return None
        c = comp.get("competitors") or []
        hm = next((x for x in c if x.get("homeAway") == "home"), c[0])
        aw = next((x for x in c if x is not hm), c[-1])
        try:
            start = datetime.fromisoformat(str(ev.get("date"))[:16]) + timedelta(hours=2)
        except Exception:
            start = k
        e = {"_t1": (hm.get("team") or {}).get("displayName"), "_t2": (aw.get("team") or {}).get("displayName"),
             "Tr1": int(float(hm.get("score") or 0)), "Tr2": int(float(aw.get("score") or 0)), "Eps": eps,
             "_sport": s, "_league": "ESPN " + path.split("/")[1], "_start": start,
             "Ewt": 1 if hm.get("winner") else 2 if aw.get("winner") else None}
        break
    else:
        return None
    h, a = leg.get("home"), leg.get("away")
    sw = (R.mscore(h, e["_t2"]) + R.mscore(a, e["_t1"])) > (R.mscore(h, e["_t1"]) + R.mscore(a, e["_t2"]))
    return dict(e, _swapped=sw)


# ---------------------------------------------------------------- FotMob – labai daug futbolo lygų (jaunimo, rezervų, žemesnių)
FOTMOB = "https://www.fotmob.com/api/data/matches?date={d:%Y%m%d}&timezone=Europe%2FVilnius&ccode3=LTU"


def fotmob_event(leg):
    """Futbolo rungtynės iš FotMob dienos sąrašo LiveScore formatu (None – nerado ar nesibaigė tvarkingai)."""
    if _sp(leg) not in (None, "soccer"):
        return None
    k, c = _when(leg)
    best = None
    for d in (k.date(), k.date() - timedelta(days=1), k.date() + timedelta(days=1)):
        old = d < (datetime.now() - timedelta(days=2)).date()
        try:
            data = _get(FOTMOB.format(d=d), ttl=86400 if old else 600)
        except Exception:
            continue
        for L in data.get("leagues") or []:
            for m in L.get("matches") or []:
                h, a = m.get("home") or {}, m.get("away") or {}
                ok, _ = _pair(leg, [h.get("name"), h.get("shortName")], [a.get("name"), a.get("shortName")])
                if not ok:
                    continue
                st = m.get("status") or {}
                try:
                    t = datetime.fromisoformat(str(st.get("utcTime")).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                except Exception:
                    continue
                if not (c - timedelta(hours=30) <= t <= c + timedelta(hours=36)):
                    continue
                gap = abs((t - k).total_seconds())
                if best is None or gap < best[0]:
                    best = (gap, m, t, L.get("name"))
    if not best:
        return None
    _, m, t, lname = best
    st = m.get("status") or {}
    short = str(((st.get("reason") or {}).get("short")) or "").upper()
    if st.get("cancelled") or "CANC" in short or "PP" == short or "POSTP" in short:
        eps = "POSTP."
    elif st.get("finished") and short in ("FT", ""):
        eps = "FT"
    elif st.get("finished"):
        return None                                       # AET / Pen – 90 min. rezultato sąraše nėra
    else:
        eps = "NS"
    e = {"_t1": (m.get("home") or {}).get("name"), "_t2": (m.get("away") or {}).get("name"),
         "Tr1": (m.get("home") or {}).get("score"), "Tr2": (m.get("away") or {}).get("score"), "Eps": eps,
         "_sport": "soccer", "_league": f"FotMob, {lname}", "_start": t}
    if e["Tr1"] is not None and e["Tr2"] is not None:
        e["Ewt"] = 1 if e["Tr1"] > e["Tr2"] else 2 if e["Tr2"] > e["Tr1"] else None
    h, a = leg.get("home"), leg.get("away")
    e["_swapped"] = (R.mscore(h, e["_t2"]) + R.mscore(a, e["_t1"])) > (R.mscore(h, e["_t1"]) + R.mscore(a, e["_t2"]))
    return e


# ---------------------------------------------------------------- SofaScore – jei atvirai pasiekiama (jei blokuoja – negudraujam)
SOFA = "https://api.sofascore.com/api/v1/sport/{sport}/scheduled-events/{d:%Y-%m-%d}"
SOFA_SPORT = {"soccer": "football", "basketball": "basketball", "hockey": "ice-hockey", "tennis": "tennis",
              "american_football": "american-football", "baseball": "baseball", "handball": "handball",
              "volleyball": "volleyball"}
SOFA_STATE = {"off_until": 0, "last": ""}
SOFA_C = {}
SOFA_LOCK = threading.Lock()


def sofa_event(leg):
    if time.time() < SOFA_STATE["off_until"]:
        return None
    sp = _sp(leg) or ({"handball": "handball", "volleyball": "volleyball", "tennis": "tennis"}.get(_fold(leg.get("sport"))))
    slug = SOFA_SPORT.get(sp or "soccer", "football")
    k, c = _when(leg)
    best = None
    for d in (k.date(), k.date() - timedelta(days=1), k.date() + timedelta(days=1)):
        old = d < (datetime.now() - timedelta(days=2)).date()
        url = SOFA.format(sport=slug, d=d)
        hit = SOFA_C.get(url)
        if hit and time.time() - hit[0] < (7 * 86400 if old else 600):
            data = hit[1]
        else:
            data = None
        if data is None:                                  # mandagiai: ne dažniau kaip kas 6 s (ir vienu metu – viena)
            with SOFA_LOCK:
                wait = SOFA_STATE.get("t", 0) + 6 - time.time()
                if wait > 0:
                    time.sleep(wait)
                SOFA_STATE["t"] = time.time()
        try:
            if data is None:
                raw = _get(url, ttl=1)
                _c.pop(url, None)                         # dideli sąrašai – laikom tik reikalingus laukus
                keep = ("homeTeam", "awayTeam", "startTimestamp", "status", "homeScore", "awayScore", "winnerCode")
                data = {"events": [{k: ev.get(k) for k in keep} | {"tournament": {"name": ((ev.get("tournament") or {})
                                                                                            .get("name"))}}
                                   for ev in raw.get("events") or []]}
                for ev in data["events"]:
                    for t in ("homeTeam", "awayTeam"):
                        ev[t] = {"name": (ev.get(t) or {}).get("name"), "shortName": (ev.get(t) or {}).get("shortName")}
                    ev["status"] = {"type": (ev.get("status") or {}).get("type")}
                SOFA_C[url] = (time.time(), data)
        except urllib.error.HTTPError as e:
            SOFA_STATE["last"] = f"HTTP {e.code}"
            if e.code in (401, 403, 429):
                SOFA_STATE["off_until"] = time.time() + 1200          # ribojimas – palaukiam 20 min., neapeinam
                return None
            continue
        except Exception as e:
            SOFA_STATE["last"] = type(e).__name__
            continue
        SOFA_STATE["last"] = "ok"
        for ev in data.get("events") or []:
            h, a = ev.get("homeTeam") or {}, ev.get("awayTeam") or {}
            ok, _ = _pair(leg, [h.get("name"), h.get("shortName")], [a.get("name"), a.get("shortName")])
            if not ok:
                continue
            try:
                t = datetime.fromtimestamp(int(ev.get("startTimestamp")))
            except Exception:
                continue
            if not (c - timedelta(hours=30) <= t <= c + timedelta(hours=36)):
                continue
            gap = abs((t - k).total_seconds())
            if best is None or gap < best[0]:
                best = (gap, ev, t)
    if not best:
        return None
    _, ev, t = best
    st = str((ev.get("status") or {}).get("type") or "").lower()
    hs, as_ = ev.get("homeScore") or {}, ev.get("awayScore") or {}
    eps = {"finished": "FT", "canceled": "CANC.", "postponed": "POSTP.", "notstarted": "NS"}.get(st, "LIVE")
    if eps == "FT" and slug == "football" and hs.get("normaltime") is not None and hs.get("current") != hs.get("normaltime"):
        eps = "AET"
    e = {"_t1": (ev.get("homeTeam") or {}).get("name"), "_t2": (ev.get("awayTeam") or {}).get("name"),
         "Tr1": hs.get("current"), "Tr2": as_.get("current"), "Eps": eps,
         "_sport": {"football": "soccer", "ice-hockey": "hockey"}.get(slug, slug),
         "_league": "SofaScore, " + str(((ev.get("tournament") or {}).get("name")) or ""), "_start": t}
    if hs.get("normaltime") is not None and as_.get("normaltime") is not None:
        e["Tr1OR"], e["Tr2OR"] = hs["normaltime"], as_["normaltime"]
    if hs.get("period1") is not None and as_.get("period1") is not None:
        e["Trh1"], e["Trh2"] = hs["period1"], as_["period1"]
    wc = ev.get("winnerCode")
    e["Ewt"] = 1 if wc == 1 else 2 if wc == 2 else None
    h, a = leg.get("home"), leg.get("away")
    e["_swapped"] = (R.mscore(h, e["_t2"]) + R.mscore(a, e["_t1"])) > (R.mscore(h, e["_t1"]) + R.mscore(a, e["_t2"]))
    return e
