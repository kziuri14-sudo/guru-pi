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
import subprocess
import time
import urllib.error
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
  "bets": [{{"sport": "soccer/basketball/tennis/hockey/handball/volleyball/baseball/american_football/mma/esports/kita",
    "league": "...", "home": "...", "away": "...",
    "date": "YYYY-MM-DD" arba null, "time": "HH:MM" arba null,
    "side": "Win|DC|BTTS|Over|Under|Spread|Kita", "team": "kuriai komandai arba null",
    "line": skaičius arba null, "pick": "statymas originaliai", "odds": skaičius arba null, "women": true/false,
    "player": "žaidėjo vardas, jei statoma už žaidėjo statistiką (taškai, perdavimai, atkovoti, įvarčiai), kitaip null"}}]}}]}}
Žinučių be statymo NEĮTRAUK. Jei statymų nėra – {{"msgs": []}}.
Lygiosios = side "Win", team "Draw". „Abi įmuš“ = BTTS (pick „Yes“/„No“). „1X“, „X2“, „12“ = DC.
Datą imk iš žinutės arba jos laiko. Komandas rašyk angliškai. NIEKO NESUGALVOK – ko nėra, rašyk null.

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
        self._keys = {(b["c"], b["mid"]) for b in self.d["bets"]}
        self._done, self._last_save, self._sum = [], 0, (0, None)
        self._ocr = False

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
            if (score >= 0.12 and hits >= 3) or str(dlg.id) in self.d["chats"]:
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

    def backfill_left(self):
        return sum(1 for c in self.d["chats"] if self.d["bf_done"].get(c, 0) < self.days)

    async def collect(self, per_chat=3000):
        since = datetime.now().astimezone() - timedelta(days=self.days)
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
        return self._groq_ok() or self._gem_ok()

    def _batch(self, photos_ok):
        """Seniausia neišanalizuota žinutė ir paskui ją einančios tos pačios grupės žinutės (kontekstas)."""
        where = " AND tries<99" + ("" if photos_ok else " AND photo=0")
        recent = (datetime.now().astimezone() - timedelta(hours=36)).isoformat(timespec="minutes")
        first = self.db.execute("SELECT c, t FROM q WHERE t>=?" + where + " ORDER BY t, id LIMIT 1", (recent,)).fetchone() \
            or self.db.execute("SELECT c, t FROM q WHERE 1=1" + where + " ORDER BY t, id LIMIT 1").fetchone()
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
                             "kickoff": kick, "created": t[:16], "status": "pending"})
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
                       r"nepalaikoma|ne rungtynių komanda")

    def settle_some(self, limit=60):
        now = datetime.now()
        n = 0
        for b in list(reversed(self.d["bets"])):          # naujausi pirmi – šviežūs rezultatai greičiau
            if b["status"] != "pending" or n >= limit:
                continue
            if b.get("next", 0) > time.time():
                continue
            n0 = n
            waiting = 0
            for l in b["legs"]:
                if l["status"] != "pending":
                    continue
                k = self._kick(l, b)
                if k + timedelta(hours=2, minutes=15) > now:
                    l["note"] = "laukia rungtynių pabaigos"
                    continue
                n += 1
                info = ""
                try:
                    lg = dict(l, kickoff=k.isoformat(timespec="minutes"))
                    if props and props.is_prop(l):                  # žaidėjai, kampiniai, kortelės – EuroLeague / ESPN
                        st, detail, info = props.settle(lg)
                    else:
                        st, detail, info = results.settle(lg)
                        if not st and props and re.search(r"žaidėj|ne rungtynių komanda|neįvertina", info or ""):
                            st, detail, info = props.settle(lg)
                except Exception as e:
                    st, detail, info = None, None, f"klaida: {str(e)[:80]}"
                if st:
                    l["status"], l["detail"] = st, detail
                    l.pop("note", None)
                elif info and self.FINAL.search(info):
                    l["status"], l["detail"] = "unknown", info + " – neįskaitoma"   # šito patikrinti neįmanoma
                    l.pop("note", None)
                else:
                    l["note"] = info or "tikrinama"
                    if "dar vyksta arba neprasidėjo" in (info or ""):
                        waiting += 1                      # rungtynės dar nesibaigė (pvz. kombo kitos dienos dalis)
            sts = [l["status"] for l in b["legs"]]
            if "lost" in sts:
                b["status"] = "lost"
            elif all(x in ("won", "push") for x in sts):
                b["status"] = "push" if all(x == "push" for x in sts) else "won"
            elif "pending" not in sts:
                b["status"] = "unknown"                   # bent viena dalis nepatikrinama – kombo neįskaitom
            elif waiting and str(b["t"])[:10] >= (now - timedelta(days=4)).strftime("%Y-%m-%d") and waiting == sum(1 for l in b["legs"] if l["status"] == "pending" and l.get("note") and
                                            "dar vyksta arba neprasidėjo" in l["note"]) and \
                    all("dar vyksta arba neprasidėjo" in (l.get("note") or "") or l.get("note") == "laukia rungtynių pabaigos"
                        for l in b["legs"] if l["status"] == "pending"):
                b["next"] = time.time() + 1800            # LiveScore rodo, kad dar nesibaigė – bandymų neskaičiuojam
            elif n == n0:                                 # rungtynės dar nesibaigė – laukiam, bandymų neskaičiuojam
                try:
                    b["next"] = min(self._kick(l, b) for l in b["legs"]
                                    if l["status"] == "pending").timestamp() + 2.25 * 3600
                except Exception:
                    b["next"] = time.time() + 3600
            else:
                b["tries"] += 1
                b["next"] = time.time() + (1200 if b["tries"] <= 3 else 2 * 3600)
                try:
                    old = max(self._kick(l, b) for l in b["legs"]) < now - timedelta(hours=20)
                except Exception:
                    old = True
                if (old and b["tries"] >= 5) or b["tries"] > 12:
                    b["status"] = "unknown"          # rezultato rasti nepavyko – į statistiką neskaičiuojam
                    for l in b["legs"]:
                        if l["status"] == "pending":
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
        for b in sorted(bets, key=lambda x: x["t"], reverse=True)[:150]:
            recent.append({"t": b["t"], "c": b["c"], "chat": chats.get(b["c"], b["c"]), "who": b.get("un"),
                           "status": b["status"], "odds": b.get("odds"), "photo": b.get("photo", False),
                           "legs": [{"m": f"{l.get('home')} – {l.get('away') or ''}".strip(" –"), "pick": l.get("pick"),
                                     "st": l.get("status"),
                                     "d": (l.get("detail") or (("⏳ " + l["note"]) if l.get("note") else ""))[:110],
                                     "k": str(self._kick(l, b))[5:16]} for l in b["legs"][:6]]})
        return {"groups": groups, "total": tot, "series": series, "hours": hours, "sides": sides, "sports": sports,
                "odds": oddsb, "recent": recent, "chats": len(chats), "queue": self.qlen(), "queue_photo": self.qlen(True),
                "backfill_left": self.backfill_left(),
                "done_msgs": self.d["done_msgs"], "ai_used": self.d["ai_used"] + self.d.get("groq_used", 0),
                "ai_cap": self.cap + (self.gcap if self.cfg.get("groq_key") else 0),
                "groq_used": self.d.get("groq_used", 0), "gemini_used": self.d["ai_used"],
                "ocr_used": self.d.get("ocr_used", 0), "ocr": bool(self._ocr), "dropped": self.d.get("dropped", 0),
                "alerts": self.d.get("alerts", 0), "errors": dict(sorted(self.d.get("err", {}).items(), key=lambda x: -x[1])[:8]), "gemini_cap": self.cap,
                "bets": len(bets), "days": self.days, "paused": time.time() < self.d.get("pause_until", 0)}

    # ------------------------------------------------------------------ amžinas ciklas
    async def settle_loop(self):
        """Rezultatai tikrinami atskirai nuo žinučių analizės – nelaukia nuotraukų eilės."""
        while True:
            try:
                await asyncio.to_thread(self.settle_some)
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
