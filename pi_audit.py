"""Grupių auditas fone (Raspberry): kurios Telegram lažybų grupės iš tikrųjų pelningos.

Grupės dažnai pasigiria tik laimėjimais („✅ win“), o pralaimėję tyli. Todėl:
  * imami tik PATYS signalai (žinutė su statymu, paskelbta prieš rungtynes),
  * rezultatai NEimami iš grupės „win/✅“ žinučių – kiekvienas statymas tikrinamas pagal tikrą
    rungtynių rezultatą internete (LiveScore; Sofascore blokuoja automatines užklausas),
  * skaičiuojama: kiek laimėta / pralaimėta, pataikymo %, ROI statant po 1 vnt.
Dirba lėtai ir taupiai: AI (Google, debesyje) naudojamas ribotai per dieną, kad liktų gyviems signalams.
"""
import asyncio
import base64
import json
import math
import os
import re
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import subprocess
import time
import urllib.error
import unicodedata
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import results

try:
    import props                                  # žaidėjų / kampinių statistika (EuroLeague, ESPN)
except Exception:
    props = None

BET_RE = re.compile(r"(?i)(@\s?\d+[.,]\d{1,2}\b|\b(koef|kof|odds?|pick|statym|stake|over|under|btts|abi įmuš|"
                    r"laimės|handicap|tot(al)?|1x2|\bx2\b|\b1x\b|w1|w2|p1|p2)\b|\b[1-9][.,]\d{2}\b)")
RESULT_ONLY = re.compile(r"(?i)^[\s✅❌🟢🔴✔️✖️💰🔥🎉👏+\-]*(win|won|laimėj|pataik|lose|lost|pralaim|cash|grąžin|void|"
                         r"✅|❌)[\s\S]{0,60}$")
AD_RE = re.compile(r"(?i)(vip|prenumer|subscribe|join|prisijunk|promo|bonus|nuolaid|kaina|price|€\s?\d+\s?/\s?mėn)")

PROMPT = """Tai žinutė iš Telegram lažybų grupės (tekstas ir/ar kupono nuotrauka).
Grąžink TIK JSON:
{"is_bet": true/false, "combo": true/false, "total_odds": skaičius arba null,
 "bets": [{"sport": "soccer/basketball/tennis/hockey/kita", "league": "...", "home": "...", "away": "...",
   "date": "YYYY-MM-DD" arba null, "time": "HH:MM" arba null,
   "side": "Win|DC|BTTS|Over|Under|Spread|Kita", "team": "kuriai komandai (Win/Spread/komandos total) arba null",
   "line": skaičius arba null, "pick": "statymas originaliai", "odds": skaičius arba null,
   "women": true/false}]}
is_bet=true TIK jei siūlomas naujas konkretus statymas prieš rungtynes ar gyvai. NE statymas: reklama, VIP pardavimas,
pasigyrimas laimėjimu, rezultato pranešimas („✅ win“), jau laimėtas/pralaimėtas kuponas, statistika.
Lygiosios = side "Win", team "Draw". „Abi įmuš“ = BTTS (pick „Yes“/„No“). „1X“, „X2“, „12“ = DC.
Komandas rašyk angliškai. NIEKO NESUGALVOK – ko nėra, rašyk null."""


def _n(x):
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", str(x or "")).lower())


def _who(m):
    s = getattr(m, "sender", None)
    if s is None:
        return None
    name = " ".join(x for x in (getattr(s, "first_name", None), getattr(s, "last_name", None)) if x)
    return (name or getattr(s, "title", None) or getattr(s, "username", None) or str(m.sender_id))[:40]


def _units(b):
    try:
        o = float(b.get("odds") or 0)
    except Exception:
        o = 0
    if o <= 1.01:
        return None
    return (o - 1) if b["status"] == "won" else (-1.0 if b["status"] == "lost" else 0.0)


def _bettable(bets):
    """Tik tie statymai, kuriuos buvo galima realiai pastatyti prieš rungtynes – su bukmekerių koeficientu."""
    out = []
    for b in bets:
        m = b.get("mkt")
        if isinstance(m, dict) and m.get("odds"):
            out.append(dict(b, odds=m["odds"]))
    return out


def _leaders(bets, chats, kind):
    """Pelningiausia grupė ir taikliausias žmogus pokalbių grupėse (tik patikrinti statymai)."""
    gs, ms = {}, {}
    for b in bets:
        if b["status"] not in ("won", "lost", "push"):
            continue
        gs.setdefault(b["c"], []).append(b)
        if kind.get(b["c"]) == "group" and b.get("u"):
            ms.setdefault((b["c"], str(b["u"])), []).append(b)
    def pack(lst, extra):
        s = _stats(lst)
        return {**extra, "n": s["n"], "w": s["w"], "l": s["l"], "p": s["p"], "hit": s["hit"], "roi": s["roi"],
                "units": s["units"], "n_odds": s["n_odds"]}
    groups = [pack(l, {"cid": c, "chat": chats.get(c, c)}) for c, l in gs.items()]
    members = [pack(l, {"cid": c, "chat": chats.get(c, c), "u": u,
                        "name": next((x.get("un") for x in l if x.get("un")), u)}) for (c, u), l in ms.items()]
    bg = max(groups, key=lambda g: (g["units"], g["hit"] or 0, g["n"]), default=None)
    bm = max(members, key=lambda m: (m["w"], m["units"], m["hit"] or 0), default=None)
    return {"group": bg if bg and bg["n"] else None, "member": bm if bm and bm["w"] else None,
            "groups": sorted(groups, key=lambda g: -g["units"])[:30],
            "members": sorted(members, key=lambda m: (-m["w"], -m["units"]))[:30]}


def _stats(bets):
    """Pataikymas, ROI (po 1 vnt.), vid. koef., serijos, didžiausias nuosmukis."""
    r = {"n": 0, "w": 0, "l": 0, "p": 0, "pend": 0, "unk": 0, "units": 0.0, "n_odds": 0, "odds_sum": 0.0,
         "best_streak": 0, "worst_streak": 0, "max_dd": 0.0}
    cur = peak = 0.0
    sw = sl = 0
    for b in sorted(bets, key=lambda x: x["t"]):
        st = b["status"]
        if st == "pending":
            r["pend"] += 1
            continue
        if st == "unknown":
            r["unk"] += 1
            if any("ne prognozė" in (l.get("detail") or "") for l in b.get("legs") or []):
                r["late"] = r.get("late", 0) + 1          # „prognozė“ paskelbta po rungtynių
            continue
        r["n"] += 1
        r[{"won": "w", "lost": "l"}.get(st, "p")] += 1
        if st == "won":
            sw, sl = sw + 1, 0
        elif st == "lost":
            sl, sw = sl + 1, 0
        r["best_streak"], r["worst_streak"] = max(r["best_streak"], sw), max(r["worst_streak"], sl)
        u = _units(b)
        if u is not None:
            r["n_odds"] += 1
            r["odds_sum"] += float(b["odds"])
            r["units"] += u
            cur += u
            peak = max(peak, cur)
            r["max_dd"] = max(r["max_dd"], peak - cur)
    wl = r["w"] + r["l"]
    r["hit"] = round(r["w"] / wl * 100) if wl else None
    r["roi"] = round(r["units"] / r["n_odds"] * 100, 1) if r["n_odds"] else None
    r["avg_odds"] = round(r["odds_sum"] / r["n_odds"], 2) if r["n_odds"] else None
    r["units"], r["max_dd"] = round(r["units"], 2), round(r["max_dd"], 2)
    r["reliable"] = r["n_odds"] >= 30
    # atsargus įvertis: Wilson apatinė pataikymo riba (80 % vienpusė) × vid. koef. – ar pelnas ne atsitiktinis
    if wl and r["avg_odds"]:
        z, ph = 0.84, r["w"] / wl
        lb = (ph + z * z / (2 * wl) - z * math.sqrt(ph * (1 - ph) / wl + z * z / (4 * wl * wl))) / (1 + z * z / wl)
        r["roi_low"] = round((lb * r["avg_odds"] - 1) * 100, 1)
    else:
        r["roi_low"] = None
    r["proven"] = bool(r["reliable"] and r["roi_low"] is not None and r["roi_low"] > 0)
    del r["odds_sum"]
    return r


_GROQ_M = {"t": 0, "ids": []}


def _groq_models(key, vision, cfg, log=None, n=2):
    """Groq modelių sąrašas keičiasi – kartą per 6 val. paklausiam, kurie yra, ir parenkam tinkamus."""
    if time.time() - _GROQ_M["t"] > 6 * 3600:
        try:
            req = urllib.request.Request("https://api.groq.com/openai/v1/models",
                                         headers={"Authorization": f"Bearer {key}", "User-Agent": "guru-pi/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                _GROQ_M["ids"] = [m["id"] for m in json.loads(r.read().decode())["data"] if m.get("active", True)]
            _GROQ_M["t"] = time.time()
            if log:
                log.info("Groq modeliai: %s", ", ".join(_GROQ_M["ids"])[:300])
        except Exception as e:
            if log:
                log.info("Groq modelių sąrašas nepavyko: %s", e)
    ids = _GROQ_M["ids"]
    if vision:
        pref = cfg.get("groq_vision_models") or []
        cand = [m for m in pref if m in ids] + [m for m in ids if re.search(r"(?i)scout|maverick|vision|vl\b|llama-4", m)]
    else:
        pref = cfg.get("groq_text_models") or ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
                                               "llama-3.1-8b-instant"]
        cand = [m for m in pref if m in ids] + [m for m in ids if re.search(r"(?i)llama|gpt-oss|qwen|kimi", m)
                                                 and not re.search(r"(?i)guard|whisper|tts|prompt", m)]
    if not ids:                                     # sąrašo gauti nepavyko – bandom numatytuosius
        cand = [] if vision else ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    return list(dict.fromkeys(cand))[:n]


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def groq_call(cfg, prompt, image, timeout=25, log=None):
    """Groq (OpenAI suderinamas). Grąžina dict, "quota" (429) arba None."""
    key = cfg.get("groq_key")
    if not key:
        return None
    models = _groq_models(key, bool(image), cfg, log)
    if not models:
        return None
    content = prompt if not image else [{"type": "text", "text": prompt}]
    if image:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}})
    for model in models:
        for json_mode in (True, False):
            body = {"model": model, "messages": [{"role": "user", "content": content}], "temperature": 0.1}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            req = urllib.request.Request(GROQ_URL, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                                                  "User-Agent": "guru-pi/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read().decode("utf-8"))
                t = d["choices"][0]["message"]["content"]
                t = re.sub(r"^```(json)?|```$", "", t.strip()).strip()
                return json.loads(t[t.find("{"):t.rfind("}") + 1])
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    return "quota"
                if e.code == 400 and json_mode:
                    continue                       # kai kurie modeliai be JSON režimo
                if log:
                    log.info("Groq (%s) %s", model, e.code)
                break
            except Exception as e:
                if log:
                    log.info("Groq (%s) nepavyko: %s", model, str(e)[:120])
                break
    return None


PROMPT_BATCH = """Žemiau – žinutės iš Telegram lažybų {kind} „{chat}“, laiko tvarka. [n] – žinutės numeris.
Kai kurios turi kupono nuotrauką – nuotraukos pridėtos po tekstu, kiekviena pažymėta „[n] 📷“.
Rask TIK tas žinutes, kuriose siūlomas ar daromas NAUJAS konkretus statymas (prieš rungtynes ar gyvai).
Statymas yra ir tada, kai narys parašo, ką stato („imu Arsenal“, „statau over 2.5“), ar įkelia savo kuponą.
NE statymas: reklama, VIP pardavimas, pasigyrimas laimėjimu, rezultato pranešimas („✅ win“), jau sužaistas kuponas,
statistika, klausimai, pokalbis, nuomonė be konkretaus pasirinkimo.
Grąžink TIK JSON:
{{"msgs": [{{"n": numeris, "combo": true/false, "total_odds": skaičius arba null,
  "placed": "YYYY-MM-DD HH:MM" – kada kuponas pastatytas, jei tai matosi kupono nuotraukoje, kitaip null,
  "bets": [{{"sport": "soccer/basketball/tennis/hockey/handball/volleyball/baseball/american_football/mma/esports/kita",
    "league": "...", "home": "...", "away": "...",
    "date": "YYYY-MM-DD" arba null, "time": "HH:MM" arba null,
    "side": "Win|DC|BTTS|Over|Under|Spread|Kita", "team": "kuriai komandai arba null",
    "line": skaičius arba null, "pick": "statymas originaliai", "odds": skaičius arba null, "women": true/false,
    "player": "žaidėjo vardas, jei statoma už žaidėjo statistiką (taškai, perdavimai, atkovoti, įvarčiai), kitaip null"}}]}}]}}
Žinučių be statymo NEĮTRAUK. Jei statymų nėra – {{"msgs": []}}.
Lygiosios = side "Win", team "Draw". „Abi įmuš“ = BTTS (pick „Yes“/„No“). „1X“, „X2“, „12“ = DC.
Datą imk iš žinutės arba jos laiko. „time“ – rungtynių pradžia (jei parašyta), ne žinutės laikas. Komandas rašyk angliškai. NIEKO NESUGALVOK – ko nėra, rašyk null.

ŽINUTĖS:
{msgs}"""

GROQ_PAUSE = {}                                   # modelis -> iki kada ilsisi (Groq limitai skaičiuojami kiekvienam modeliui)


def _parse_json(t):
    t = re.sub(r"(?s)<think>.*?</think>", "", t or "").strip()
    t = re.sub(r"^```(json)?|```$", "", t).strip()
    return json.loads(t[t.find("{"):t.rfind("}") + 1])


def _parse_json_any(t):
    try:
        t = re.sub(r"(?s)<think>.*?</think>", "", t or "").strip()
        t = re.sub(r"^```(json)?|```$", "", t).strip()
        i = min([x for x in (t.find("{"), t.find("[")) if x >= 0] or [-1])
        if i < 0:
            return None
        v = json.loads(t[i:t.rfind("]" if t[i] == "[" else "}") + 1])
    except Exception:
        return None
    if isinstance(v, list):                        # kartais grąžina tik sąrašą
        v = {"msgs": v}
    return v if isinstance(v, dict) else None


def groq_json(cfg, prompt, log=None, timeout=90):
    """Tekstinė Groq užklausa su modelių kaita. Grąžina dict, "quota" (visi modeliai ilsisi) arba None."""
    key = cfg.get("groq_key")
    if not key:
        return None
    models = [m for m in _groq_models(key, False, cfg, log, n=5) if GROQ_PAUSE.get(m, 0) < time.time()]
    if not models:
        return "quota"
    for model in models[:3]:
        extra = {"reasoning_effort": "low"} if "gpt-oss" in model else \
            {"reasoning_format": "hidden"} if "qwen" in model else {}
        variants = [dict(extra, response_format={"type": "json_object"}), {"response_format": {"type": "json_object"}}, {}]
        for v in variants:
            body = dict({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1,
                         "max_tokens": 6000}, **v)
            req = urllib.request.Request(GROQ_URL, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                                                  "User-Agent": "guru-pi/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read().decode("utf-8"))
                out = _parse_json_any(d["choices"][0]["message"]["content"])
                if out is None:
                    raise ValueError("ne JSON")
                return out
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    try:
                        wait = float(e.headers.get("retry-after") or 120)
                    except Exception:
                        wait = 120
                    GROQ_PAUSE[model] = time.time() + max(30, min(wait, 86400))
                    if log and wait > 600:
                        log.info("⏸ Groq %s dienos limitas – ilsisi %.0f min.", model, wait / 60)
                    break
                if e.code == 400 and v:
                    continue                                       # modelis nepalaiko šio nustatymo
                if log:
                    log.info("Groq (%s) %s", model, e.code)
                break
            except Exception as e:
                if log:
                    log.info("Groq (%s) nepavyko: %s", model, str(e)[:100])
                break
    return "quota" if all(GROQ_PAUSE.get(m, 0) > time.time() for m in models) else None


_OCR = {"ok": None, "langs": "eng", "tried": 0}


def ocr_ready(log=None):
    """Tesseract OCR Raspberry'je – kuponų nuotraukoms, kai Gemini limitas baigėsi. Įsidiegia pats (apt)."""
    if _OCR["ok"]:
        return True
    if shutil.which("tesseract"):
        try:
            langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30).stdout
            _OCR["langs"] = "+".join(l for l in ("eng", "lit", "rus") if l in langs.split()) or "eng"
        except Exception:
            pass
        _OCR["ok"] = True
        if log:
            log.info("🔤 OCR paruoštas (%s)", _OCR["langs"])
        return True
    if time.time() - _OCR["tried"] < 6 * 3600:
        return False
    _OCR["tried"] = time.time()
    if log:
        log.info("🔤 Diegiu OCR (tesseract) nuotraukų skaitymui…")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    pk = ["apt-get", "install", "-y", "--no-install-recommends", "tesseract-ocr", "tesseract-ocr-eng",
          "tesseract-ocr-lit", "tesseract-ocr-rus"]
    try:
        r = subprocess.run(pk, env=env, capture_output=True, timeout=1800)
        if r.returncode != 0:
            subprocess.run(["apt-get", "update"], env=env, capture_output=True, timeout=900)
            subprocess.run(pk, env=env, capture_output=True, timeout=1800)
    except Exception as e:
        if log:
            log.info("OCR diegimas nepavyko: %s", e)
    return bool(shutil.which("tesseract")) and ocr_ready(log)


def ocr(img):
    try:
        r = subprocess.run(["tesseract", "stdin", "stdout", "-l", _OCR["langs"], "--psm", "6"], input=img,
                           capture_output=True, timeout=90)
        t = r.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""
    t = re.sub(r"[ \t]+", " ", t)
    lines = [l.strip() for l in t.splitlines() if len(re.findall(r"[A-Za-z0-9]", l)) >= 3]
    t = "\n".join(lines)                              # be OCR šiukšlių – mažiau žodžių Groq'ui (dienos limitas)
    return t[:800] if len(re.findall(r"[A-Za-z0-9]", t)) >= 15 else ""


class Auditor:
    """Eilė – SQLite faile (gali būti dešimtys tūkstančių žinučių), statymai ir būsena – audit.json."""

    def __init__(self, client, cfg, log, base: Path, boot: Path):
        self.client, self.cfg, self.log = client, cfg, log
        self.file = base / "audit.json"
        self.boot = boot
        try:
            self.d = json.loads(self.file.read_text("utf-8"))
        except Exception:
            self.d = {}
        for k, v in {"chats": {}, "last_id": {}, "oldest": {}, "bf_done": {}, "queue": [], "bets": [], "ai_day": "",
                     "ai_used": 0, "groq_used": 0, "groq_pause": 0, "discovered": 0, "done_msgs": 0,
                     "pause_until": 0, "kind": {}}.items():
            self.d.setdefault(k, v)
        self.days = int(cfg.get("audit_days", 60))
        self.cap = int(cfg.get("audit_gemini_per_day", 800))        # Gemini užklausos (po 8 nuotraukas) per dieną
        self.gcap = int(cfg.get("audit_groq_per_day", 3000))        # Groq – limitus saugo patys modeliai (429)
        self.db = sqlite3.connect(str(base / "audit_q.db"), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS q (c TEXT, id INTEGER, t TEXT, txt TEXT, photo INTEGER, "
                        "u TEXT, un TEXT, tries INTEGER DEFAULT 0, PRIMARY KEY (c, id))")
        self.db.execute("CREATE INDEX IF NOT EXISTS q_t ON q (t, id)")
        self.db.execute("CREATE TABLE IF NOT EXISTS done (c TEXT, id INTEGER, PRIMARY KEY (c, id))")
        self.db.execute("UPDATE q SET tries=0 WHERE tries=99")    # nutrūko prieš išsaugant – analizuosim iš naujo
        self.db.commit()
        if self.d["queue"]:                                          # perkeliam seną eilę iš JSON
            self._insert(self.d["queue"])
            self.d["queue"] = []
        if self.d.get("settle_ver", 0) < 5:          # pataisytas rezultatų tikrinimas – perskaičiuojam viską
            for b in self.d["bets"]:
                b["status"], b["tries"], b["next"] = "pending", 0, 0
                for l in b["legs"]:
                    l["status"], l["detail"] = "pending", None
            self.d["settle_ver"] = 5
        if self.d.get("recollect_ver", 0) < 1:       # nesėkmingai nuskaitytos nuotraukos buvo išmestos – renkam iš naujo
            self.d["bf_done"], self.d["oldest"] = {}, {}
            self.d["recollect_ver"] = 1
        self.d.setdefault("err", {})
        if self.d.get("reset_ver", 0) < 1:           # vartotojo prašymu: viską nunulinam, analizuojam nuo šiandienos
            try:
                bk = base / f"audit_backup_{datetime.now():%Y%m%d_%H%M}.json"
                bk.write_text(json.dumps(self.d, ensure_ascii=False), "utf-8")
            except Exception:
                pass
            keep = {k: self.d[k] for k in ("chats", "kind", "discovered", "ai_day", "ai_used", "groq_used",
                                          "web_used", "settle_ver", "recollect_ver", "err") if k in self.d}
            self.d = {**keep, "bets": [], "queue": [], "last_id": {}, "oldest": {}, "bf_done": {}, "done_msgs": 0,
                      "pause_until": 0, "groq_pause": 0, "web_cache": {}, "alerts": 0, "reset_ver": 1,
                      "since": datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
                      .isoformat(timespec="minutes")}
            self.db.execute("DELETE FROM q")
            self.db.execute("DELETE FROM done")
            self.db.commit()
        if self.d.get("mkt_ver", 0) < 1:             # pataisyta rinkų atpažinimo logika – koeficientus tikrinam iš naujo
            for b in self.d["bets"]:
                b["next"] = 0
                for k in ("mkt", "mkt_try", "mkt_next"):
                    b.pop(k, None)
                for l in b["legs"]:
                    l.pop("mkt", None)
            self.d["mkt_ver"] = 1
        if self.d.get("force_ver", 0) < 1:           # GuruChat LT visada įtraukiama – grupes perieškom iškart
            self.d["discovered"], self.d["force_ver"] = 0, 1
        if self.d.get("settle_ver", 0) < 6:          # nauji šaltiniai ir pavadinimai – „nežinomus“ tikrinam iš naujo
            for b in self.d["bets"]:
                if b["status"] == "unknown":
                    b["status"], b["tries"], b["next"] = "pending", 0, 0
                    for l in b["legs"]:
                        if l["status"] == "unknown":
                            l["status"], l["detail"] = "pending", None
            self.d["settle_ver"] = 6
        if self.d.get("settle_ver", 0) < 7:          # laukiančius tikrinam iškart naujais būdais
            for b in self.d["bets"]:
                if b["status"] == "pending":
                    b["tries"], b["next"] = 0, 0
            self.d["settle_ver"] = 7
        if self.d.get("settle_ver", 0) < 8:          # nauja: paieška internete – „nežinomus“ bandom dar kartą
            for b in self.d["bets"]:
                if b["status"] in ("unknown", "pending"):
                    b["status"], b["tries"], b["next"] = "pending", 0, 0
                    for l in b["legs"]:
                        if l["status"] == "unknown":
                            l["status"], l["detail"] = "pending", None
            self.d["settle_ver"] = 8
        if self.d.get("settle_ver", 0) < 12:          # FotMob, DNB, „sausai“ – tikrinam iš naujo
            for b in self.d["bets"]:
                if b["status"] in ("unknown", "pending"):
                    b["status"], b["tries"], b["next"] = "pending", 0, 0
                    for l in b["legs"]:
                        if l["status"] == "unknown":
                            l["status"], l["detail"] = "pending", None
            self.d["settle_ver"] = 12
        if self.d.get("settle_ver", 0) < 13:          # kėlinių statymai buvo vertinami pagal visas rungtynes – perskaičiuojam viską
            for b in self.d["bets"]:
                b["status"], b["tries"], b["next"] = "pending", 0, 0
                for l in b["legs"]:
                    l["status"], l["detail"] = "pending", None
                    l.pop("note", None)
            self.d["settle_ver"] = 13
        if self.d.get("settle_ver", 0) < 14:          # nauja nemokama paieška internete – neišspręstus bandom iš naujo
            for b in self.d["bets"]:
                if b["status"] == "unknown" and not any("ne prognozė" in (l.get("detail") or "") for l in b["legs"]):
                    b["status"], b["tries"], b["next"] = "pending", 0, 0
                    for l in b["legs"]:
                        if l.get("status") in (None, "unknown", "pending"):
                            l["status"], l["detail"] = "pending", None
                            l.pop("note", None)
            self.d["web_cache"] = {}
            self.d["settle_ver"] = 14
        if self.d.get("settle_ver", 0) < 15:          # „paskelbta po rungtynių“ – tikslesnė taisyklė (LIVE tinka), perskaičiuojam
            for b in self.d["bets"]:
                if any("ne prognozė" in (l.get("detail") or "") for l in b["legs"]):
                    b["status"], b["tries"], b["next"] = "pending", 0, 0
                    b.pop("mkt", None)
                    for l in b["legs"]:
                        l["status"], l["detail"] = "pending", None
                        l.pop("note", None)
            self.d["settle_ver"] = 15
        if self.d.get("settle_ver", 0) < 16:          # BTTS, „komanda & over“, įvarčių autoriai, alias'ai – perskaičiuojam neišspręstus
            for b in self.d["bets"]:
                if b["status"] in ("unknown", "pending"):
                    b["status"], b["tries"], b["next"] = "pending", 0, 0
                    b.pop("mkt", None)
                    for l in b["legs"]:
                        if l.get("status") in (None, "unknown", "pending"):
                            l["status"], l["detail"] = "pending", None
                            l.pop("note", None)
                            l.pop("mkt", None)
            self.d["web_cache"] = {}
            self.d["settle_ver"] = 16
        if self.d.get("settle_ver", 0) < 17:          # rungtynių paieška iki 7 d. į priekį, MMA – neišspręstus dar kartą
            for b in self.d["bets"]:
                if b["status"] == "unknown":
                    b["status"], b["tries"], b["next"] = "pending", 0, 0
                    b.pop("mkt", None)
                    for l in b["legs"]:
                        if l.get("status") in (None, "unknown", "pending"):
                            l["status"], l["detail"] = "pending", None
                            l.pop("note", None)
                for l in b["legs"]:
                    if l.get("status") == "pending":
                        l["tries"] = 0
            self.d["settle_ver"] = 17
        self._keys = {(b["c"], b["mid"]) for b in self.d["bets"]}
        self._done, self._last_save, self._sum = [], 0, (0, None)
        self._ocr = False
        results.WEB = self.web_event                      # paskutinis šaltinis – Google paieška
        if props:
            props.WEB_STAT = self.web_stat
            props.CFG = cfg                               # tas pats žodynas – raktas įsigalioja iškart
            props.CACHE_DIR = base

    # ------------------------------------------------------------------ saugojimas
    def _insert(self, items):
        self.db.executemany("INSERT OR IGNORE INTO q (c, id, t, txt, photo, u, un) VALUES (?,?,?,?,?,?,?)",
                            [(q["c"], q["id"], q["t"], q.get("txt", ""), int(bool(q.get("photo"))),
                              str(q["u"]) if q.get("u") else None, q.get("un")) for q in items])
        self.db.commit()

    def qlen(self, photo=None):
        sql = "SELECT COUNT(*) FROM q WHERE tries<99" + ("" if photo is None else " AND photo=%d" % int(photo))
        return self.db.execute(sql).fetchone()[0]

    def save(self, force=False):
        if not force and time.time() - self._last_save < 120:       # SD kortelę tausojam – ne dažniau kaip kas 2 min.
            return
        try:
            self.d["bets"] = self.d["bets"][-20000:]
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.d, ensure_ascii=False, separators=(",", ":")), "utf-8")
            os.replace(tmp, self.file)
            if self._done:                                           # išanalizuotas trinam tik po išsaugojimo
                self.db.executemany("DELETE FROM q WHERE c=? AND id=?", self._done)
                self.db.executemany("INSERT OR IGNORE INTO done (c, id) VALUES (?, ?)", self._done)
                self.db.commit()
                self._done = []
            self._last_save = time.time()
        except Exception as e:
            self.log.info("audit save: %s", e)

    # ------------------------------------------------------------------ 1) kurios grupės lažybų
    async def discover(self):
        found = {}
        async for dlg in self.client.iter_dialogs():
            if dlg.is_user:
                continue
            try:
                msgs = await self.client.get_messages(dlg.entity, limit=60)
            except Exception:
                continue
            texts = [(m.message or "") for m in msgs if m]
            if len(texts) < 5:
                continue
            hits = sum(1 for t in texts if BET_RE.search(t))
            photos = sum(1 for m in msgs if m and m.photo)
            score = (hits + 0.5 * photos) / len(texts)
            forced = any(w and w in _n(dlg.name) for w in self.forced())
            if forced or (score >= 0.12 and hits >= 3) or str(dlg.id) in self.d["chats"]:
                found[str(dlg.id)] = dlg.name
                ent = dlg.entity
                self.d["kind"][str(dlg.id)] = "channel" if getattr(ent, "broadcast", False) else "group"
        self.d["chats"] = found
        self.d["discovered"] = time.time()
        self.log.info("🔎 Auditas: lažybų grupių rasta %d", len(found))
        self.save(True)

    # ------------------------------------------------------------------ 2) VISOS žinutės į eilę (iki N dienų atgal)
    def _keep(self, m, cid):
        t = (m.message or "").strip()
        if not m.photo and len(t) < 4:                  # lipdukai, „ok“, tuščios
            return None
        if not m.photo and (RESULT_ONLY.match(t) or (AD_RE.search(t) and not BET_RE.search(t) and len(t) > 120)):
            return None
        if (cid, m.id) in self._keys or self.db.execute("SELECT 1 FROM done WHERE c=? AND id=?",
                                                          (cid, m.id)).fetchone():
            return None
        q = {"c": cid, "id": m.id, "t": m.date.astimezone().isoformat(timespec="minutes"), "txt": t[:1200],
             "photo": bool(m.photo)}
        if self.d["kind"].get(cid) == "group":          # pokalbių grupė – kas parašė (narys)
            q["u"], q["un"] = m.sender_id, _who(m)
        return q

    def since(self):
        """Nuo kada analizuojam: nustatymų „audit_since“ arba nunulinimo diena (00:00), bet ne senesnė nei N d."""
        s = self.d.get("since") or self.cfg.get("audit_since")
        lim = datetime.now().astimezone() - timedelta(days=self.days)
        try:
            d = datetime.fromisoformat(str(s))
            if d.tzinfo is None:
                d = d.astimezone()
            return max(d, lim)
        except Exception:
            return lim

    def backfill_left(self):
        return sum(1 for c in self.d["chats"] if self.d["bf_done"].get(c, 0) < self.days)

    async def collect(self, per_chat=3000):
        since = self.since()
        for cid, name in list(self.d["chats"].items()):
            new = []
            try:
                last = int(self.d["last_id"].get(cid, 0))
                if last and self.d["bf_done"].get(cid, 0) >= self.days:       # naujos žinutės nuo paskutinės
                    async for m in self.client.iter_messages(int(cid), min_id=last, reverse=True, limit=per_chat):
                        self.d["last_id"][cid] = max(int(self.d["last_id"].get(cid, 0)), m.id)
                        q = self._keep(m, cid)
                        if q:
                            new.append(q)
                if self.d["bf_done"].get(cid, 0) < self.days:                 # istorija atgal iki N dienų
                    off = int(self.d["oldest"].get(cid, 0))
                    got, reached = 0, False
                    async for m in self.client.iter_messages(int(cid), offset_id=off, limit=per_chat):
                        got += 1
                        if not off and got == 1:                              # naujausia – nuo jos vėliau imsim naujas
                            self.d["last_id"][cid] = max(int(self.d["last_id"].get(cid, 0)), m.id)
                        if m.date < since:
                            reached = True
                            break
                        self.d["oldest"][cid] = m.id
                        q = self._keep(m, cid)
                        if q:
                            new.append(q)
                    if reached or got < per_chat:
                        self.d["bf_done"][cid] = self.days
            except Exception as e:
                self.log.info("audit collect %s: %s", name, str(e)[:150])
            if new:
                self._insert(new)
                self.log.info("📥 %s: +%d žinučių", name, len(new))
        self.save(True)

    # ------------------------------------------------------------------ 3) AI – po kelias žinutes vienu kartu
    def _groq_ok(self):
        return bool(self.cfg.get("groq_key")) and self.d["groq_used"] < self.gcap and \
            time.time() > self.d.get("groq_pause", 0)

    def _gem_ok(self):
        return bool(self.cfg.get("gemini_key")) and self.d["ai_used"] < self.cap and \
            time.time() > self.d.get("pause_until", 0)

    def _ai_ok(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.d["ai_day"] != today:
            self.d["ai_day"], self.d["ai_used"], self.d["groq_used"] = today, 0, 0
            self.d["web_used"] = self.d["sweb_used"] = self.d["sweb_ok"] = 0
        return self._groq_ok() or self._gem_ok()

    def forced(self):
        """Grupės, kurios analizuojamos VISADA (net jei mažai statymų) – pagal nutylėjimą GURU G⚽AL ir GuruChat LT."""
        return [_n(x) for x in (self.cfg.get("audit_first") or ["GURU G", "GuruGoal", "GuruChat"])]

    def order(self):
        """Grupių eilė: pirma GURU G⚽AL, po jos GuruChat LT (ar nurodytos nustatymuose), tada – kurios turi daugiausia žinučių."""
        want = self.forced()
        cnt = dict(self.db.execute("SELECT c, COUNT(*) FROM q WHERE tries<99 GROUP BY c").fetchall())
        nb = {}
        for b in self.d["bets"]:
            nb[b["c"]] = nb.get(b["c"], 0) + 1
        chats = list(self.d["chats"])
        first = []
        for w in want:                                    # nustatymų tvarka: GURU G⚽AL → GuruChat LT
            first += [c for c in chats if c not in first and w and w in _n(self.d["chats"][c])]
        rest = sorted((c for c in chats if c not in first), key=lambda c: -(cnt.get(c, 0) + 3 * nb.get(c, 0)))
        return first + rest, cnt

    def focus(self):
        od, cnt = self.order()
        return next((c for c in od if cnt.get(c)), None)

    def _batch(self, photos_ok):
        """Grupės analizuojamos po vieną: kol einamoji grupė neišanalizuota iki galo – kitų neimam.
        Išimtis – visai naujos (< 2 val.) žinutės: jos imamos iš karto, kad pranešimai ateitų laiku."""
        where = " AND tries<99" + ("" if photos_ok else " AND photo=0")
        recent = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="minutes")
        first = self.db.execute("SELECT c, t FROM q WHERE t>=?" + where + " ORDER BY t, id LIMIT 1", (recent,)).fetchone()
        if not first:
            od, cnt = self.order()
            for c in od:
                if not cnt.get(c):
                    continue
                first = self.db.execute("SELECT c, t FROM q WHERE c=?" + where + " ORDER BY t, id LIMIT 1", (c,)).fetchone()
                if first or photos_ok:
                    break                                 # einamoji grupė – kitų neliečiam
                # nuotraukų dabar nėra kuo skaityti – kol kas imam kitos grupės tekstą
        if not first:
            return []
        rows = self.db.execute("SELECT c, id, t, txt, photo, u, un, tries FROM q WHERE c=? AND t>=?" + where +
                               " ORDER BY t, id LIMIT 30", first).fetchall()
        if rows and rows[0][7] > 0:                  # jau nepavyko kartu su kitomis – dabar bandom po vieną
            return rows[:1]
        out, chars, ph = [], 0, 0
        for r in rows:
            chars += len(r[3] or "") + 40
            if out and (chars > 6000 or len(out) >= 20 or (r[4] and ph >= 8)):
                break
            ph += r[4]
            out.append(r)
        return out

    def _gemini(self, prompt, images):
        key = self.cfg.get("gemini_key")
        parts = [{"text": prompt}]
        for n, img in images:
            parts += [{"text": f"[{n}] 📷"}, {"inline_data": {"mime_type": "image/jpeg",
                                                            "data": base64.b64encode(img).decode()}}]
        body = json.dumps({"contents": [{"role": "user", "parts": parts}],
                           "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}).encode()
        for model in self.cfg.get("gemini_models") or ["gemini-3.5-flash-lite"]:
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=body,
                method="POST", headers={"Content-Type": "application/json", "x-goog-api-key": key})
            try:
                with urllib.request.urlopen(req, timeout=150) as r:
                    d = json.loads(r.read().decode())
                parts = ((d.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                out = _parse_json_any(txt)
                if out is not None:
                    return out
                self._err("gemini: neaiškus atsakymas " + str((d.get("candidates") or [{}])[0].get("finishReason")))
            except urllib.error.HTTPError as e:
                eb = ""
                try:
                    eb = e.read().decode("utf-8", "ignore")[:300]
                except Exception:
                    pass
                if e.code == 429:
                    day = "PerDay" in eb or "per day" in eb.lower()
                    self.d["pause_until"] = time.time() + (3 * 3600 if day else 120)
                    self._err("gemini 429" + (" (dienos)" if day else " (minutės)"))
                    return "quota"
                self._err(f"gemini {e.code}: " + re.sub(r"\s+", " ", eb)[:100])
            except Exception as e:
                self._err(f"gemini: {type(e).__name__} {str(e)[:80]}")
        return None

    WEB_PROMPT = """Surask internete OFICIALŲ GALUTINĮ rungtynių rezultatą.
Rungtynės: {home} – {away}. Sportas: {sport}. Lyga/turnyras: {league}. Data: apie {date} (gali būti ±1 d.).
Grąžink TIK JSON:
{{"found": true/false, "home": "namų komanda kaip šaltinyje", "away": "svečių komanda kaip šaltinyje",
 "date": "YYYY-MM-DD", "status": "finished/postponed/cancelled/not_played/live",
 "home_score": sveikas sk., "away_score": sveikas sk.,
 "regular_home": pagrindinio laiko rezultatas (be pratęsimo) arba null, "regular_away": ... arba null,
 "ht_home": kėlinio/pirmo kėlinio rezultatas arba null, "ht_away": ... arba null,
 "source": "šaltinio adresas"}}
Jei patikimo šaltinio neradai arba nesi tikras, kad tai tos pačios rungtynės – {{"found": false}}. NIEKO NESUGALVOK."""

    WEB_STAT_PROMPT = """Surask internete rungtynių {home} – {away} ({sport}, {league}, apie {date}) oficialią statistiką.
Reikia: {what}. Grąžink TIK JSON: {{"found": true/false, "value": skaičius, "played": true/false, "source": "adresas"}}
Jei žaidėjas nežaidė – "played": false. Jei patikimo šaltinio neradai – {{"found": false}}. NIEKO NESUGALVOK."""

    def _web_groq(self, prompt):
        """Groq „compound“ modelis su paieška internete – kai Gemini paieška ilsisi."""
        key = self.cfg.get("groq_key")
        if not key or time.time() < self.d.get("gweb_pause", 0):
            return None
        if self.d.get("gweb_none", 0) > time.time():
            return None                                   # šio rakto Groq paieškos modelio nėra
        _groq_models(key, False, self.cfg, self.log)
        ids = [m for m in _GROQ_M["ids"] if "compound" in m] or ["groq/compound", "groq/compound-mini"]
        for model in ids[:2]:
            body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                               "temperature": 0}).encode()
            req = urllib.request.Request(GROQ_URL, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                                                  "User-Agent": "guru-pi/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    d = json.loads(r.read().decode())
                msg = (d.get("choices") or [{}])[0].get("message") or {}
                self.d["web_used"] = self.d.get("web_used", 0) + 1
                if not msg.get("executed_tools"):
                    continue                                # neieškojo internete – nepasitikim
                out = _parse_json_any(msg.get("content") or "")
                if out is not None:
                    return out
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self.d["gweb_pause"] = time.time() + 120
                elif e.code in (403, 404):
                    self.d["gweb_none"] = time.time() + 86400
                self._err(f"web groq {e.code}")
            except Exception as e:
                self._err(f"web groq: {type(e).__name__}")
        return None

    SEARCH_NOTE = """

Žemiau – ką radau internete (paieškos ištraukos ir puslapiai). Naudok TIK šią informaciją, ne savo atmintį.
Papildomai JSON'e grąžink "evidence": TIKSLIĄ trumpą citatą iš žemiau esančio teksto, kurioje matosi atsakymas.
Jei tekste atsakymo nėra – {{"found": false}}.

"""

    def _web_search(self, prompt, queries, check=None):
        """Paieška per Tavily API + Groq perskaito rezultatus. check(r, ctx) – ar atsakymas tikrai tekste."""
        if not props or not hasattr(props, "search_context") or not self.cfg.get("groq_key") or not self.cfg.get("tavily_key"):
            return None
        if time.time() < self.d.get("sweb_pause", 0):
            return None
        try:
            ctx, urls = props.search_context(queries)
        except Exception as e:
            self._err(f"paieška: {type(e).__name__}")
            return None
        self.d["sweb_used"] = self.d.get("sweb_used", 0) + 1
        if not ctx:
            return None
        r = groq_json(self.cfg, prompt + self.SEARCH_NOTE.replace("{{", "{").replace("}}", "}") + ctx, self.log)
        if r == "quota":
            self.d["sweb_pause"] = time.time() + 300
            return None
        if not isinstance(r, dict) or not r.get("found"):
            return None
        if check and not check(r, ctx):
            self._err("paieška: AI atsakymo tekste nėra – atmesta")
            return None
        self.d["sweb_ok"] = self.d.get("sweb_ok", 0) + 1
        r.setdefault("source", (urls or [""])[0])
        return r

    def _web(self, prompt, queries=None, check=None):
        """Gemini su Google paieška → Groq su paieška → nemokama paieška (DuckDuckGo/Bing) + AI."""
        if self.d.get("web_used", 0) >= int(self.cfg.get("audit_web_per_day", 600)):
            return None
        r = self._web_google(prompt)
        if r is None and queries:
            r = self._web_search(prompt, queries, check)
        return r

    def _web_google(self, prompt):
        if not self.cfg.get("gemini_key") or time.time() < self.d.get("web_pause", 0):
            return self._web_groq(prompt)
        body = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                           "tools": [{"google_search": {}}], "generationConfig": {"temperature": 0}}).encode()
        pz = self.d.setdefault("web_mpause", {})
        for model in (self.cfg.get("gemini_web_models") or ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash",
                                                            "gemini-2.5-flash-lite", "gemini-2.0-flash"]):
            if pz.get(model, 0) > time.time():
                continue                                   # šio modelio paieškos limitas – bandom kitą (kiekvienas turi savo)
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=body,
                method="POST", headers={"Content-Type": "application/json", "x-goog-api-key": self.cfg["gemini_key"]})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    d = json.loads(r.read().decode())
                self.d["web_used"] = self.d.get("web_used", 0) + 1
                cand = (d.get("candidates") or [{}])[0]
                if not ((cand.get("groundingMetadata") or {}).get("groundingChunks")):
                    return None                            # be tikro šaltinio internete – nepasitikim
                txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or []
                              if not p.get("thought"))
                return _parse_json_any(txt)
            except urllib.error.HTTPError as e:
                eb = ""
                try:
                    eb = e.read().decode("utf-8", "ignore")
                except Exception:
                    pass
                if e.code == 429:
                    day = "PerDay" in eb or "per day" in eb.lower() or "quota" in eb.lower()
                    pz[model] = time.time() + (3 * 3600 if day else 90)
                    self._err("web 429 " + model)
                    continue
                if e.code in (400, 403, 404):
                    pz[model] = time.time() + 86400        # modelio nėra / paieška jam neleidžiama
                self._err(f"web {e.code} {model}: " + re.sub(r"\s+", " ", eb)[:60])
            except Exception as e:
                self._err(f"web: {type(e).__name__}")
        return self._web_groq(prompt)

    def web_event(self, leg):
        """Rezultatas iš interneto (Google) – LiveScore formatu, kad įvertintų tas pats results.evaluate."""
        key = f"{_n(leg.get('home'))}|{_n(leg.get('away'))}|{str(leg.get('kickoff'))[:10]}"
        cache = self.d.setdefault("web_cache", {})
        hit = cache.get(key)
        if hit and (hit.get("found") or time.time() - hit.get("ts", 0) < 6 * 3600):
            r = hit
        else:
            h, a, d0 = leg.get("home"), leg.get("away") or "", str(leg.get("kickoff"))[:10]

            def check(r, ctx):                            # rezultatas turi būti tekste (pvz. „2-1“, „2:1“)
                try:
                    x, y = int(r["home_score"]), int(r["away_score"])
                except Exception:
                    return str(r.get("status") or "").lower() in ("postponed", "cancelled") and \
                        re.search(r"postpon|cancel|atidet|atšauk", ctx, re.I)
                return bool(re.search(rf"(?<!\d){x}\s*[-:–—]\s*{y}(?!\d)|(?<!\d){y}\s*[-:–—]\s*{x}(?!\d)", ctx))

            qs = [f"{h} vs {a} {d0}", f"{h} {a} score {leg.get('league') or ''}".strip()] if a else [f"{h} {d0} score"]
            r = self._web(self.WEB_PROMPT.format(home=h, away=a or "?",
                                                 sport=leg.get("sport") or "?", league=leg.get("league") or "?",
                                                 date=d0), qs, check) or {"found": False}
            r["ts"] = time.time()
            cache[key] = r
            if len(cache) > 3000:
                cache.pop(next(iter(cache)))
        if not r.get("found"):
            return None
        try:
            k = datetime.fromisoformat(str(leg.get("kickoff"))[:16])
            d = datetime.fromisoformat(str(r.get("date"))[:10])
            if abs((d.date() - k.date()).days) > 1:
                return None                                 # kitos dienos rungtynės
            if not (results.same(leg.get("home"), r.get("home")) or results.same(leg.get("home"), r.get("away"))):
                return None
            st = str(r.get("status") or "").lower()
            e = {"_t1": r.get("home"), "_t2": r.get("away"), "Eps": "FT" if st == "finished" else
                 "POSTP." if st in ("postponed", "cancelled") else "NS",
                 "Tr1": int(r["home_score"]) if r.get("home_score") is not None else None,
                 "Tr2": int(r["away_score"]) if r.get("away_score") is not None else None,
                 "_sport": results.ls_sport(leg) or "soccer", "_league": "internetas: " + str(r.get("source") or "")[:60],
                 "_start": k}
            if r.get("regular_home") is not None and r.get("regular_away") is not None:
                e["Tr1OR"], e["Tr2OR"] = int(r["regular_home"]), int(r["regular_away"])
            if r.get("ht_home") is not None and r.get("ht_away") is not None:
                e["Trh1"], e["Trh2"] = int(r["ht_home"]), int(r["ht_away"])
            if e["Tr1"] is not None and e["Tr2"] is not None:
                e["Ewt"] = 1 if e["Tr1"] > e["Tr2"] else 2 if e["Tr2"] > e["Tr1"] else None
            h, a = leg.get("home"), leg.get("away")
            e["_swapped"] = (results.mscore(h, e["_t2"]) + results.mscore(a, e["_t1"])) > \
                (results.mscore(h, e["_t1"]) + results.mscore(a, e["_t2"]))
            return e
        except Exception:
            return None

    def web_stat(self, leg, what):
        def check(r, ctx):
            if r.get("played") is False:
                return True
            try:
                v = float(r["value"])
            except Exception:
                return False
            return bool(re.search(rf"(?<![\d.]){v:g}(?![\d])", ctx))

        d0 = str(leg.get("kickoff"))[:10]
        qs = [f"{leg.get('home')} vs {leg.get('away') or ''} {what} {d0}", f"{leg.get('home')} {leg.get('away') or ''} stats {d0}"]
        r = self._web(self.WEB_STAT_PROMPT.format(home=leg.get("home"), away=leg.get("away") or "?",
                                                  sport=leg.get("sport") or "?", league=leg.get("league") or "?",
                                                  date=d0, what=what), qs, check)
        return r if r and r.get("found") else None

    def _err(self, what):
        e = self.d.setdefault("err", {})
        e[what[:140]] = e.get(what[:140], 0) + 1
        if len(e) > 30:
            e.pop(min(e, key=e.get))

    async def analyze_some(self, n=3):
        if not self._ocr:
            self._ocr = await asyncio.to_thread(ocr_ready, self.log)
        for _ in range(n):
            if not self._ai_ok():
                return
            gem = self._gem_ok()
            photos_ok = gem or (self._ocr and self._groq_ok())
            items = await asyncio.to_thread(self._batch, photos_ok)
            if not items:
                return
            c = items[0][0]
            kind = "kanalo" if self.d["kind"].get(c) == "channel" else "pokalbių grupės"
            imgs = {}
            for i, it in enumerate(items, 1):
                if it[4]:
                    try:
                        m = await self.client.get_messages(int(it[0]), ids=it[1])
                        img = await m.download_media(bytes) if m and m.photo else None
                        if img:
                            imgs[i] = img
                    except Exception:
                        pass

            def lines(ocr_txt=None):
                out = []
                for i, (cid, mid, t, txt, photo, u, un, tries) in enumerate(items, 1):
                    who = f" {un}:" if un else ":"
                    ln = f"[{i}] {t[:10]} {t[11:16]}{who} {(txt or '(be teksto)')[:1200]}"
                    if photo and ocr_txt is not None:
                        o = ocr_txt.get(i)
                        ln += f"\n    📷 kupono tekstas (OCR, gali būti klaidų): {o}" if o else "\n    📷 (nuotraukos perskaityti nepavyko)"
                    elif photo:
                        ln += " 📷"
                    out.append(ln)
                return "\n".join(out)

            chat = self.d["chats"].get(c, c)
            res = None
            if imgs and gem:                                  # nuotraukos – Gemini „mato“ kuponą
                self.d["ai_used"] += 1
                res = await asyncio.to_thread(self._gemini, PROMPT_BATCH.format(kind=kind, chat=chat, msgs=lines()),
                                              sorted(imgs.items()))
            if not isinstance(res, dict) and self._groq_ok():
                ocr_txt = None
                if imgs:
                    if not self._ocr:
                        if res is None:
                            self._fail(items)
                        return
                    ocr_txt = {}
                    for i, img in imgs.items():
                        ocr_txt[i] = await asyncio.to_thread(ocr, img)
                    self.d["ocr_used"] = self.d.get("ocr_used", 0) + len(imgs)
                prompt = PROMPT_BATCH.format(kind=kind, chat=chat, msgs=lines(ocr_txt))
                r2 = await asyncio.to_thread(groq_json, self.cfg, prompt, self.log)
                if isinstance(r2, dict):
                    self.d["groq_used"] += 1
                    res = r2
                elif r2 == "quota":
                    self.d["groq_pause"] = time.time() + 300
                    return
                else:
                    self._err("groq: nepavyko")
            if not isinstance(res, dict):
                if res is None:
                    self._fail(items)
                return
            self._take(items, res)
            await asyncio.sleep(2)

    notify = None                                         # pi_guru priskiria ntfy siuntimą

    def _alert(self, new):
        """Naujas statymas iš PATIKRINTOS grupės ar nario (≥30 statymų, pelnas ne atsitiktinis) → pranešimas telefonu."""
        if not new or not self.notify or not self.cfg.get("audit_alerts", True):
            return
        now = datetime.now().astimezone()
        s = self.summary()
        gm = {g["cid"]: g for g in s.get("groups") or []}
        for b in new:
            try:
                if datetime.fromisoformat(b["t"]) < now - timedelta(hours=3):
                    continue                              # sena žinutė iš istorijos – ne signalas
                kick = max(datetime.fromisoformat(l["kickoff"][:16]) for l in b["legs"])
                if kick < datetime.now() - timedelta(hours=2):
                    continue
            except Exception:
                continue
            g = gm.get(b["c"])
            why = None
            if g and b.get("u"):
                m = next((x for x in g.get("members") or [] if x["u"] == str(b["u"])), None)
                if m and m["proven"]:
                    why = f"narys {m['name']}: {m['hit']}% pataikymas, ROI {m['roi']:+}% ({m['n_odds']} patikrinti st.)"
            if not why and g and g["proven"]:
                why = f"grupė: {g['hit']}% pataikymas, ROI {g['roi']:+}% ({g['n_odds']} patikrinti st.)"
            if not why:
                continue
            legs = b["legs"]
            lines = [f"{l.get('home')} – {l.get('away') or '?'} | {l.get('pick')}" + (f" @ {l['odds']}" if l.get("odds") else "")
                     for l in legs[:6]]
            title = f"⭐ {self.d['chats'].get(b['c'], b['c'])}" + (f" ({b['un']})" if b.get("un") else "")
            msg = "\n".join(lines) + (f"\nBendras koef. {b['odds']}" if len(legs) > 1 and b.get("odds") else "") + \
                f"\n✔ {why}"
            self.notify(title, msg, legs)
            self.d["alerts"] = self.d.get("alerts", 0) + 1

    def _fail(self, items):
        """Nepavyko – bandysim dar (po vieną žinutę); išmetam tik po 5 bandymų."""
        self.db.executemany("UPDATE q SET tries=tries+1 WHERE c=? AND id=?", [(r[0], r[1]) for r in items])
        gone = self.db.execute("SELECT COUNT(*) FROM q WHERE tries>=5 AND tries<99").fetchone()[0]
        if gone:
            self.d["dropped"] = self.d.get("dropped", 0) + gone
        self.db.execute("DELETE FROM q WHERE tries>=5 AND tries<99")
        self.db.commit()

    def _take(self, items, res):
        for m in res.get("msgs") or []:
            try:
                cid, mid, t, txt, photo, u, un, _ = items[int(m.get("n")) - 1]
            except Exception:
                continue
            if (cid, mid) in self._keys:
                continue
            created = t[:16]
            try:                                           # kuponas pastatytas anksčiau, nei įkeltas – imam pastatymo laiką
                pl = datetime.fromisoformat(str(m.get("placed") or "").strip().replace(" ", "T")[:16])
                tt = datetime.fromisoformat(t[:16])
                if tt - timedelta(days=2) <= pl < tt:
                    created = pl.isoformat(timespec="minutes")
            except Exception:
                pass
            legs = []
            for b in (m.get("bets") or [])[:8]:
                if not isinstance(b, dict) or not b.get("home"):
                    continue
                day = b.get("date") or t[:10]
                kick = f"{day}T{(b.get('time') or t[11:16])}"
                try:
                    datetime.fromisoformat(kick)
                except Exception:
                    kick = t[:16]
                legs.append({"home": b.get("home"), "away": b.get("away"), "sport": b.get("sport"),
                             "league": b.get("league"), "side": b.get("side"), "team": b.get("team"),
                             "line": b.get("line"), "pick": b.get("pick"), "odds": b.get("odds"),
                             "women": b.get("women") if isinstance(b.get("women"), bool) else None,
                             "subject": b.get("player") or None,
                             "kickoff": kick, "created": created, "status": "pending"})
            if legs:
                combo = len(legs) > 1
                odds = m.get("total_odds") if combo else legs[0].get("odds")
                self.d["bets"].append({"c": cid, "mid": mid, "t": t, "legs": legs, "odds": odds, "status": "pending",
                                       "tries": 0, "u": u, "un": un, "photo": bool(photo)})
                self._keys.add((cid, mid))
        self.d["done_msgs"] += len(items)
        self._done += [(r[0], r[1]) for r in items]
        try:
            self._alert([b for b in self.d["bets"][-40:] if (b["c"], b["mid"]) in {(r[0], r[1]) for r in items}])
        except Exception as e:
            self._err(f"alert: {e}")
        self.db.executemany("UPDATE q SET tries=99 WHERE c=? AND id=?", [(r[0], r[1]) for r in items])
        self.db.commit()                                 # tries=99 – nebeimti į kitą paketą (trinsis po save)

    # ------------------------------------------------------------------ 4) rezultatai internete
    @staticmethod
    def _kick(l, b):
        """Rungtynių pradžia; jei AI datą suklydo (gerokai prieš žinutę ar > 4 d. po jos) – imam žinutės laiką."""
        try:
            t = datetime.fromisoformat(str(b["t"])[:16])
        except Exception:
            t = datetime.now()
        try:
            k = datetime.fromisoformat(str(l.get("kickoff"))[:16])
            if t - timedelta(hours=12) <= k <= t + timedelta(days=4):
                return k
        except Exception:
            pass
        return t

    FINAL = re.compile(r"neskaičiuojam|neįvertina|procentin|kombinuotas|neaišku|nežinoma rinka|nėra linijos|"
                       r"nepalaikoma|ne rungtynių komanda|ne prognozė")

    @staticmethod
    def _check_leg(l, k):
        try:
            lg = dict(l, kickoff=k.isoformat(timespec="minutes"))
            if props and props.is_prop(l):                  # žaidėjai, kampiniai, kortelės – EuroLeague / ESPN
                return props.settle(lg)
            st, detail, info = results.settle(lg)
            if not st and props and re.search(r"žaidėj|ne rungtynių komanda|neįvertina", info or ""):
                return props.settle(lg)
            return st, detail, info
        except Exception as e:
            return None, None, f"klaida: {str(e)[:80]}"

    def settle_some(self, limit=60):
        now = datetime.now()
        todo, picked, probes = [], [], set()
        od, _ = self.order()
        rank = {c: i for i, c in enumerate(od)}
        bets = sorted(self.d["bets"], key=lambda x: (rank.get(x["c"], 999), str(x["t"])), reverse=False)
        for b in bets:                                    # grupės eilės tvarka (pirma GURU G⚽AL)
            if b["status"] != "pending" or len(todo) >= limit or b.get("next", 0) > time.time():
                continue
            picked.append(b)
            for l in b["legs"]:
                if l["status"] != "pending":
                    continue
                k = self._kick(l, b)
                if k + timedelta(hours=2, minutes=15) > now:
                    l["note"] = "laukia rungtynių pabaigos"
                    # AI kartais neteisingai nuskaito laiką (kita laiko juosta) – kas 30 min. vis tiek pažiūrim,
                    # gal rungtynės jau baigėsi; nepavykus – bandymų neskaičiuojam
                    if l.get("probe", 0) > time.time() or k > now + timedelta(hours=8):
                        continue
                    l["probe"] = time.time() + 1800
                    probes.add(id(l))
                todo.append((b, l, k))
        with ThreadPoolExecutor(max_workers=6) as ex:     # keli šaltiniai vienu metu – ~6× greičiau
            outs = list(ex.map(lambda x: self._check_leg(x[1], x[2]), todo))
        checked, waiting = {}, {}
        for (b, l, k), (st, detail, info) in zip(todo, outs):
            if id(l) in probes and not st:
                continue                                  # dar nesibaigė – laukiam kaip anksčiau
            checked[id(b)] = checked.get(id(b), 0) + 1
            if st:
                l["status"], l["detail"] = st, detail
                l.pop("note", None)
            elif info and self.FINAL.search(info):
                l["status"], l["detail"] = "unknown", info + " – neįskaitoma"   # šito patikrinti neįmanoma
                l.pop("note", None)
            else:
                l["note"] = info or "tikrinama"
                if "dar vyksta arba neprasidėjo" in (info or ""):
                    waiting[id(b)] = waiting.get(id(b), 0) + 1
        for b in picked:
            sts = [l["status"] for l in b["legs"]]
            pend = [l for l in b["legs"] if l["status"] == "pending"]
            if "lost" in sts:
                b["status"] = "lost"
            elif all(x in ("won", "push") for x in sts):
                b["status"] = "push" if all(x == "push" for x in sts) else "won"
            elif not pend:
                b["status"] = "unknown"                   # bent viena dalis nepatikrinama – kombo neįskaitom
            elif waiting.get(id(b)) and str(b["t"])[:10] >= (now - timedelta(days=4)).strftime("%Y-%m-%d") and \
                    all(re.search(r"dar vyksta arba neprasidėjo|laukia rungtynių pabaigos", l.get("note") or "")
                        for l in pend):
                b["next"] = time.time() + 1800            # LiveScore rodo, kad dar nesibaigė – bandymų neskaičiuojam
            elif not checked.get(id(b)):                  # rungtynės dar nesibaigė – laukiam, bandymų neskaičiuojam
                try:                                      # (bet ne ilgiau 30 min. – laikas gali būti nuskaitytas klaidingai)
                    b["next"] = min(min(self._kick(l, b) for l in pend).timestamp() + 2.25 * 3600, time.time() + 1800)
                except Exception:
                    b["next"] = time.time() + 1800
            else:
                b["tries"] += 1
                b["next"] = time.time() + (1200 if b["tries"] <= 3 else 2 * 3600)
                try:
                    old = max(self._kick(l, b) for l in b["legs"]) < now - timedelta(hours=20)
                except Exception:
                    old = True
                if (old and b["tries"] >= 5) or b["tries"] > 12:
                    b["status"] = "unknown"          # rezultato rasti nepavyko – į statistiką neskaičiuojam
                    for l in pend:
                        l["detail"] = (l.get("note") or "rezultato nerasta") + " – neįskaitoma"

    # ------------------------------------------------------------------ 5) reitingas
    def summary(self):
        if time.time() - self._sum[0] < 30 and self._sum[1]:
            return self._sum[1]
        r = self._summary()
        self._sum = (time.time(), r)
        return r

    def _summary(self):
        bets = list(self.d["bets"])
        chats, kind = self.d["chats"], self.d["kind"]
        by_c = {}
        for b in bets:
            by_c.setdefault(b["c"], []).append(b)
        groups = []
        for c, lst in by_c.items():
            g = _stats(lst)
            g.update(cid=c, chat=chats.get(c, c), kind=kind.get(c, "channel"))
            if g["kind"] == "group":                      # nariai – kas iš tikrųjų pataiko
                by_u = {}
                for b in lst:
                    if b.get("u"):
                        by_u.setdefault(str(b["u"]), []).append(b)
                mem = []
                for u, ul in by_u.items():
                    m = _stats(ul)
                    m.update(u=u, name=next((x.get("un") for x in ul if x.get("un")), u),
                             photos=sum(1 for x in ul if x.get("photo")))
                    if m["n"] or m["pend"]:
                        mem.append(m)
                mem.sort(key=lambda m: (m["proven"], m["n_odds"] >= 10,
                                        m["roi_low"] if m["roi_low"] is not None else -999), reverse=True)
                g["members"] = mem[:40]
            # pelno kreivė (dienomis)
            day = {}
            for b in lst:
                u = _units(b) if b["status"] in ("won", "lost", "push") else None
                if u is not None:
                    day[b["t"][:10]] = day.get(b["t"][:10], 0) + u
            cum, ser = 0.0, []
            for d in sorted(day):
                cum += day[d]
                ser.append([d, round(cum, 2)])
            g["series"] = ser
            groups.append(g)
        today = datetime.now().strftime("%Y-%m-%d")
        for g in groups:
            g["today"] = _stats([b for b in by_c.get(g["cid"], []) if str(b["t"])[:10] == today])
            g["bet"] = _stats(_bettable(by_c.get(g["cid"], [])))
        od, cnt = self.order()
        fc = next((c for c in od if cnt.get(c)), None)
        for g in groups:
            g["q"] = cnt.get(g["cid"], 0)
            g["state"] = "analizuojama" if g["cid"] == fc else ("eilėje" if g["q"] else "išanalizuota")
        groups.sort(key=lambda g: (g["proven"], g["n_odds"] >= 15,
                                   g["roi_low"] if g["roi_low"] is not None else -999), reverse=True)
        tot = _stats(bets)
        # bendros analitikos dalys
        day, hours, sides, sports, oddsb = {}, [0] * 24, {}, {}, {}
        for b in bets:
            try:
                hours[int(b["t"][11:13])] += 1
            except Exception:
                pass
            u = _units(b) if b["status"] in ("won", "lost", "push") else None
            if u is not None:
                day[b["t"][:10]] = day.get(b["t"][:10], 0) + u
            if b["status"] not in ("won", "lost", "push"):
                continue
            leg = b["legs"][0] if len(b["legs"]) == 1 else None
            for key, dct in ((("Kombo" if not leg else (leg.get("side") or "Kita")), sides),
                             ((leg.get("sport") if leg else "kombo") or "kita", sports)):
                x = dct.setdefault(key, {"n": 0, "w": 0, "units": 0.0, "n_odds": 0})
                x["n"] += 1
                x["w"] += b["status"] == "won"
                if u is not None:
                    x["units"] += u
                    x["n_odds"] += 1
            try:
                o = float(b.get("odds") or 0)
                if o > 1.01:
                    k = "<1.5" if o < 1.5 else "1.5–2" if o < 2 else "2–3" if o < 3 else "3–5" if o < 5 else "5+"
                    x = oddsb.setdefault(k, {"n": 0, "units": 0.0})
                    x["n"] += 1
                    x["units"] += u or 0
            except Exception:
                pass
        for dct in (sides, sports):
            for x in dct.values():
                x["roi"] = round(x["units"] / x["n_odds"] * 100, 1) if x["n_odds"] else None
                x["units"] = round(x["units"], 2)
        cum, series = 0.0, []
        for d in sorted(day):
            cum += day[d]
            series.append([d, round(cum, 2), round(day[d], 2)])
        recent = []
        for b in sorted(bets, key=lambda x: x["t"], reverse=True)[:400]:
            recent.append({"t": b["t"], "c": b["c"], "chat": chats.get(b["c"], b["c"]), "who": b.get("un"),
                           "status": b["status"], "odds": b.get("odds"), "photo": b.get("photo", False),
                           "mkt": b.get("mkt"),
                           "legs": [{"m": f"{l.get('home')} – {l.get('away') or ''}".strip(" –"), "pick": l.get("pick"),
                                     "st": l.get("status"),
                                     "d": (l.get("detail") or (("⏳ " + l["note"]) if l.get("note") else ""))[:110],
                                     "k": str(self._kick(l, b))[5:16]} for l in b["legs"][:6]]})
        bb = _bettable(bets)
        leaders = {"today": _leaders([b for b in bets if str(b["t"])[:10] == today], chats, kind),
                   "all": _leaders(bets, chats, kind), "bet": _leaders(bb, chats, kind)}
        return {"groups": groups, "total": tot, "leaders": leaders, "since": self.d.get("since"),
                "today_total": _stats([b for b in bets if str(b["t"])[:10] == today]), "bet_total": _stats(bb),
                "mkt_checked": sum(1 for b in bets if "mkt" in b), "mkt_ok": len(bb),
                "odds_src": (props.ODDS_STATE.get("last") if props and hasattr(props, "ODDS_STATE") else ""), "series": series, "hours": hours, "sides": sides, "sports": sports,
                "odds": oddsb, "recent": recent, "chats": len(chats), "queue": self.qlen(), "queue_photo": self.qlen(True),
                "backfill_left": self.backfill_left(), "focus": self.d["chats"].get(fc, "") if fc else "",
                "web_used": self.d.get("web_used", 0),
                "search": {"n": self.d.get("sweb_used", 0), "ok": self.d.get("sweb_ok", 0),
                           "last": (props.SEARCH_STATE.get("last") if props and hasattr(props, "SEARCH_STATE") else "")},
                "signal": [c for c, nm in self.d["chats"].items()
                           if any(_n(x) and _n(x) in _n(nm) for x in (self.cfg.get("groups") or []))],
                "sofascore": (props.SOFA_STATE.get("last") if props else ""),
                "apifootball": (props.APIS_STATE.get("last") if props else ""),
                "s365": (props.S365_STATE.get("last") if props else ""),
                "sportscore": (props.SPS_STATE.get("last") if props else ""),
                "done_msgs": self.d["done_msgs"], "ai_used": self.d["ai_used"] + self.d.get("groq_used", 0),
                "ai_cap": self.cap + (self.gcap if self.cfg.get("groq_key") else 0),
                "groq_used": self.d.get("groq_used", 0), "gemini_used": self.d["ai_used"],
                "ocr_used": self.d.get("ocr_used", 0), "ocr": bool(self._ocr), "dropped": self.d.get("dropped", 0),
                "alerts": self.d.get("alerts", 0), "errors": dict(sorted(self.d.get("err", {}).items(), key=lambda x: -x[1])[:8]), "gemini_cap": self.cap,
                "bets": len(bets), "days": self.days, "paused": time.time() < self.d.get("pause_until", 0)}

    # ------------------------------------------------------------------ amžinas ciklas
    def odds_some(self, limit=6):
        """Prototipas „ar galima pastatyti“: kiekvienam futbolo statymui – bukmekerių koeficientas prieš rungtynes."""
        if not props or not hasattr(props, "market_odds") or not self.cfg.get("apisports_key"):
            return
        cut = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        done = 0
        for b in sorted(self.d["bets"], key=lambda x: str(x["t"]), reverse=True):
            if done >= limit:
                break
            if "mkt" in b or str(b["t"])[:10] < cut or b.get("mkt_try", 0) >= 3 or b.get("mkt_next", 0) > time.time():
                continue
            if any("ne prognozė" in (l.get("detail") or "") for l in b["legs"]):
                b["mkt"] = None                           # paskelbta po rungtynių pradžios – pastatyti nebuvo galima
                continue
            got, why = [], ""
            for l in b["legs"]:
                if "mkt" not in l:
                    lg = dict(l, kickoff=self._kick(l, b).isoformat(timespec="minutes"), created=str(b["t"])[:16])
                    try:
                        r = props.market_odds(lg)
                    except Exception as e:
                        r = None
                        self._err(f"koef.: {e}")
                    done += 1
                    if r is None:                         # rungtynių API-Sports nėra arba limitas – vėliau
                        break
                    l["mkt"] = r
                r = l.get("mkt")
                if not r or "odds" not in r:
                    why = (r or {}).get("none", "")
                    break
                got.append(r["odds"])
            else:
                o = 1.0
                for x in got:
                    o *= x
                b["mkt"] = {"odds": round(o, 2), "posted": b.get("odds"), "legs": len(got)}
                try:
                    po = float(b.get("odds") or 0)
                except Exception:
                    po = 0
                if po > 1.01 and not (0.7 <= o / po <= 1.45):   # grupės ir bukmekerių koef. labai skiriasi –
                    b["mkt"] = {"none": f"rinka neatitinka (grupė {po:g}, bukmekeriai {o:.2f})"}   # tikriausiai kita rinka
                continue
            if why:
                b["mkt"] = {"none": why}
            else:
                b["mkt_try"] = b.get("mkt_try", 0) + 1
                b["mkt_next"] = time.time() + 3 * 3600

    async def settle_loop(self):
        """Rezultatai tikrinami atskirai nuo žinučių analizės – nelaukia nuotraukų eilės."""
        last_pf = 0
        while True:
            if props and time.time() - last_pf > 3600:
                last_pf = time.time()
                try:
                    await asyncio.to_thread(props.apis_prefetch)
                except Exception as e:
                    self._err(f"api-sports: {e}")
            try:
                await asyncio.to_thread(self.settle_some)
                await asyncio.to_thread(self.odds_some)
                self.save()
            except Exception as e:
                self.log.info("audit rezultatai: %s", e)
            await asyncio.sleep(30)

    async def run(self):
        await asyncio.sleep(90)
        asyncio.get_running_loop().create_task(self.settle_loop())
        last_collect = 0
        while True:
            try:
                if time.time() - self.d.get("discovered", 0) > 86400 or not self.d["chats"]:
                    await self.discover()
                # kol renkama istorija – dažniau (kas 3 min.), vėliau – naujos žinutės kas 15 min.
                if time.time() - last_collect > 180:           # naujos žinutės – kas 3 min.
                    await self.collect()
                    last_collect = time.time()
                await self.analyze_some(5)
                self.save()
            except Exception as e:
                self.log.info("audit klaida: %s", e)
            await asyncio.sleep(20)
