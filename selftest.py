#!/usr/bin/env python3
"""Regresiniai testai – kiekviena kada nors padaryta klaida čia tampa testu, kad nepasikartotų.
Paleidžiama prieš kiekvieną atnaujinimą Raspberry (jei nepraeina – atnaujinimas atmetamas) ir prieš įkeliant į GitHub.
Jokio interneto: visi šaltiniai pakeičiami įrašytais tikrais duomenimis (fixtures)."""
import json, os, sys, tempfile, traceback, logging
from datetime import datetime, timedelta, date
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import urllib.request


def _no_net(*a, **k):
    raise RuntimeError("selftest: internetas draudžiamas")


urllib.request.urlopen = _no_net
import results as R
import props as P

FX = json.loads('{"nhl_score": {"games": [{"id": 2026020326, "startTimeUTC": "2026-11-16T00:30:00Z", "gameState": "OFF", "homeTeam": {"name": {"default": "Flyers"}, "abbrev": "PHI", "score": 2}, "awayTeam": {"name": {"default": "Oilers"}, "abbrev": "EDM", "score": 5}, "gameOutcome": {"lastPeriodType": "REG"}, "goals": [{"period": 1, "periodDescriptor": {"periodType": "REG"}, "teamAbbrev": "EDM"}, {"period": 1, "periodDescriptor": {"periodType": "REG"}, "teamAbbrev": "EDM"}, {"period": 1, "periodDescriptor": {"periodType": "REG"}, "teamAbbrev": "EDM"}, {"period": 2, "periodDescriptor": {"periodType": "REG"}, "teamAbbrev": "PHI"}, {"period": 2, "periodDescriptor": {"periodType": "REG"}, "teamAbbrev": "PHI"}, {"period": 3, "periodDescriptor": {"periodType": "REG"}, "teamAbbrev": "EDM"}, {"period": 3, "periodDescriptor": {"periodType": "REG"}, "teamAbbrev": "EDM"}]}]}, "nhl_box": {"gameState": "OFF", "playerByGameStats": {"homeTeam": {"forwards": [{"name": {"default": "T. Konecny"}, "goals": 0, "assists": 0, "points": 0, "sog": 6, "hits": 0, "blockedShots": 0, "toi": "18:00"}], "defense": [], "goalies": [{"name": {"default": "D. Vladar"}, "saves": 16, "toi": "58:15"}]}, "awayTeam": {"forwards": [{"name": {"default": "L. Draisaitl"}, "goals": 2, "assists": 1, "points": 3, "sog": 3, "hits": 0, "blockedShots": 0, "toi": "18:00"}, {"name": {"default": "C. McDavid"}, "goals": 1, "assists": 2, "points": 3, "sog": 4, "hits": 0, "blockedShots": 0, "toi": "18:00"}], "defense": [], "goalies": [{"name": {"default": "T. Jarry"}, "saves": 34, "toi": "60:00"}]}}}, "lkl_box": {"gameStatistics": {"3": {"updated_at": "2026-11-15T16:55:12.000000Z", "points": 87}, "5": {"updated_at": "2026-11-15T16:55:12.000000Z", "points": 82}}, "boxscore": {"home": {"players": [{"slug": "deividas-sirvydis", "name": {"value": "D. Sirvydis"}, "time": {"value": "20:31"}, "points": {"value": 12}, "total_rebounds": {"value": 2}, "assists": {"value": 1}, "steals": {"value": 0}, "blocks": {"value": 0}, "fg3": {"value": "1/5"}}, {"slug": "marek-blazevic", "name": {"value": "M. Blaževič"}, "time": {"value": "13:52"}, "points": {"value": 6}, "total_rebounds": {"value": 4}, "assists": {"value": 1}, "steals": {"value": 0}, "blocks": {"value": 0}, "fg3": {"value": "0/0"}}, {"slug": "majus-bulanovas", "name": {"value": "M. Bulanovas"}, "time": {"value": "00:00"}, "points": {"value": 0}, "total_rebounds": {"value": 0}, "assists": {"value": 1}, "steals": {"value": 0}, "blocks": {"value": 0}, "fg3": {"value": "0/0"}}]}, "away": {"players": [{"slug": "martynas-echodas", "name": {"value": "M. Echodas"}, "time": {"value": "27:35"}, "points": {"value": 7}, "total_rebounds": {"value": 9}, "assists": {"value": 1}, "steals": {"value": 0}, "blocks": {"value": 0}, "fg3": {"value": "0/0"}}]}}}, "ls_hockey": {"Eid": "1835377", "Tr1": "1", "Tr2": "4", "Tr1OR": "1", "Tr2OR": "4", "Tr1Pe1": "0", "Tr2Pe1": "2", "Tr1Pe2": "1", "Tr2Pe2": "0", "Tr1Pe3": "0", "Tr2Pe3": "2", "T1": [{"Nm": "Montreal Canadiens"}], "T2": [{"Nm": "Colorado Avalanche"}], "Eps": "FT", "Ewt": 2, "Esd": 20261115020000}}')
FX.update(json.loads('{"s365_soc": {"games": [{"id": 4891453, "startTime": "2026-11-13T17:00:00+02:00", "statusText": "Ended", "competitionDisplayName": "Friendly International", "homeCompetitor": {"id": 5078, "name": "Iraq", "score": 0}, "awayCompetitor": {"id": 5088, "name": "Lebanon", "score": 0}}, {"id": 4697788, "startTime": "2026-11-13T21:45:00+02:00", "statusText": "Ended", "competitionDisplayName": "UEFA Nations League - League D - Group 1", "homeCompetitor": {"id": 5052, "name": "Andorra", "score": 2}, "awayCompetitor": {"id": 12775, "name": "Gibraltar", "score": 0}}]}, "s365_stats": {"statistics": [{"name": "Total Shots", "competitorId": 5052, "value": "8"}, {"name": "Shots On Target", "competitorId": 5052, "value": "3"}, {"name": "Corners", "competitorId": 5052, "value": "3"}, {"name": "Red Cards", "competitorId": 5052, "value": "0"}, {"name": "Yellow Cards", "competitorId": 5052, "value": "2"}, {"name": "Total Shots", "competitorId": 12775, "value": "8"}, {"name": "Shots On Target", "competitorId": 12775, "value": "1"}, {"name": "Corners", "competitorId": 12775, "value": "2"}, {"name": "Red Cards", "competitorId": 12775, "value": "0"}, {"name": "Yellow Cards", "competitorId": 12775, "value": "5"}]}, "s365_bas": {"games": [{"id": 4810395, "startTime": "2026-11-14T17:00:00+02:00", "statusText": "Final", "competitionDisplayName": "Super Ligi", "homeCompetitor": {"id": 54405, "name": "Manisa Basket", "score": 82}, "awayCompetitor": {"id": 972, "name": "Galatasaray", "score": 81}}]}, "s365_bas_game": {"game": {"stages": [{"shortName": "Q1", "homeCompetitorScore": 20, "awayCompetitorScore": 14}, {"shortName": "Q2", "homeCompetitorScore": 24, "awayCompetitorScore": 23}, {"shortName": "Q3", "homeCompetitorScore": 26, "awayCompetitorScore": 23}, {"shortName": "Q4", "homeCompetitorScore": 12, "awayCompetitorScore": 21}, {"shortName": "Score", "homeCompetitorScore": 82, "awayCompetitorScore": 81}]}}}'))
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def ls_event(raw, sport, league="X"):
    e = dict(raw)
    e["_t1"], e["_t2"] = e["T1"][0]["Nm"], e["T2"][0]["Nm"]
    e["_league"], e["_sport"] = league, sport
    e["_start"] = datetime.strptime(str(e["Esd"]), "%Y%m%d%H%M%S")
    return e


def ev(t1, t2, s1, s2, sport="soccer", start=datetime(2026, 11, 15, 18), **kw):
    return dict({"_t1": t1, "_t2": t2, "Tr1": s1, "Tr2": s2, "Eps": "FT", "_sport": sport, "_league": "X",
                 "_start": start, "Esd": 1, "Ewt": 1 if s1 > s2 else 2 if s2 > s1 else 3}, **kw)


def st(r):
    return r[0] if isinstance(r, tuple) else r


# ---------------------------------------------------------------- rinkų supratimas
@test
def btts_not_player_prop():
    for pk in ("Both Teams to Score – YES", "Both to score - No", "Abi komandos pelnys įvartį – Taip"):
        assert not P.is_prop({"pick": pk, "home": "Leixoes", "away": "Chaves"}), pk
    e = ev("Leixoes", "Chaves", 2, 0)
    assert st(R.evaluate({"home": "Leixoes", "away": "Chaves", "pick": "Both Teams to Score – YES", "side": "Kita",
                          "sport": "soccer"}, e)) == "lost"
    assert st(R.evaluate({"home": "Leixoes", "away": "Chaves", "pick": "Both to score - No", "side": "Kita",
                          "sport": "soccer"}, e)) == "won"


@test
def team_win_and_total():
    e = ev("Philadelphia Flyers", "Edmonton Oilers", 2, 5, "hockey")
    b = {"home": "Philadelphia Flyers", "away": "Edmonton Oilers", "pick": "Edmonton Oilers & Over 5.5",
         "side": "Win", "team": "Edmonton Oilers", "sport": "hockey"}
    assert st(R.evaluate(b, e)) == "won"
    assert st(R.evaluate(dict(b, pick="Edmonton Oilers & Over 7.5"), e)) == "lost"


@test
def win_to_nil_and_asian_whole_line():
    e = ev("Scotland", "North Macedonia", 2, 1)
    assert st(R.evaluate({"home": "Scotland", "away": "North Macedonia", "pick": "Scotland will win to nil - Yes",
                          "sport": "soccer"}, e)) == "lost"
    assert st(R.evaluate({"home": "Scotland", "away": "North Macedonia", "pick": "Under 3.0 Total Goals",
                          "side": "Under", "line": 3.0, "sport": "soccer"}, e)) == "push"


@test
def hockey_period_uses_period_score():
    e = ls_event(FX["ls_hockey"], "hockey", "NHL")
    b = {"home": "Montreal Canadiens", "away": "Colorado Avalanche", "sport": "hockey",
         "pick": "1st period over 1.5 goals", "side": "Over", "line": 1.5}
    r = R.evaluate(b, e)
    assert st(r) == "won" and "0:2" in r[1], r
    assert st(R.evaluate(dict(b, pick="1st period over 2.5 goals", line=2.5), e)) == "lost"


@test
def basketball_quarter_and_single_team_total():
    e = ev("Mersin BSB", "Kecioren", 80, 70, "basketball", Tr1Q2=20, Tr2Q2=15, Tr1Q1=20, Tr2Q1=20, Tr1Q3=20,
           Tr2Q3=20, Tr1Q4=20, Tr2Q4=15)
    assert R._period_text({"pick": "Mersin BSB 2 kelinys - nugalėtojas", "sport": "basketball"}) == "q2"
    assert R._period_text({"pick": "2 kėlinys daugiau 1.5", "sport": "soccer"}) == "h2"
    e2 = ev("Neptunas", "Zalgiris", 80, 95, "basketball")
    assert st(R.evaluate({"home": "Neptūnas", "sport": "basketball", "pick": "under 85.5", "side": "Under",
                          "line": 85.5}, e2)) == "won"


@test
def ambiguous_stat_stays_unknown():
    leg = {"sport": "basketball", "league": "NBA", "home": "Phoenix Suns", "away": "Los Angeles Lakers",
           "subject": "Devin Booker", "pick": "Devin Booker (PHX) daugiau 1.5", "line": 1.5, "side": "Over",
           "kickoff": "2026-11-16T04:00"}
    r = P._settle(leg)
    assert r[0] is None and "neaišku" in r[2], r          # geriau „nežinoma“, nei spėti statistiką


@test
def not_lose_is_double_chance():
    e = ev("Kosovo", "Israel", 0, 0)
    b = {"home": "Kosovo", "away": "Israel", "pick": "Izraelis nepralaimes @2.0", "side": "Win", "team": "Israel",
         "sport": "soccer"}
    assert st(R.evaluate(b, e)) == "won"
    assert st(R.evaluate(dict(b, pick="Izraelis @4.5"), e)) == "lost"
    assert st(R.evaluate(b, ev("Kosovo", "Israel", 1, 0))) == "lost"


@test
def team_subject_is_not_player():
    leg = {"sport": "basketball", "home": "PAO", "away": "Barca", "subject": "PAO vs Barca",
           "pick": "PAO vs Barca total under 83.5", "side": "Under", "line": 83.5}
    assert R.team_subject(leg) and not P.is_prop(leg)
    leg2 = dict(leg, subject="PAO vs Barca total")
    assert R.team_subject(leg2) and not P.is_prop(leg2)
    assert not R.team_subject({"home": "Phoenix Suns", "away": "Lakers", "subject": "Devin Booker"})


# ---------------------------------------------------------------- laikas: LIVE tinka, po rungtynių – ne
@test
def late_rules():
    b = {"created": "2026-11-15T21:30", "kickoff": "2026-11-15T21:00", "sport": "soccer"}
    assert R.late(b, {"_start": datetime(2026, 11, 15, 20, 0), "Esd": 1, "_sport": "soccer"}) is None   # LIVE
    assert R.late(b, {"_start": datetime(2026, 11, 15, 19, 0), "Esd": 1, "_sport": "soccer"})           # jau baigėsi
    assert R.late(b, {"_start": datetime(2026, 11, 15, 18, 0), "_sport": "soccer", "_league": "X"}) is None  # +2 h atsarga


# ---------------------------------------------------------------- rungtynių paieška
@test
def find_prefers_unfinished_and_mislabeled_opponent():
    evs = {("hockey", date(2026, 11, 15)): [dict(ev("Montreal Canadiens", "Colorado Avalanche", 1, 4, "hockey",
                                                       datetime(2026, 11, 15, 2)))],
           ("hockey", date(2026, 11, 17)): [dict(ev("New York Rangers", "Montreal Canadiens", 0, 0, "hockey",
                                                       datetime(2026, 11, 17, 2, 30)), Eps="NS")],
           ("basketball", date(2026, 11, 14)): [ev("Manisa BB", "Galatasaray", 82, 81, "basketball",
                                                   datetime(2026, 11, 14, 17))]}
    old = R.day
    R.day = lambda sport, d: evs.get((sport, d), [])
    try:
        e = R.find({"home": "Montreal Canadiens", "sport": "hockey", "pick": "Montreal Canadiens win",
                    "kickoff": "2026-11-15T17:20", "created": "2026-11-15T17:20"})
        assert e and e["_t1"] == "New York Rangers", e
        b = {"home": "Kiorfez Basket", "away": "Galatasaray", "sport": "basketball", "side": "Win",
             "team": "Galatasaray", "pick": "Galatasaray Nugalėtojas (+OT)", "kickoff": "2026-11-14T11:31",
             "created": "2026-11-14T11:31"}
        e = R.find(b)
        assert e and e["_t1"] == "Manisa BB" and not e["_swapped"], e
        assert st(R.evaluate(b, e)) == "lost"
    finally:
        R.day = old


# ---------------------------------------------------------------- statistikos šaltiniai
@test
def lkl_player_points():
    P._lkl_games = lambda: [("11625", "Žalgiris", "Neptūnas")]
    P._el_game = lambda leg: None
    old = P._get
    P._get = lambda url, ttl=0: FX["lkl_box"]
    try:
        base = {"sport": "basketball", "league": "LKL", "home": "Zalgiris", "away": "Neptunas",
                "kickoff": "2026-11-15T16:30", "side": "Over"}
        assert st(P._settle(dict(base, subject="D. Sirvydis", pick="D. Sirvydis over 8.5", line=8.5))) == "won"
        assert st(P._settle(dict(base, subject="M. Blazevic", pick="M. Blazevic over 10.5", line=10.5))) == "lost"
        assert st(P._settle(dict(base, subject="M. Bulanovas", pick="M. Bulanovas over 2.5 points", line=2.5))) == "push"
    finally:
        P._get = old


@test
def nhl_official_source():
    old = P._get

    def g(url, ttl=0):
        if "/score/2026-11-15" in url:
            return FX["nhl_score"]
        if "/boxscore" in url:
            return FX["nhl_box"]
        return {"games": []}
    P._get = g
    try:
        leg = {"sport": "hockey", "league": "NHL", "home": "Philadelphia Flyers", "away": "Edmonton Oilers",
               "kickoff": "2026-11-16T02:30", "created": "2026-11-15T20:00"}
        e = P.nhl_event(leg)
        assert e and e["Tr1"] == 2 and e["Tr2"] == 5 and (e["Tr1Pe1"], e["Tr2Pe1"]) == (0, 3), e
        assert st(P._settle(dict(leg, subject="Leon Draisaitl", pick="Leon Draisaitl over 1.5 points", line=1.5,
                                 side="Over"))) == "won"
        assert st(P._settle(dict(leg, subject="Connor McDavid", pick="Connor McDavid anytime goalscorer"))) == "won"
        assert st(P._settle(dict(leg, subject="Tristan Jarry", pick="Tristan Jarry over 30.5 saves", line=30.5,
                                 side="Over"))) == "won"
        assert st(P._settle(dict(leg, subject="Travis Konecny", pick="Travis Konecny over 3.5 shots on goal",
                                 line=3.5, side="Over"))) == "won"
    finally:
        P._get = old


@test
def nfl_player_yards():
    summ = {"boxscore": {"players": [{"statistics": [
        {"name": "rushing", "labels": ["CAR", "YDS", "AVG", "TD", "LONG"],
         "athletes": [{"athlete": {"displayName": "James Cook"}, "stats": ["18", "104", "5.8", "1", "22"]}]}]}]}}
    oe, og = P._espn_game, P._get
    P._espn_game = lambda leg, paths: ("football/nfl", {"id": 1, "competitions": [{"status": {"type": {"state": "post"}}}]})
    P._get = lambda url, ttl=0: summ
    try:
        leg = {"sport": "american_football", "league": "NFL", "home": "New York Jets", "away": "Buffalo Bills",
               "subject": "James Cook", "kickoff": "2026-11-15T20:00"}
        assert P.is_prop(dict(leg, pick="James Cook daugiau 86,5 bėgimo jardo"))
        assert st(P._settle(dict(leg, pick="James Cook daugiau 86,5 bėgimo jardo", line=86.5, side="Over"))) == "won"
        assert st(P._settle(dict(leg, pick="James Cook anytime touchdown"))) == "won"
    finally:
        P._espn_game, P._get = oe, og


@test
def soccer_goal_order_and_scorer():
    su = {"keyEvents": [{"scoringPlay": True, "team": {"displayName": t}, "type": {"text": "Goal"},
                         "participants": [{"athlete": {"displayName": p}}]}
                        for t, p in [("England", "Harry Kane"), ("England", "Bukayo Saka"), ("England", "Harry Kane"),
                                     ("Spain", "Lamine Yamal"), ("Spain", "Pedri")]]}
    oe, og = P._espn_game, P._get
    P._espn_game = lambda leg, paths: ("soccer/all", {"id": 1, "competitions": [{"status": {"type": {"state": "post"}}}]})
    P._get = lambda url, ttl=0: su
    try:
        base = {"home": "Spain", "away": "England", "sport": "soccer", "kickoff": "2026-11-15T21:45"}
        assert st(P._settle(dict(base, pick="Spain to score 5th goal"))) == "won"
        assert st(P._settle(dict(base, pick="Spain to score first goal"))) == "lost"
        assert st(P._settle(dict(base, pick="England to score 6th goal"))) == "lost"
        assert st(P._settle(dict(base, pick="Anytime goalscorer Harry Kane", subject="Harry Kane"))) == "won"
        assert st(P._settle(dict(base, pick="Anytime goalscorer Michael Olise", subject="Michael Olise"))) == "lost"
    finally:
        P._espn_game, P._get = oe, og


@test
def mma_fighter_order():
    data = {"events": [{"date": "2026-11-15T03:00Z", "competitions": [{"date": "2026-11-15T05:00Z",
            "status": {"type": {"completed": True}},
            "competitors": [{"athlete": {"displayName": "Josh Hokit"}, "winner": False},
                            {"athlete": {"displayName": "Ciryl Gane"}, "winner": True}]}]}]}
    og = P._get
    P._get = lambda url, ttl=0: data if "ufc" in url else {}
    try:
        leg = {"sport": "mma", "home": "Cyril Gane", "away": "Josh Hokit", "kickoff": "2026-11-14T23:00",
               "created": "2026-11-14T19:41", "side": "Win", "team": "Cyril Gane", "pick": "Cyril Gane"}
        e = P.mma_event(leg)
        assert e and e["_swapped"] and st(R.evaluate(leg, e)) == "won"
        assert st(R.evaluate(dict(leg, team="Josh Hokit", pick="Josh Hokit"), e)) == "lost"
    finally:
        P._get = og


def _mock365(url, ttl=0):
    if "allscores" in url and "sports=1" in url and "13/11/2026" in url:
        return FX["s365_soc"]
    if "allscores" in url and "sports=2" in url and "14/11/2026" in url:
        return FX["s365_bas"]
    if "/game/stats/" in url:
        return FX["s365_stats"]
    if "/web/game/" in url:
        return FX["s365_bas_game"]
    return {"games": []}


@test
def s365_cards_corners_and_quarters():
    og, oe = P._get, P._espn_game
    P._get, P._espn_game = _mock365, (lambda leg, paths: (None, None))
    try:
        base = {"home": "Andorra", "away": "Gibraltar", "sport": "soccer", "kickoff": "2026-11-13T21:45",
                "created": "2026-11-13T14:35"}
        r = P._settle(dict(base, pick="Gibraltar gaus daugiau geltonų kortelių", team="Gibraltar"))
        assert st(r) == "won" and "2 – 5" in r[1], r                     # geltonos 2 – 5
        assert st(P._settle(dict(base, pick="Over 4.5 corners", side="Over", line=4.5))) == "won"   # 3 + 2
        assert st(P._settle(dict(base, pick="Andorra over 3.5 corners", side="Over", line=3.5, team="Andorra"))) == "lost"
        leg = {"home": "Manisa", "away": "Galatasaray", "sport": "basketball", "kickoff": "2026-11-14T17:00",
               "created": "2026-11-14T11:31"}
        e = P.s365_event(leg)
        assert e and (e["Tr1Q2"], e["Tr2Q2"]) == (24, 23) and e["Tr2"] == 81, e
        assert st(R.evaluate(dict(leg, pick="Manisa 2 kelinys - nugalėtojas", side="Win", team="Manisa"), e)) == "won"
    finally:
        P._get, P._espn_game = og, oe


@test
def utma_77lt_parser():
    import io
    page = ('<div class="l77-game is-final l77-game--combat"><div class="l77-game-when"><b>Št vas. 21</b></div>'
            '<div class="l77-gt l77-gt--home is-win"><span class="l77-gt-name">Nikola Cvetkovič</span></div>'
            '<span class="l77-method l77-method--ko"><b>TKO</b></span><div class="l77-gt l77-gt--away">'
            '<span class="l77-gt-name">Nauris Lukošiūnas</span></div></div>')

    class F(io.BytesIO):
        def __enter__(s):
            return s

        def __exit__(s, *a):
            pass
    old = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=0: F(page.encode())
    P.UTMA_STATE["off_until"] = 0
    try:
        leg = {"sport": "mma", "home": "Nauris Lukošiūnas", "away": "Nikola Cvetkovič", "side": "Win",
               "team": "Nikola Cvetkovič", "pick": "Cvetkovič", "kickoff": f"{datetime.now().year}-02-21T19:00",
               "created": f"{datetime.now().year}-02-20T10:00"}
        e = P.utma_event(leg)
        assert e and e["_swapped"] and st(R.evaluate(leg, e)) == "won", e
    finally:
        urllib.request.urlopen = old


@test
def bookmaker_market_mapping():
    m = {"h": "Scotland", "a": "North Macedonia"}
    w = lambda **k: P._wanted(dict({"home": "Scotland", "away": "North Macedonia", "sport": "soccer"}, **k), m, False)
    assert w(side="Kita", pick="Scotland will win to nil - Yes") is None
    assert w(side="Kita", pick="Both Teams to Score – YES") == ("Both Teams Score", ["Yes"])
    assert w(side="DC", pick="1X") == ("Double Chance", ["Home/Draw"])
    assert P._wanted({"home": "North Macedonia", "away": "Scotland", "side": "DC", "pick": "1X", "sport": "soccer"},
                     m, True) == ("Double Chance", ["Draw/Away"])


# ---------------------------------------------------------------- auditorius
@test
def group_marks():
    import pi_audit as A
    assert A._mark("Prognozė: Galatasaray 88:78.\n\n✖️82-81")["st"] == "lost"
    assert A._mark("✅ Pasirinkimas\nArsenal laimės\nSėkmės!") is None
    assert A._mark("Lakers ML\n✅ WIN +0.9u")["st"] == "won"
    assert A._mark("✅✅", whole=True)["st"] == "won"
    assert A._mark("Kada rungtynės?", whole=True) is None


@test
def auditor_summary_smoke():
    import pi_audit as A
    d = tempfile.mkdtemp()
    from pathlib import Path
    a = A.Auditor(None, {"groups": ["GURU G⚽AL"]}, logging.getLogger("t"), Path(d), Path(d))
    a.d["chats"], a.d["kind"] = {"c1": "GURU G⚽AL", "c2": "Chat"}, {"c1": "channel", "c2": "group"}
    now = datetime.now()
    a.d["bets"] = [{"c": "c1", "mid": 1, "t": (now - timedelta(hours=3)).isoformat(timespec="minutes"),
                    "status": "won", "tries": 0, "next": 0, "odds": 1.9,
                    "legs": [{"home": "A", "away": "B", "pick": "1", "status": "won", "detail": "x"}]}]
    s = a._summary()
    for k in ("total", "today_total", "leaders", "groups", "recent", "signal", "bet_total"):
        assert k in s, k
    assert s["signal"] == ["c1"] and s["total"]["w"] == 1


@test
def ai_verifier_flags_doubts():
    import pi_audit as A
    from pathlib import Path
    d = tempfile.mkdtemp()
    a = A.Auditor(None, {"groq_key": "x"}, logging.getLogger("t"), Path(d), Path(d))
    now = datetime.now().isoformat(timespec="minutes")
    a.d["bets"] = [{"c": "c1", "mid": 1, "t": now, "status": "won", "legs": [
        {"home": "A", "away": "B", "pick": "1st period over 1.5", "status": "won", "detail": "įvarčių: 5 (visos rungtynės)"}]}]
    old = A.groq_json
    A.groq_json = lambda cfg, prompt, log=None, timeout=90: {"res": [{"i": 0, "ok": False, "why": "vertinta visos rungtynės"}]}
    try:
        a.verify_some()
    finally:
        A.groq_json = old
    b = a.d["bets"][0]
    assert b["vf"] is False and b["legs"][0]["doubt"] and a._summary()["quality"]["doubts"] == 1


@test
def alerts_only_for_followed():
    import pi_audit as A
    from pathlib import Path
    d = tempfile.mkdtemp()
    a = A.Auditor(None, {}, logging.getLogger("t"), Path(d), Path(d))
    a.d["chats"], a.d["kind"] = {"c1": "Chat", "c2": "Kitas"}, {"c1": "group", "c2": "group"}
    sent = []
    a.notify = lambda title, msg, legs: sent.append(title)
    a.settings = {"follow": ["m:c1:77"], "auto_proven": False}
    now = datetime.now()
    kick = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    mk = lambda c, u: {"c": c, "mid": 1, "u": u, "un": "Jonas", "t": now.astimezone().isoformat(timespec="minutes"),
                       "status": "pending", "legs": [{"home": "A", "away": "B", "pick": "1", "kickoff": kick}]}
    a._alert([mk("c1", "77"), mk("c1", "88"), mk("c2", "77")])
    assert len(sent) == 1, sent
    a.settings = {"follow": ["g:c2"]}
    sent.clear()
    a._alert([mk("c1", "77"), mk("c2", "5")])
    assert len(sent) == 1, sent


@test
def dashboard_has_required_parts():
    h = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dash.html"), encoding="utf-8").read()
    for x in ("fetch('/api'", "id=\"dayTable\"", "id=\"sigCard\"", "id=\"qLog\"", "toggleFollow", "id=\"n_guru\"", "Powered by SportScore", "function renderBets",
              "post('/settings'"):
        assert x in h, x


def main():
    bad = 0
    for fn in TESTS:
        try:
            fn()
            print("✓", fn.__name__)
        except Exception:
            bad += 1
            print("✗", fn.__name__)
            traceback.print_exc(limit=3)
    print(f"{len(TESTS) - bad}/{len(TESTS)} testų praėjo")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
