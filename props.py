"""Statymai, kurių LiveScore neįvertina: žaidėjų statistika ir futbolo kampiniai/kortelės/smūgiai.

Šaltiniai (vieši, nemokami, be prisijungimo):
  * EuroLeague / EuroCup – oficialus api-live.euroleague.net (kiekvieno žaidėjo taškai, atkovoti, perdavimai...)
  * NBA / WNBA / NCAA – ESPN (site.api.espn.com) rungtynių protokolas
  * futbolo kampiniai, kortelės, smūgiai – ESPN (didžiosios lygos ir taurės)
Jei rungtynių ar žaidėjo rasti nepavyksta, grąžinama None – toks statymas į statistiką neįskaitomas.
"""
import json
import html
import re
import threading
import time
import urllib.error
import urllib.parse
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


NFL_KEYS = (("rush_yds", r"begimo jard\w*|rushing yards?|rush yds|rushing yds|begim\w* jard"),
            ("rec_yds", r"priemim\w* jard\w*|receiving yards?|rec yds|receiving yds"),
            ("pass_yds", r"perdavim\w* jard\w*|passing yards?|pass yds|passing yds"),
            ("rec", r"\breceptions?\b|priemim\w*(?! jard)|\bcatches\b"),
            ("rush_att", r"rushing attempts?|carries|begimo bandym\w*"),
            ("pass_td", r"passing (td|touchdowns?)|perdavim\w* (td|tacdaun\w*)"),
            ("td", r"anytime (td|touchdown)|touchdown|tacdaun\w*|\btd\b"),
            ("pass_cmp", r"completions?|tiksl\w* perdavim\w*"),
            ("int", r"interceptions?|perimim\w*"))
NFL_LAB = {"rush_yds": "bėgimo jardai", "rec_yds": "priėmimo jardai", "pass_yds": "perdavimo jardai",
           "rec": "priėmimai", "rush_att": "bėgimo bandymai", "pass_td": "perdavimo TD", "td": "TD",
           "pass_cmp": "tikslūs perdavimai", "int": "perimti kamuoliai"}


def _nfl_keys(text):
    t = _fold(text)
    for k, rx in NFL_KEYS:
        if re.search(rx, t):
            return [k]
    return []


def _players_nfl(summary):
    """ESPN NFL/NCAA protokolas → [{first, last, played, v}] (kategorijos sujungiamos pagal žaidėją)."""
    by = {}
    col = {"rushing": {"CAR": "rush_att", "YDS": "rush_yds", "TD": "rush_td"},
           "receiving": {"REC": "rec", "YDS": "rec_yds", "TD": "rec_td"},
           "passing": {"YDS": "pass_yds", "TD": "pass_td", "INT": "int_thrown", "C/ATT": "pass_cmp"},
           "defensive": {}, "interceptions": {"INT": "int"}}
    for team in (summary.get("boxscore") or {}).get("players") or []:
        for grp in team.get("statistics") or []:
            cmap = col.get(str(grp.get("name") or "").lower())
            if cmap is None:
                continue
            labels = [x.upper() for x in grp.get("labels") or []]
            for a in grp.get("athletes") or []:
                nm = (a.get("athlete") or {}).get("displayName", "")
                p = by.setdefault(nm, {"v": {}})
                st = a.get("stats") or []
                for lab, key in cmap.items():
                    if lab in labels and len(st) > labels.index(lab):
                        raw = str(st[labels.index(lab)]).split("/")[0]
                        try:
                            p["v"][key] = p["v"].get(key, 0) + float(raw)
                        except Exception:
                            pass
    out = []
    for nm, p in by.items():
        v = p["v"]
        for k in ("rush_yds", "rec_yds", "pass_yds", "rec", "rush_att", "pass_td", "pass_cmp", "int"):
            v.setdefault(k, 0.0)
        v["td"] = v.get("rush_td", 0) + v.get("rec_td", 0)
        parts = [x for x in nm.split() if x.lower().strip(".") not in ("jr", "sr", "ii", "iii", "iv")]
        out.append({"first": " ".join(parts[:-1]), "last": parts[-1] if parts else "", "played": True, "v": v})
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
    line, side = _line(leg, leg.get("pick") or "")
    sport = _fold(leg.get("sport"))
    lg = _fold(leg.get("league"))
    nk = _nfl_keys(leg.get("pick") or "")
    if nk or "american" in sport or re.search(r"\bnfl\b|ncaaf|college football", lg):
        if not nk:
            return None, None, "neaišku, kokia NFL statistika"
        if nk == ["td"] and line is None:
            line, side = 0.5, "Over"                     # „anytime touchdown“
        if line is None or side is None:
            return None, None, "nėra linijos"
        path, ev = _espn_game(leg, ["football/nfl", "football/college-football"])
        if not ev:
            return None, None, "NFL rungtynės ESPN nerastos"
        if ((ev.get("competitions") or [{}])[0].get("status") or {}).get("type", {}).get("state") != "post":
            return None, None, "rungtynės dar nesibaigė"
        players = _players_nfl(_get(f"{ESPN}/{path}/summary?event={ev['id']}", ttl=86400))
        return _apply_players(leg, t, players, nk, line, side, "ESPN " + path.split("/")[1].upper(), NFL_LAB)
    keys = _stat_keys(leg.get("pick") or "")
    if keys is None:
        return None, None, "procentinė statistika – neskaičiuojam"
    if line is None or side is None:
        return None, None, "nėra linijos"
    if not keys:
        if line < 7.5:
            return None, None, "neaišku, kokia statistika"
        keys = ["pts"]
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
        if players is None and not re.search(r"\b(nba|wnba|ncaa|euroleag|eurocup)\b", lg):
            try:
                g = _lkl_game(leg)                       # LKL / KMT – oficialus lkl.lt protokolas
            except Exception:
                g = None
            if g:
                players, src = _players_lkl(g[2]), "LKL.lt"
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
    return _apply_players(leg, t, players, keys, line, side, src, LAB)


def _apply_players(leg, t, players, keys, line, side, src, labels):
    text = " ".join(str(leg.get(x) or "") for x in ("subject", "pick", "team"))
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
    lab = "+".join(labels.get(k, k) for k in keys)
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
    if R.BTTS_RE.search(t):
        return False                                  # „abi komandos įmuš“ – ne žaidėjo statymas
    if leg.get("subject") and R.team_subject(leg):
        leg = dict(leg, subject=None)
        return bool(CORNER_RE.search(t) or CARD_RE.search(t) or SHOTS_RE.search(t) or R.PLAYER_RE.search(t))
    if leg.get("subject"):
        return True
    if CORNER_RE.search(t) or CARD_RE.search(t) or SHOTS_RE.search(t):
        return True
    return bool(R.PLAYER_RE.search(t)) or bool(_nfl_keys(t)) and bool(re.search(r"jard|yard|touchdown|tacdaun|\btd\b|reception|priemim", t))


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


SCORER_RE = re.compile(r"goal ?scorer|anytime|to score|score a goal|imus ivarti|pelnys ivarti|ivarcio autorius|ives ivarti")


_ORD = {"first": 1, "1st": 1, "pirm": 1, "second": 2, "2nd": 2, "antr": 2, "third": 3, "3rd": 3, "trec": 3,
        "fourth": 4, "4th": 4, "ketvirt": 4, "fifth": 5, "5th": 5, "penkt": 5, "sixth": 6, "6th": 6, "sest": 6}


def _team_goal(leg):
    """„Spain to score 5th goal“, „first goal“, „last goal“ – kuri komanda įmušė N-tąjį įvartį (ESPN įvykių eiga)."""
    t = _fold(leg.get("pick"))
    last = bool(re.search(r"\blast goal|paskutin\w* ivart", t))
    m = re.search(r"\b(\d)(st|nd|rd|th)? (goal|ivart)", t) or \
        re.search(r"\b(first|second|third|fourth|fifth|sixth|pirm|antr|trec|ketvirt|penkt|sest)\w* (goal|ivart)", t)
    first = re.search(r"score first|first to score|scores? first|pirm\w* imus|imus pirm", t)
    if not m and not last and not first:
        return None, None, "komandos įvarčio rinka (neaišku, kelintas įvartis) – nežinoma rinka"
    n = None if last else 1 if (first and not m) else int(m.group(1)) if m.group(1).isdigit() else _ORD[m.group(1)]
    mine = leg.get("home") if R.same(t, leg.get("home")) and not R.same(t, leg.get("away") or "") else leg.get("away")
    no = bool(re.search(r"\b(no|ne)\s*$", t))
    path, ev = _espn_game(leg, [f"soccer/{x}" for x in SOCCER])
    if not ev:
        return None, None, "rungtynių įvykių eiga nerasta"
    if ((ev.get("competitions") or [{}])[0].get("status") or {}).get("type", {}).get("state") != "post":
        return None, None, "rungtynės dar nesibaigė"
    su = _get(f"{ESPN}/{path}/summary?event={ev['id']}", ttl=86400)
    if su.get("keyEvents") is None:
        return None, None, "rungtynių įvykių eiga nerasta"
    goals = [((k.get("team") or {}).get("displayName") or "") for k in su["keyEvents"] if k.get("scoringPlay")]
    pen = [k for k in su["keyEvents"] if k.get("scoringPlay") and "shootout" in _fold(((k.get("type") or {}).get("text")))]
    goals = goals[:len(goals) - len(pen)] if pen else goals
    if last:
        if not goals:
            return ("won" if no else "lost"), "įvarčių nebuvo", "ok"
        hit = R.same(mine, goals[-1])
        return ("won" if hit != no else "lost"), f"paskutinį įvartį įmušė {goals[-1]} (ESPN)", "ok"
    if len(goals) < n:
        return ("won" if no else "lost"), f"{n}-ojo įvarčio nebuvo – iš viso {len(goals)} (ESPN)", "ok"
    hit = R.same(mine, goals[n - 1])
    return ("won" if hit != no else "lost"), f"{n}-ąjį įvartį įmušė {goals[n - 1]} (ESPN, įvarčių eiga: {', '.join(goals)})", "ok"


def _scorer(leg):
    """Futbolas: ar žaidėjas įmušė įvartį (ESPN įvykiai; jei nėra – internetas)."""
    who = leg.get("subject") or re.sub(r"(?i)anytime|goal ?scorer|to score|score a goal|\(.*?\)|-|–", " ",
                                       str(leg.get("pick") or "")).strip()
    toks = [x for x in _fold(who).split() if len(x) > 2]
    if not toks:
        return None, None, "neaišku, kuris žaidėjas"
    if not leg.get("subject") and any(R.same(who, leg.get(x)) for x in ("home", "away") if leg.get(x)):
        return _team_goal(leg)
    no = bool(re.search(r"\b(no|ne)\s*$", _fold(leg.get("pick"))))
    path, ev = _espn_game(leg, [f"soccer/{x}" for x in SOCCER])
    if ev:
        if ((ev.get("competitions") or [{}])[0].get("status") or {}).get("type", {}).get("state") != "post":
            return None, None, "rungtynės dar nesibaigė"
        su = _get(f"{ESPN}/{path}/summary?event={ev['id']}", ttl=86400)
        names = []
        for k in su.get("keyEvents") or []:
            typ = _fold(((k.get("type") or {}).get("text")) or (k.get("type") or {}).get("type"))
            if not k.get("scoringPlay") or "own" in typ:
                continue
            for pt in k.get("participants") or []:
                names.append(_fold(((pt.get("athlete") or {}).get("displayName")) or ""))
                break
        if names or (su.get("keyEvents") is not None):
            last = toks[-1]
            hit = any(last in n.split() or (len(toks) > 1 and toks[0] in n.split() and last[:4] in n) for n in names)
            res = "won" if hit != no else "lost"
            return res, f"{who}: {'įmušė' if hit else 'neįmušė'} (ESPN: {', '.join(n.title() for n in names) or 'įvarčių nebuvo'})", "ok"
    if WEB_STAT:
        r = WEB_STAT(leg, f"kiek įvarčių įmušė žaidėjas „{who}“ (be savų įvarčių)")
        if r and r.get("played") is False:
            return "push", f"{who} nežaidė – grąžinama", "ok"
        try:
            v = float(r["value"])
            hit = v >= 1
            return ("won" if hit != no else "lost"), f"{who}: įvarčių {v:g} – internetas: {str(r.get('source'))[:60]}", "ok"
        except Exception:
            pass
    return None, None, "įvarčių autoriai nerasti"


def _settle(leg):
    """(status, detail, info) kaip results.settle; status None – nepavyko."""
    t = _fold(f"{leg.get('pick')} {leg.get('market') or ''}")
    if SCORER_RE.search(t) and (_sp(leg) or "soccer") == "soccer" and not R.BTTS_RE.search(t):
        try:
            return _scorer(leg)
        except Exception as e:
            return None, None, f"statistikos šaltinis nepasiekiamas: {str(e)[:80]}"
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
            start = datetime.fromisoformat(str(ev.get("date")).replace("Z", "+00:00"))
            start = start.astimezone().replace(tzinfo=None) if start.tzinfo else start + timedelta(hours=2)
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


# ---------------------------------------------------------------- API-Football (api-sports.io) – oficialus API su raktu
CFG = {}                                              # Raspberry priskiria savo nustatymus (apisports_key)
CACHE_DIR = None                                      # kur laikyti dienų sąrašus (kad neeikvotume 100 užkl./d.)
APIS_STATE = {"last": "", "used": {}, "day": ""}


APIS = {"soccer": ("https://v3.football.api-sports.io/fixtures", "f"),
        "basketball": ("https://v1.basketball.api-sports.io/games", "b"),
        "hockey": ("https://v1.hockey.api-sports.io/games", "h")}


def _apis_day(d, sport="soccer"):
    """Vienos dienos visos rungtynės iš API-Sports (vienas raktas – futbolas, krepšinis, ledo ritulys; po 100 užkl./d.)."""
    key = (CFG or {}).get("apisports_key")
    if not key or sport not in APIS:
        return None
    url, tag = APIS[sport]
    f = (CACHE_DIR / f"apis_{tag}_{d:%Y%m%d}.json") if CACHE_DIR else None
    old = d < (datetime.now() - timedelta(days=2)).date()
    if f and f.exists() and (old or time.time() - f.stat().st_mtime < 900):
        try:
            return json.loads(f.read_text("utf-8"))
        except Exception:
            pass
    today = datetime.now().strftime("%Y-%m-%d")
    if APIS_STATE["day"] != today:
        APIS_STATE["day"], APIS_STATE["used"] = today, {}
    used = APIS_STATE["used"] if isinstance(APIS_STATE["used"], dict) else {}
    APIS_STATE["used"] = used
    rng = APIS_STATE.get("range", {}).get(sport)          # nemokamas planas: tik vakar–rytoj
    if rng and not (rng[0] <= d.isoformat() <= rng[1]) and d < datetime.now().date():
        return None
    if used.get(sport, 0) >= int((CFG or {}).get("apisports_per_day", 95)):
        return None
    req = urllib.request.Request(f"{url}?date={d:%Y-%m-%d}&timezone=Europe/Vilnius",
                                 headers={"x-apisports-key": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        APIS_STATE["last"] = f"{sport}: klaida {type(e).__name__}"
        return None
    used[sport] = used.get(sport, 0) + 1
    if raw.get("errors"):
        msg = str(raw["errors"])
        APIS_STATE["last"] = f"{sport}: " + msg[:200]
        m = re.search(r"from (\d{4}-\d\d-\d\d) to (\d{4}-\d\d-\d\d)", msg)
        if m:
            APIS_STATE.setdefault("range", {})[sport] = (m.group(1), m.group(2))
        return None
    out = []
    for x in raw.get("response") or []:
        tm = x.get("teams") or {}
        lg = x.get("league") or {}
        if sport == "soccer":
            fx, sc = x.get("fixture") or {}, x.get("score") or {}
            out.append({"id": fx.get("id"), "date": fx.get("date"), "st": (fx.get("status") or {}).get("short"),
                        "h": (tm.get("home") or {}).get("name"), "a": (tm.get("away") or {}).get("name"),
                        "gh": (x.get("goals") or {}).get("home"), "ga": (x.get("goals") or {}).get("away"),
                        "ft": sc.get("fulltime"), "ht": sc.get("halftime"),
                        "lg": f"{(x.get('league') or {}).get('country')} – {lg.get('name')}"})
        else:
            sc = x.get("scores") or {}
            hs, as_ = sc.get("home"), sc.get("away")
            if isinstance(hs, dict):
                hs, as_ = hs.get("total"), (as_ or {}).get("total")
            out.append({"date": x.get("date"), "st": (x.get("status") or {}).get("short"),
                        "h": (tm.get("home") or {}).get("name"), "a": (tm.get("away") or {}).get("name"),
                        "gh": hs, "ga": as_, "lg": f"{(x.get('country') or {}).get('name')} – {lg.get('name')}"})
    APIS_STATE["last"] = f"{sport}: ok ({len(out)} rungtynių {d:%m-%d})"
    if f:
        try:
            f.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
        except Exception:
            pass
    return out


def apisports_event(leg):
    sport = _sp(leg) or "soccer"
    if sport not in APIS or not (CFG or {}).get("apisports_key"):
        return None
    k, c = _when(leg)
    best = None
    for d in (k.date(), k.date() - timedelta(days=1), k.date() + timedelta(days=1)):
        for m in _apis_day(d, sport) or []:
            ok, _ = _pair(leg, [m["h"]], [m["a"]])
            if not ok:
                continue
            try:
                t = datetime.fromisoformat(str(m["date"])).astimezone().replace(tzinfo=None)
            except Exception:
                continue
            if not (c - timedelta(hours=30) <= t <= c + timedelta(hours=36)):
                continue
            gap = abs((t - k).total_seconds())
            if best is None or gap < best[0]:
                best = (gap, m, t)
    if not best:
        return None
    _, m, t = best
    st = str(m.get("st") or "").upper()
    eps = {"FT": "FT", "AET": "AET", "PEN": "AP", "AOT": "AOT", "AP": "AP", "PST": "POSTP.", "POST": "POSTP.",
           "CANC": "CANC.", "ABD": "ABD.", "AWD": "AW.", "WO": "WO"}.get(st, "NS" if st in ("NS", "TBD") else "LIVE")
    e = {"_t1": m["h"], "_t2": m["a"], "Tr1": m.get("gh"), "Tr2": m.get("ga"), "Eps": eps, "_sport": sport,
         "_league": "API-Sports, " + str(m.get("lg") or ""), "_start": t}
    ft, ht = m.get("ft") or {}, m.get("ht") or {}
    if ft.get("home") is not None and ft.get("away") is not None:
        e["Tr1OR"], e["Tr2OR"] = ft["home"], ft["away"]       # 90 min. rezultatas
    if ht.get("home") is not None and ht.get("away") is not None:
        e["Trh1"], e["Trh2"] = ht["home"], ht["away"]
    if e["Tr1"] is not None and e["Tr2"] is not None:
        e["Ewt"] = 1 if e["Tr1"] > e["Tr2"] else 2 if e["Tr2"] > e["Tr1"] else None
    h, a = leg.get("home"), leg.get("away")
    e["_swapped"] = (R.mscore(h, e["_t2"]) + R.mscore(a, e["_t1"])) > (R.mscore(h, e["_t1"]) + R.mscore(a, e["_t2"]))
    return e


def apis_prefetch():
    """Kas valandą: vakar ir šiandien – visų rungtynių sąrašai išsaugomi diske visam laikui
    (nemokamas planas leidžia tik paskutines dienas, todėl renkam iš anksto)."""
    if not (CFG or {}).get("apisports_key"):
        return
    now = datetime.now()
    for sport in APIS:
        for d in ((now - timedelta(days=1)).date(), now.date()):
            f = (CACHE_DIR / f"apis_{APIS[sport][1]}_{d:%Y%m%d}.json") if CACHE_DIR else None
            if f and f.exists() and time.time() - f.stat().st_mtime < 3000:
                continue
            if f and f.exists():
                f.touch(exist_ok=True)
                try:
                    f.unlink()                            # pasenusi diena – parsisiunčiam iš naujo
                except Exception:
                    pass
            _apis_day(d, sport)


# ---------------------------------------------------------------- 365Scores – viešas svetainės sąrašas (jei atvirai pasiekiamas)
S365 = ("https://webws.365scores.com/web/games/allscores/?appTypeId=5&langId=1&timezoneName=Europe/Vilnius"
        "&startDate={d:%d/%m/%Y}&endDate={d:%d/%m/%Y}&sports=1")
S365_STATE = {"off_until": 0, "last": ""}


def s365_event(leg):
    if _sp(leg) not in (None, "soccer") or time.time() < S365_STATE["off_until"]:
        return None
    k, c = _when(leg)
    best = None
    for d in (k.date(), k.date() - timedelta(days=1), k.date() + timedelta(days=1)):
        old = d < (datetime.now() - timedelta(days=2)).date()
        try:
            data = _get(S365.format(d=d), ttl=7 * 86400 if old else 600)
        except urllib.error.HTTPError as e:
            S365_STATE["last"] = f"HTTP {e.code}"
            if e.code in (401, 403, 429):
                S365_STATE["off_until"] = time.time() + 1800
                return None
            continue
        except Exception as e:
            S365_STATE["last"] = type(e).__name__
            continue
        S365_STATE["last"] = "ok"
        for g in data.get("games") or []:
            h, a = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
            ok, _ = _pair(leg, [h.get("name"), h.get("shortName")], [a.get("name"), a.get("shortName")])
            if not ok:
                continue
            try:
                t = datetime.fromisoformat(str(g.get("startTime"))).astimezone().replace(tzinfo=None)
            except Exception:
                continue
            if not (c - timedelta(hours=30) <= t <= c + timedelta(hours=36)):
                continue
            gap = abs((t - k).total_seconds())
            if best is None or gap < best[0]:
                best = (gap, g, t)
    if not best:
        return None
    _, g, t = best
    txt = str(g.get("statusText") or "").lower()
    if "ended" in txt or txt in ("ft", "final"):
        eps = "FT"
    elif "postp" in txt or "cancel" in txt or "abandon" in txt:
        eps = "POSTP."
    elif "penalt" in txt or "extra" in txt or "aet" in txt:
        return None                                       # be 90 min. rezultato
    else:
        eps = "NS"
    hs, as_ = (g.get("homeCompetitor") or {}).get("score"), (g.get("awayCompetitor") or {}).get("score")
    e = {"_t1": (g.get("homeCompetitor") or {}).get("name"), "_t2": (g.get("awayCompetitor") or {}).get("name"),
         "Tr1": None if hs is None or hs < 0 else int(hs), "Tr2": None if as_ is None or as_ < 0 else int(as_),
         "Eps": eps, "_sport": "soccer", "_league": "365Scores, " + str(g.get("competitionDisplayName") or ""),
         "_start": t}
    if e["Tr1"] is not None and e["Tr2"] is not None:
        e["Ewt"] = 1 if e["Tr1"] > e["Tr2"] else 2 if e["Tr2"] > e["Tr1"] else None
    h, a = leg.get("home"), leg.get("away")
    e["_swapped"] = (R.mscore(h, e["_t2"]) + R.mscore(a, e["_t1"])) > (R.mscore(h, e["_t1"]) + R.mscore(a, e["_t2"]))
    return e


# ---------------------------------------------------------------- SportScore – viešas API (TheSports duomenys), labai plati aprėptis
# Sąlyga: rodyti „Powered by SportScore“ nuorodą ten, kur naudojami duomenys (programėlėje – „analitika“ skiltyje).
SPS = "https://sportscore.com/api/v1"
SPS_STATE = {"off_until": 0, "last": ""}


def _sps(path, ttl):
    if time.time() < SPS_STATE["off_until"]:
        return None
    try:
        return _get(f"{SPS}{path}{'&' if '?' in path else '?'}src=guru-pi", ttl=ttl)
    except urllib.error.HTTPError as e:
        SPS_STATE["last"] = f"HTTP {e.code}"
        if e.code in (403, 429):
            SPS_STATE["off_until"] = time.time() + 900
        return None
    except Exception as e:
        SPS_STATE["last"] = type(e).__name__
        return None


def _sps_teams(name, sport):
    """Komandų kandidatai pagal pavadinimą: pilnas, be trumpų žodžių/žymių, ilgiausias žodis."""
    full = re.sub(r"\s+", " ", str(name or "")).strip()
    words = [w for w in re.findall(r"[^\W\d_]{4,}", full) if _fold(w) not in R.GENERIC]
    qs = [full, " ".join(words), max(words, key=len) if words else ""]
    seen, out = set(), []
    for q in qs:
        if len(q) < 3 or q in seen:
            continue
        seen.add(q)
        d = _sps(f"/search/?sport={sport}&limit=20&q={urllib.parse.quote(q)}", ttl=7 * 86400)
        for t in (d or {}).get("teams") or []:
            if t.get("slug") and _team_ok(name, [t.get("name")]) and t["slug"] not in {x[1] for x in out}:
                out.append((t.get("name"), t["slug"]))
        if out:
            break
    return out[:4]


def sportscore_event(leg):
    sp = _sp(leg) or "soccer"
    sport = {"soccer": "football", "basketball": "basketball"}.get(sp)
    if not sport:
        return None
    k, c = _when(leg)
    best = None
    for side in ("home", "away"):
        if not leg.get(side):
            continue
        for tname, slug in _sps_teams(leg.get(side), sport):
            for d in (k.date(), k.date() - timedelta(days=1), k.date() + timedelta(days=1)):
                old = d < (datetime.now() - timedelta(days=2)).date()
                f = _sps(f"/fixtures/?sport={sport}&team={slug}&date={d:%Y-%m-%d}", ttl=7 * 86400 if old else 600)
                for m in (f or {}).get("matches") or []:
                    ok, _ = _pair(leg, [m.get("home")], [m.get("away")])
                    if not ok:
                        continue
                    try:
                        t = datetime.fromisoformat(str(m.get("time"))).astimezone().replace(tzinfo=None)
                    except Exception:
                        continue
                    if not (c - timedelta(hours=30) <= t <= c + timedelta(hours=36)):
                        continue
                    gap = abs((t - k).total_seconds())
                    if best is None or gap < best[0]:
                        best = (gap, m, t)
        if best:
            break
    SPS_STATE["last"] = "ok" if best else (SPS_STATE["last"] if SPS_STATE["last"].startswith("HTTP") else "nerasta")
    if not best:
        return None
    _, m, t = best
    st, txt = str(m.get("status") or "").lower(), str(m.get("status_text") or "").lower()
    if st == "finished":
        if sport == "football" and re.search(r"pen|extra|aet|after", txt):
            return None                                   # su pratęsimu / baudiniais – 90 min. rezultato nėra
        eps = "FT"
    elif re.search(r"postpon|cancel|abandon", txt):
        eps = "POSTP."
    else:
        eps = "NS" if st == "upcoming" else "LIVE"
    try:
        hs, as_ = int(m.get("home_score")), int(m.get("away_score"))
    except Exception:
        hs = as_ = None
    e = {"_t1": m.get("home"), "_t2": m.get("away"), "Tr1": hs, "Tr2": as_, "Eps": eps, "_sport": sp,
         "_league": "SportScore, " + str(m.get("competition") or ""), "_start": t}
    if hs is not None and as_ is not None:
        e["Ewt"] = 1 if hs > as_ else 2 if as_ > hs else None
    if eps == "FT" and sport == "football":                   # kėlinio rezultatas – iš rungtynių kortelės
        parts = [x for x in str(m.get("url") or "").split("/") if x]
        if len(parts) >= 3:
            det = _sps(f"/match/?sport=football&slug={parts[2]}", ttl=30 * 86400)
            mm = (det or {}).get("match") or {}
            if str(mm.get("time")) == str(m.get("time")) and mm.get("home_ht_score") not in (None, ""):
                try:
                    e["Trh1"], e["Trh2"] = int(mm["home_ht_score"]), int(mm["away_ht_score"])
                except Exception:
                    pass
    h, a = leg.get("home"), leg.get("away")
    e["_swapped"] = (R.mscore(h, e["_t2"]) + R.mscore(a, e["_t1"])) > (R.mscore(h, e["_t1"]) + R.mscore(a, e["_t2"]))
    return e


# ---------------------------------------------------------------- lažybų bendrovių koeficientai (API-Sports /odds)
# Prototipas: ar statymą realiai galima pastatyti ir už kokį koeficientą. Imam viešus bukmekerių koeficientus
# iš API-Sports (Bet365, Unibet, 1xBet ir kt.) – kazino privataus API neliečiam.
ODDS_URL = "https://v3.football.api-sports.io/odds"
ODDS_STATE = {"last": "", "day": "", "used": 0}


def _apis_fixture(leg):
    """Rungtynės iš API-Sports dienos sąrašo (futbolas) – grąžina (įrašas, ar apversta)."""
    if (_sp(leg) or "soccer") != "soccer":
        return None, False, None
    k, c = _when(leg)
    best = None
    for d in (k.date(), k.date() - timedelta(days=1), k.date() + timedelta(days=1)):
        for m in _apis_day(d, "soccer") or []:
            ok, sw = _pair(leg, [m["h"]], [m["a"]])
            if not ok or not m.get("id"):
                continue
            try:
                t = datetime.fromisoformat(str(m["date"])).astimezone().replace(tzinfo=None)
            except Exception:
                continue
            if not (c - timedelta(hours=30) <= t <= c + timedelta(hours=36)):
                continue
            gap = abs((t - k).total_seconds())
            if best is None or gap < best[0]:
                best = (gap, m, sw, t)
    return (best[1], best[2], best[3]) if best else (None, False, None)


def _odds_raw(fid):
    key = (CFG or {}).get("apisports_key")
    f = (CACHE_DIR / f"odds_{fid}.json") if CACHE_DIR else None
    if f and f.exists() and time.time() - f.stat().st_mtime < 6 * 3600:
        try:
            return json.loads(f.read_text("utf-8"))
        except Exception:
            pass
    today = datetime.now().strftime("%Y-%m-%d")
    used = APIS_STATE["used"] if isinstance(APIS_STATE.get("used"), dict) else {}
    if APIS_STATE.get("day") != today:
        APIS_STATE["day"], APIS_STATE["used"] = today, {}
        used = APIS_STATE["used"]
    if used.get("soccer", 0) >= int((CFG or {}).get("apisports_odds_until", 80)):
        ODDS_STATE["last"] = "dienos užklausų limitas – tęsiam rytoj"
        return None
    req = urllib.request.Request(f"{ODDS_URL}?fixture={fid}", headers={"x-apisports-key": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        ODDS_STATE["last"] = f"klaida {type(e).__name__}"
        return None
    used["soccer"] = used.get("soccer", 0) + 1
    if raw.get("errors"):
        ODDS_STATE["last"] = str(raw["errors"])[:160]
        return None
    books = []
    for x in raw.get("response") or []:
        for bk in x.get("bookmakers") or []:
            mk = {}
            for bt in bk.get("bets") or []:
                mk[str(bt.get("name"))] = {str(v.get("value")): v.get("odd") for v in bt.get("values") or []}
            books.append({"name": bk.get("name"), "m": mk})
    if f:
        try:
            f.write_text(json.dumps(books, ensure_ascii=False), "utf-8")
        except Exception:
            pass
    return books


def _side_of(team, m, sw):
    """'Home' / 'Away' / 'Draw' API-Sports rungtynėse."""
    t = _fold(team or "")
    if re.fullmatch(r"draw|x|lygios\w*", t):
        return "Draw"
    sh, sa = R.mscore(team, m["h"]), R.mscore(team, m["a"])
    if max(sh, sa) <= 0:
        return None
    return "Home" if sh >= sa else "Away"


def _wanted(leg, m, sw):
    """Kokios rinkos ir kokios reikšmės ieškoti (API-Sports pavadinimais)."""
    if is_prop(leg) or R._period_text(leg) != "ft":
        return None
    if re.search(r"nil|sausai|be praleist|clean sheet|exact|tiksl|correct score|margin|half|kelin|"
                 r"interval|corner|kamp|card|kortel|both halves|abu kelin|\bor\b|\barba\b|&|and both|ir abi|team total|"
                 r"individual|komandos total|\bodd\b|\beven\b|lyginis|nelyginis|first goal|pirm\w* ivart|scorer",
                 _fold(f"{leg.get('pick')} {leg.get('market') or ''}")):
        return None                                       # kita rinka – tik pagrindinės (1X2, DC, BTTS, Over/Under, AH)
    side = str(leg.get("side") or "").lower()
    pick = _fold(leg.get("pick") or "")
    line = leg.get("line")
    try:
        line = float(line) if line is not None else None
    except Exception:
        line = None
    if side == "win" or (not side and re.search(r"\b(win|laimes|pergale)\b", pick)):
        sd = _side_of(leg.get("team") or (leg.get("home") if not sw else leg.get("away")), m, sw)
        return ("Match Winner", [sd]) if sd else None
    if side == "dc":
        mm = re.search(r"\b(1x|x2|12)\b", pick)
        if mm:
            v = {"1x": "Home/Draw", "x2": "Draw/Away", "12": "Home/Away"}[mm.group(1)]
            if sw and v != "Home/Away":
                v = {"Home/Draw": "Draw/Away", "Draw/Away": "Home/Draw"}[v]
            return "Double Chance", [v]
        sd = _side_of(leg.get("team"), m, sw)
        return ("Double Chance", ["Home/Draw" if sd == "Home" else "Draw/Away"]) if sd in ("Home", "Away") else None
    if side == "btts" or R.BTTS_RE.search(pick):
        return "Both Teams Score", ["No" if re.search(r"\b(no|ne)\b", pick) else "Yes"]
    if side in ("over", "under") and line is not None:
        if leg.get("team") and _side_of(leg.get("team"), m, sw) in ("Home", "Away"):
            return None                                   # komandos totalas – kol kas nevertinam
        w = "Over" if side == "over" else "Under"
        return "Goals Over/Under", [f"{w} {line:g}", f"{w} {line:.1f}", f"{w} {line:.2f}"]
    if side == "spread" and line is not None:
        sd = _side_of(leg.get("team"), m, sw)
        if sd not in ("Home", "Away"):
            return None
        return "Asian Handicap", [f"{sd} {line:+g}", f"{sd} {line:+.1f}", f"{sd} {line:g}"]
    return None


def market_odds(leg):
    """Ar šį statymą buvo galima pastatyti prieš rungtynes ir už kiek: {"odds": mediana, "best", "book", "n"}.
    None – rinkos nėra / ne futbolas / nežinoma; {"none": priežastis} – tikrai nerasta."""
    if not (CFG or {}).get("apisports_key"):
        return None
    m, sw, t = _apis_fixture(leg)
    if not m:
        return None
    want = _wanted(leg, m, sw)
    if not want:
        return {"none": "rinka netikrinama (ne pagrindinė)"}
    books = _odds_raw(m["id"])
    if books is None:
        return None
    if not books:
        return {"none": "bukmekeriai šių rungtynių nesiūlo"}
    name, vals = want
    got = []
    for bk in books:
        mk = bk["m"].get(name) or {}
        for v in vals:
            if v in mk:
                try:
                    got.append((float(mk[v]), bk["name"]))
                except Exception:
                    pass
                break
    if not got:
        return {"none": f"rinkos „{name} {vals[0]}“ bukmekeriai neturi"}
    got.sort()
    med = got[len(got) // 2][0] if len(got) % 2 else round((got[len(got) // 2 - 1][0] + got[len(got) // 2][0]) / 2, 3)
    ODDS_STATE["last"] = f"ok: {m['h']} – {m['a']} {name} {vals[0]} @ {med}"
    return {"odds": round(med, 2), "best": got[-1][0], "book": got[-1][1], "n": len(got),
            "mk": f"{name}: {vals[0]}", "ko": t.isoformat(timespec="minutes") if t else None}


# ---------------------------------------------------------------- paieška internete per oficialų API (Tavily)
# Paieškos sistemų puslapių (Google, DuckDuckGo, Bing) neskenuojam – jie tai blokuoja (CAPTCHA), o apeidinėti negalima.
# Tavily – paieškos API, skirtas AI: nemokamai 1000 paieškų/mėn., raktas – app.tavily.com (įrašomas „#pi_key tavily …“).
SEARCH_STATE = {"last": "", "n": 0, "ok": 0, "month": "", "used": 0}
_UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def search_lib():
    return None                                           # (suderinamumui su senesniu pi_audit.py)


def web_search(q, n=8):
    """[{title, href, body}] – paieškos rezultatai per Tavily API (jei įrašytas raktas)."""
    key = (CFG or {}).get("tavily_key")
    if not key:
        SEARCH_STATE["last"] = "nėra Tavily rakto"
        return []
    mon = datetime.now().strftime("%Y-%m")
    if SEARCH_STATE["month"] != mon:
        SEARCH_STATE["month"], SEARCH_STATE["used"] = mon, 0
    if SEARCH_STATE["used"] >= int((CFG or {}).get("tavily_per_month", 950)):
        SEARCH_STATE["last"] = "Tavily mėnesio limitas"
        return []
    body = json.dumps({"query": q, "max_results": n, "search_depth": "basic", "include_answer": False,
                       "topic": "general"}).encode()
    req = urllib.request.Request("https://api.tavily.com/search", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    SEARCH_STATE["n"] += 1
    SEARCH_STATE["used"] += 1
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        SEARCH_STATE["last"] = f"Tavily {e.code}"
        return []
    except Exception as e:
        SEARCH_STATE["last"] = f"Tavily {type(e).__name__}"
        return []
    res = [{"title": x.get("title", ""), "href": x.get("url", ""), "body": x.get("content", "")}
           for x in d.get("results") or []]
    SEARCH_STATE["last"] = f"Tavily ok ({len(res)})"
    return res


def page_text(url, limit=5000):
    """Statinio puslapio tekstas (be JavaScript) – papildomai prie paieškos ištraukų."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=20) as r:
            if "html" not in (r.headers.get("Content-Type") or ""):
                return ""
            h = r.read(600000).decode("utf-8", "ignore")
    except Exception:
        return ""
    h = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", h)
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))
    return t[:limit]


def search_context(queries, pages=2, n=8):
    """Paieškos ištraukos (+ kelių puslapių tekstas) AI'ui. Grąžina (tekstas, url sąrašas)."""
    seen, chunks, urls = set(), [], []
    for q in queries:
        for r in web_search(q, n):
            u = r.get("href") or ""
            if not u or u in seen:
                continue
            seen.add(u)
            urls.append(u)
            chunks.append(f"[{len(urls)}] {r.get('title')}\n{u}\n{r.get('body')}")
        if len(urls) >= 12:
            break
    good = [u for u in urls if re.search(r"espn|bbc|wikipedia|worldfootball|soccerway|besoccer|transfermarkt|"
                                         r"livesport|flashscore|sofascore|fotmob|eurobasket|basketball-reference|"
                                         r"proballers|uefa|fifa|skysports|theguardian|reuters|apnews|nba\.com|"
                                         r"euroleague|lkl|lff|aiscore|sportscore|scoreaxis|footystats", u)]
    for u in good[:pages]:
        t = page_text(u)
        if t:
            chunks.append(f"[puslapis] {u}\n{t}")
    return "\n\n".join(chunks)[:16000], urls


# ---------------------------------------------------------------- MMA (UFC ir kt.) – ESPN kovų rezultatai
MMA_PATHS = ["mma/ufc", "mma/pfl", "mma/bellator"]


def mma_event(leg):
    """Kovos nugalėtojas iš ESPN (UFC, PFL, Bellator). Grąžina LiveScore formatą: laimėtojui 1, pralaimėtojui 0."""
    sp = _fold(f"{leg.get('sport')} {leg.get('league')}")
    if not re.search(r"\bmma\b|ufc|pfl|bellator|fight|boks|box", sp):
        return None
    k, c = _when(leg)
    best = None
    for path in MMA_PATHS:
        for d in (k.date(), k.date() - timedelta(days=1), k.date() + timedelta(days=1)):
            old = d < (datetime.now() - timedelta(days=2)).date()
            try:
                data = _get(f"{ESPN}/{path}/scoreboard?dates={d:%Y%m%d}", ttl=86400 if old else 900)
            except Exception:
                continue
            for ev in data.get("events") or []:
                for comp in ev.get("competitions") or []:
                    cs = comp.get("competitors") or []
                    if len(cs) != 2:
                        continue
                    nm = [((x.get("athlete") or {}).get("displayName") or (x.get("athlete") or {}).get("fullName") or "")
                          for x in cs]
                    ok, sw = _pair(leg, [nm[0]], [nm[1]])
                    if not ok:                            # kovotojai – užtenka pavardžių (vardai rašomi įvairiai)
                        ln = lambda x: [w for w in _fold(re.sub(r"\(.*?\)", "", x or "")).split() if len(w) >= 4][-1:]
                        h, a = ln(leg.get("home")), ln(leg.get("away"))
                        f0, f1 = _fold(nm[0]).split(), _fold(nm[1]).split()
                        if h and a and h[0] in f0 and a[0] in f1:
                            ok, sw = True, False
                        elif h and a and h[0] in f1 and a[0] in f0:
                            ok, sw = True, True
                    if not ok:
                        continue
                    try:
                        t = datetime.fromisoformat(str(comp.get("date") or ev.get("date")).replace("Z", "+00:00")) \
                            .astimezone().replace(tzinfo=None)
                    except Exception:
                        t = k
                    gap = abs((t - k).total_seconds())
                    if best is None or gap < best[0]:
                        best = (gap, comp, cs, nm, t, path, sw)
    if not best:
        return None
    _, comp, cs, nm, t, path, sw = best
    stt = ((comp.get("status") or {}).get("type") or {})
    if not stt.get("completed"):
        return {"_t1": nm[0], "_t2": nm[1], "Eps": "NS", "_sport": "mma", "_league": f"ESPN {path}", "_start": t,
                "_swapped": sw}
    w = [bool(x.get("winner")) for x in cs]
    if not any(w):
        return {"_t1": nm[0], "_t2": nm[1], "Tr1": 0, "Tr2": 0, "Eps": "FT", "_sport": "mma",
                "_league": f"ESPN {path} (lygiosios / be sprendimo)", "_start": t, "Ewt": 3, "_swapped": sw}
    return {"_t1": nm[0], "_t2": nm[1], "Tr1": 1 if w[0] else 0, "Tr2": 1 if w[1] else 0, "Eps": "FT",
            "_sport": "mma", "_league": f"ESPN {path}", "_start": t, "Ewt": 1 if w[0] else 2, "_swapped": sw}


# ---------------------------------------------------------------- LKL / KMT žaidėjų statistika (oficialus lkl.lt)
LKL = "https://lkl.lt"
_lkl_c = {}


def _lkl_html(path, ttl=900):
    hit = _lkl_c.get(path)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(LKL + path, headers={"User-Agent": HDR.get("User-Agent", "Mozilla/5.0"),
                                                      "Accept-Language": "lt"})
    with urllib.request.urlopen(req, timeout=25) as r:
        h = r.read().decode("utf-8", "ignore")
    _lkl_c[path] = (time.time(), h)
    return h


def _lkl_games():
    """[(id, namų, svečių)] – paskutinės LKL ir Karaliaus Mindaugo taurės rungtynės iš lkl.lt rezultatų puslapių."""
    out = []
    for path in ("/rezultatai", "/rezultatai?page=2", "/karaliaus-mindaugo-taure/rezultatai"):
        try:
            h = _lkl_html(path)
        except Exception:
            continue
        for chunk in h.split("battle-row")[1:]:
            alts = re.findall(r'alt="([^"]+)"', chunk[:3000])
            gid = re.search(r"/rungtynes/(\d+)", chunk[:3000])
            if len(alts) >= 2 and gid:
                out.append((gid.group(1), html.unescape(alts[0]), html.unescape(alts[1])))
    return out


def _lkl_game(leg):
    k, c = _when(leg)
    best = None
    for gid, h, a in _lkl_games():
        ok, sw = _pair(leg, [h], [a])
        if not ok:
            continue
        try:
            bx = _get(f"{LKL}/api/livestream/boxscore/{gid}", ttl=1800)
        except Exception:
            continue
        upd = [v.get("updated_at") for v in (bx.get("gameStatistics") or {}).values() if isinstance(v, dict)]
        try:
            t = datetime.fromisoformat(str(max(u for u in upd if u)).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except Exception:
            continue
        if not (k - timedelta(hours=6) <= t <= k + timedelta(hours=40)):
            continue                                       # kitos to paties poros rungtynės
        gap = abs((t - k).total_seconds())
        if best is None or gap < best[0]:
            best = (gap, gid, bx, t)
    return best


def _players_lkl(bx):
    out = []
    for side in ("home", "away"):
        for p in ((bx.get("boxscore") or {}).get(side) or {}).get("players") or []:
            slug = str(p.get("slug") or "")
            parts = slug.split("-") if slug else _fold((p.get("name") or {}).get("value")).split()
            val = lambda k: (p.get(k) or {}).get("value")
            try:
                v = {"pts": float(val("points") or 0), "reb": float(val("total_rebounds") or 0),
                     "ast": float(val("assists") or 0), "stl": float(val("steals") or 0),
                     "blk": float(val("blocks") or 0), "3pm": float(str(val("fg3") or "0/0").split("/")[0])}
            except Exception:
                v = {}
            out.append({"first": " ".join(parts[:-1]), "last": parts[-1] if parts else "",
                        "played": str(val("time") or "00:00") not in ("00:00", "0:00", ""), "v": v})
    return out
