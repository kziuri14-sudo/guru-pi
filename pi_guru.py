"""Raspberry Pi: GuruGoal signalai -> garsus pranešimas telefone (ntfy) su nuoroda į fs7.casino.

Nieko nestato. Kiekviena nauja grupės žinutė (tekstas arba kupono nuotrauka):
  1) Google AI (debesyje, ne ant Pi) per kelias sekundes perskaito: rungtynės, statymas, koeficientas;
  2) jei tai statymas – iškart siunčiamas skubus pranešimas su nuotrauka ir nuoroda į rungtynes casino.
Jei AI neveikia – pranešimas siunčiamas vis tiek (su originaliu tekstu/nuotrauka).
"""
import asyncio
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.header import Header
from pathlib import Path

from telethon import TelegramClient, events

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "pi_config.json").read_text("utf-8"))
LOGF = Path(CFG.get("log_file") or (BASE / "guru.log"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOGF, encoding="utf-8")])
log = logging.getLogger("guru")
for noisy in ("telethon",):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# papildomi moduliai: jei kortelėje (boot) naujesni – pasiimam (pirmas įdiegimas per kortelę)
for _f in ("pi_audit.py", "results.py", "props.py", "dash.html"):
    try:
        _src, _dst = Path("/boot/firmware/guru") / _f, BASE / _f
        if _src.exists() and (not _dst.exists() or _src.stat().st_mtime > _dst.stat().st_mtime):
            _dst.write_bytes(_src.read_bytes())
    except Exception:
        pass

SPORTS = "https://fs7.casino/en/betting/sports"
NTFY = CFG.get("ntfy_server", "https://ntfy.sh").rstrip("/") + "/" + CFG["ntfy_topic"]


def norm(s):
    # NFKC: „𝗚𝗨𝗥𝗨“ (paryškintos Unicode raidės) -> „GURU“
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", s or "").lower())


def slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def hdr(v):
    """ntfy antraštės – UTF-8 per RFC 2047, VIENOJE eilutėje (ilgas tekstas skaidomas į kelis
    „=?UTF-8?B?…?=“ žodžius per tarpą; eilutės lūžis antraštėje = HTTP 400)."""
    v = str(v).replace("\r", "").replace("\n", "\\n")
    try:
        v.encode("ascii")
        return v
    except UnicodeEncodeError:
        pass
    words, cur = [], b""
    for ch in v:
        e = ch.encode("utf-8")
        if len(cur) + len(e) > 45:
            words.append(cur)
            cur = b""
        cur += e
    if cur:
        words.append(cur)
    return " ".join("=?UTF-8?B?" + base64.b64encode(w).decode() + "?=" for w in words)


def ntfy(title, message, click=None, image=None, priority=5):
    h = {"Title": hdr(title[:200]), "Priority": str(priority), "Tags": "moneybag"}
    if click:
        h["Click"] = click
        h["Actions"] = f"view, Atidaryti casino, {click}, clear=true"
    if image:
        h["Filename"] = "kuponas.jpg"
        h["Message"] = hdr(message[:3000])
        data = image
    else:
        data = message[:3500].encode("utf-8")
    req = urllib.request.Request(NTFY, data=data, headers=h, method="PUT" if image else "POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        # atsarginis kelias: JSON (be nuotraukos) – antraštės nenaudojamos, todėl neužstringa ant simbolių
        log.info("ntfy %s – siunčiu JSON būdu", e.code)
        body = {"topic": CFG["ntfy_topic"], "title": title[:200], "message": message[:3500],
                "priority": int(priority), "tags": ["moneybag"]}
        if click:
            body["click"] = click
            body["actions"] = [{"action": "view", "label": "Atidaryti casino", "url": click, "clear": True}]
        req = urllib.request.Request(CFG.get("ntfy_server", "https://ntfy.sh").rstrip("/"),
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status


PROMPT = """Tai žinutė iš Telegram lažybų grupės (tekstas ir/ar kupono nuotrauka).
Atsakyk TIK JSON: {"is_bet": true/false, "bets": [{"home": "...", "away": "...", "pick": "...",
"odds": 1.85, "date": "DD.MM" arba null, "sport": "football/basketball/tennis/hockey/...",
"women": true jei tai moterų rungtynės (Women, W, moterys, WSL, UWCL ir pan.), kitaip false, "live": true jei statoma gyvai}],
"total_odds": kombo bendras koef. arba null, "summary": "trumpai lietuviškai: kas statoma"}
is_bet=true tik jei žinutėje siūlomas KONKRETUS statymas (ne reklama, ne rezultatų pasigyrimas,
ne jau laimėtas/pralaimėtas kuponas). Komandų pavadinimus rašyk angliškai, kaip bukmekerio puslapyje.
NIEKO NESUGALVOK: jei žinutėje nėra antros komandos, koeficiento ar datos – rašyk null."""


def ai(text, image):
    key = CFG.get("gemini_key")
    if not key:
        return None
    parts = [{"text": PROMPT + "\n\nŽINUTĖ:\n" + (text or "(be teksto)")}]
    if image:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode()}})
    body = json.dumps({"contents": [{"role": "user", "parts": parts}],
                       "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}).encode()
    for model in CFG.get("gemini_models") or ["gemini-3.5-flash-lite", "gemini-3.5-flash"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=float(CFG.get("ai_timeout", 9))) as r:
                d = json.loads(r.read().decode("utf-8"))
            t = d["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.sub(r"^```(json)?|```$", "", t.strip()))
        except Exception as e:
            log.info("AI (%s) nepavyko: %s", model, str(e)[:150])
    r = groq_call(CFG, PROMPT + "\n\nŽINUTĖ:\n" + (text or "(be teksto)"), image, timeout=12, log=log)   # atsarginis
    if isinstance(r, dict):
        log.info("AI: panaudotas Groq (Gemini neatsakė)")
        return r
    return None


_GROQ_M = {"t": 0, "ids": []}


def _groq_models(key, vision, cfg, log=None):
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
    return list(dict.fromkeys(cand))[:2]


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


def url_ok(url):
    """fs7 visada grąžina 200 – tikrinam, ar įvykis yra ir ar tai NE e-sportas (FIFA/eFootball turi tuos pačius pavadinimus)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) Chrome/120 Safari/537.36",
                                                   "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=6) as r:
            body = r.read(2_000_000).decode("utf-8", "ignore")
        if "No event found with slug" in body or len(body) < 50_000:
            return False
        flat = body.replace('\\', '')
        slug_ = url.rstrip("/").rsplit("/", 1)[-1]
        k = flat.find(f'"slug":"{slug_}"')
        seg = flat[max(0, k - 2000):k + 2000] if k >= 0 else flat[:0]
        if re.search(r"(?i)esports?|e-sports?|cyber|efootball|ebasketball", seg):
            log.info("↪ %s – e-sportas, praleidžiu", slug_)
            return False
        return True
    except Exception:
        return False


STRIP = {"fc", "cf", "afc", "sc", "ac", "fk", "sk", "nk", "bk", "kk", "cd", "ud", "sv", "vfb", "vfl", "1"}


def _variants(name, women):
    base = slug(name)
    if not base:
        return []
    parts = [p for p in base.split("-") if p]
    short = "-".join(p for p in parts if p not in STRIP and p not in ("women", "w", "ladies", "fem")) or base
    out = [short, base] if short != base else [base]
    if women:
        out = [f"{v}-w" for v in out] + out
    return list(dict.fromkeys(out))[:3]


def casino_link(b, live=False):
    """fs7 rungtynių adresas: /prematch/match/<namai>-vs-<svečiai>-<DD>-<MM>. Bandome kelis pavadinimų variantus
    (be „FC“, moterų „-w“) ir tikrinam, ar įvykis tikrai yra ir nėra e-sportas."""
    women = bool(b.get("women"))
    hv, av = _variants(b.get("home"), women), _variants(b.get("away"), women)
    if not hv or not av:
        return None
    m = re.match(r"(\d{1,2})\D(\d{1,2})", str(b.get("date") or ""))
    d = f"{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else datetime.now().strftime("%d-%m")
    kinds = ("live", "prematch") if (live or b.get("live")) else ("prematch", "live")
    tried = 0
    for h in hv:
        for a in av:
            for kind in kinds:
                if tried >= 10:
                    return None
                tried += 1
                u = f"{SPORTS}/{kind}/match/{h}-vs-{a}-{d}"
                if url_ok(u):
                    return u
    return None


GENERIC = {"fc", "cf", "sc", "ac", "afc", "fk", "nk", "sk", "bk", "kk", "club", "city", "united", "team", "res", "reserves",
           "u19", "u20", "u21", "u23", "women", "real", "sporting", "athletic", "deportivo", "the", "and", "vs"}


def search_link(text, bets):
    """fs7 paieška su jau įvestu komandos pavadinimu (telefone atsidaro paieškos langas su rezultatais)."""
    words = []
    for b in bets[:1]:
        for team in (b.get("home"), b.get("away")):
            for w in re.findall(r"[^\W\d_]{3,}", str(team or "")):
                if w.lower() not in GENERIC:
                    words.append(w)
    if not words:
        return None
    term = max(words, key=len)                       # ilgiausias žodis – dažniausiai unikalus (Rzeszow, Manchester)
    kind = "live" if re.search(r"(?i)\blive\b|gyvai", text or "") else "prematch"
    return f"https://fs7.casino/en/betting/sports/search?sportEventStatusSlug={kind}&searchString={urllib.parse.quote(term)}"


async def handle(msg, name):
    t0 = time.time()
    text = msg.message or ""
    image = None
    if msg.photo:
        try:
            image = await msg.download_media(bytes)
        except Exception as e:
            log.info("Nuotraukos parsisiųsti nepavyko: %s", e)
    if not text.strip() and not image:
        return
    res = await asyncio.to_thread(ai, text, image)
    STATS["last_msg"] = now_iso()
    if res is None:
        STATS["ai_fail"] = STATS.get("ai_fail", 0) + 1
    if res is not None and not res.get("is_bet"):
        log.info("⏭ ne statymas: %s", (res.get("summary") or text)[:100])
        add_event("skip", (res.get("summary") or text)[:120], time.time() - t0)
        return
    bets = (res or {}).get("bets") or []
    link = None
    if bets:
        is_live = bool(re.search(r"(?i)\blive\b|gyvai", text))
        links = await asyncio.gather(*[asyncio.to_thread(casino_link, b, is_live) for b in bets[:3]])
        link = next((l for l in links if l), None)
        found_exact = bool(link)
        if not link:
            link = search_link(text, bets)
    if bets:
        vs = lambda b: " – ".join(str(x) for x in (b.get("home"), b.get("away")) if x and str(x) != "None")
        title = f"⚽ {name}: " + (f"kombo {len(bets)}" if len(bets) > 1 else vs(bets[0]))
        lines = [f"{vs(b)} | {b.get('pick')} @ {b.get('odds') or '?'}" for b in bets]
        if len(bets) > 1 and res.get("total_odds"):
            lines.append(f"Bendras koef. {res['total_odds']}")
        body = "\n".join(lines)
    else:
        title = f"⚽ {name}: naujas signalas"
        body = text[:600] or "(nuotrauka)"
    exact = bets and locals().get("found_exact")
    body += "\n" + ("Paspausk – atsidarys rungtynės fs7.casino." if exact else
                    "Paspausk – fs7 paieška su jau įvestu pavadinimu." if link else
                    "Paspaudus atsidarys fs7 sporto puslapis.")
    if SETTINGS.get("paused") or not SETTINGS.get("notify_guru", True):
        log.info("⏸ pranešimai sustabdyti – nesiųsta: %s", title)
        add_event("skip", "(sustabdyta) " + title, time.time() - t0, link=link)
        return
    try:
        st = await asyncio.to_thread(ntfy, title, body, link or SPORTS, image, 2 if quiet_now() else 5)
        log.info("📱 išsiųsta (%s) per %.1f s: %s", st, time.time() - t0, title)
        add_event("signal", title + "\n" + body.split("\nPaspaus")[0],
                  time.time() - t0, link=link)
    except Exception as e:
        log.error("❗ ntfy klaida: %s", e)
        add_event("error", f"ntfy: {e}"[:120], time.time() - t0)


# ---------------------------------------------------------------- nuotolinis atnaujinimas
# Kompiuteris (PI_ATNAUJINTI.bat) į TAVO „Saved Messages“ įdeda failą su žyma #pi_update –
# Pi jį patikrina, pasikeičia ir persikrauna. #pi_status – Pi atsiunčia žurnalo pabaigą (tylus ntfy).
# Į „Saved Messages“ rašyti gali tik tu pats, todėl niekas kitas Pi atnaujinti negali.
ALLOWED = {"pi_guru.py", "pi_config.json", "pi_audit.py", "results.py", "props.py", "dash.html"}
BOOT = Path("/boot/firmware/guru")
_restart = {"h": None}


def _write(name, data):
    for d in (BASE, BOOT):
        try:
            tmp = d / (name + ".new")
            tmp.write_bytes(data)
            os.replace(tmp, d / name)
        except Exception as e:
            log.info("Nepavyko įrašyti %s į %s: %s", name, d, e)


def setup_remote(client):
    @client.on(events.NewMessage(chats="me"))
    async def on_me(ev):
        m = ev.message
        txt = m.message or ""
        mk = re.match(r"(?is)\s*#pi_key\s+(apifootball|api-football|apisports|api-sports|api|tavily)\s+(\S+)", txt)
        if mk:                                            # raktas įrašomas tik Raspberry – ne į GitHub
            fld = "tavily_key" if mk.group(1).lower() == "tavily" else "apisports_key"
            CFG[fld] = mk.group(2).strip()
            for path in (BASE / "pi_config.json", BOOT / "pi_config.json"):
                try:
                    cur = json.loads(path.read_text("utf-8"))
                    cur[fld] = CFG[fld]
                    path.write_text(json.dumps(cur, ensure_ascii=False, indent=1), "utf-8")
                except Exception as e:
                    log.info("raktas %s: %s", path, e)
            try:
                await m.delete()                          # žinutę su raktu ištrinam iš Saved Messages
            except Exception:
                pass
            if fld == "tavily_key":
                await asyncio.to_thread(ntfy, "🔑 Tavily raktas išsaugotas",
                                        "Paieška internete įjungta – nerastų rungtynių rezultatų ieškos internete. Žinutė su raktu ištrinta.", None, None, 3)
            else:
                await asyncio.to_thread(ntfy, "🔑 API-Sports raktas išsaugotas",
                                        "Futbolas, krepšinis, ledo ritulys – Raspberry naudos rezultatų paieškai. Žinutė su raktu ištrinta.", None, None, 3)
            return
        if "#pi_status" in txt:
            try:
                tail = LOGF.read_text("utf-8", errors="replace").splitlines()[-25:]
            except Exception as e:
                tail = [f"žurnalo nėra: {e}"]
            await asyncio.to_thread(ntfy, "ℹ️ Raspberry būsena", "\n".join(tail)[-3500:], None, None, 1)
            return
        if re.search(r"(?i)#pi[_ ]?(github|git|atnaujink|update)\b", txt) and not m.file:
            GH_NOW.set()                              # „#pi_github“ – tikrinti GitHub iškart
            await asyncio.to_thread(ntfy, "🔎 Raspberry tikrina GitHub", "Jei yra naujų failų – atsinaujins per minutę.",
                                    None, None, 2)
            return
        if "#pi_update" not in txt:
            # TESTAS: persiųsta žinutė (pvz. iš GURU G⚽AL) arba žinutė/nuotrauka su #test
            if getattr(m, "fwd_from", None) or "#test" in txt.lower():
                m.message = re.sub(r"(?i)#test\w*", "", txt).strip()
                log.info("🧪 Testas iš Saved Messages")
                await handle(m, "🧪 TESTAS")
            return
        if not m.file or m.file.name not in ALLOWED:
            return
        name = m.file.name
        data = await m.download_media(bytes)
        try:
            if name.endswith(".py"):
                compile(data, name, "exec")
            elif name.endswith(".html"):
                assert b"<html" in data[:500].lower()
            else:
                json.loads(data.decode("utf-8"))
        except Exception as e:
            log.error("❗ Atnaujinimas %s atmestas: %s", name, e)
            await asyncio.to_thread(ntfy, "❗ Raspberry atnaujinimas atmestas", f"{name}: {e}", None, None, 3)
            return
        _write(name, data)
        log.info("🔄 Atnaujinta: %s (%d B) – persikrausiu", name, len(data))
        await asyncio.to_thread(ntfy, "🔄 Raspberry atnaujintas", f"{name} – persikrauna", None, None, 2)
        loop = asyncio.get_running_loop()
        if _restart["h"]:
            _restart["h"].cancel()
        _restart["h"] = loop.call_later(15, os._exit, 0)      # systemd paleis iš naujo su nauju failu


# ---------------------------------------------------------------- būsenos puslapis (telefonui)
STATS_F = BASE / "stats.json"
try:
    STATS = json.loads(STATS_F.read_text("utf-8"))
except Exception:
    STATS = {}
STATS.setdefault("events", [])
STATS.setdefault("samples", [])
STATS["started"] = datetime.now().isoformat(timespec="seconds")
STATE = {"tg": False, "chats": [], "url": None}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_stats():
    try:
        cut = time.time() - 7 * 86400
        STATS["events"] = [e for e in STATS["events"] if e.get("ts", 0) > cut][-500:]
        STATS["samples"] = [x for x in STATS["samples"] if x.get("ts", 0) > cut][-2100:]
        tmp = STATS_F.with_suffix(".tmp")
        tmp.write_text(json.dumps(STATS, ensure_ascii=False), "utf-8")
        os.replace(tmp, STATS_F)
    except Exception as e:
        log.info("stats: %s", e)


def add_event(kind, text, secs, link=None):
    STATS["events"].append({"ts": time.time(), "t": now_iso(), "kind": kind, "text": text,
                            "secs": round(secs, 1), "link": link})
    save_stats()


def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except Exception:
        return default


_CPU = {"prev": None}


def _cpu_times():
    v = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v), idle


def cpu_util(update=True):
    """Procesoriaus apkrova % nuo paskutinio matavimo (visi branduoliai)."""
    try:
        tot, idle = _cpu_times()
    except Exception:
        return 0.0
    prev = _CPU["prev"]
    if update or prev is None:
        _CPU["prev"] = (tot, idle)
    if not prev or tot <= prev[0]:
        return 0.0
    return round(100 * (1 - (idle - prev[1]) / (tot - prev[0])), 1)


def throttled():
    """vcgencmd get_throttled: ar maitinimas per silpnas / ar mažinamas dažnis."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        v = int(out.strip().split("=")[1], 16)
        return {"now_low_voltage": bool(v & 1), "now_throttled": bool(v & 4), "ever_low_voltage": bool(v & 0x10000)}
    except Exception:
        return None


def watts(cpu):
    """Raspberry Pi 4 neturi elektros matuoklio – apskaičiuojam pagal apkrovą:
    ~2.6 W ramybėje (be ekrano, Wi-Fi ir Bluetooth išjungti), ~6.0 W visiškai apkrautas."""
    lo = float(CFG.get("power_idle_w", 2.6))
    hi = float(CFG.get("power_max_w", 6.0))
    return round(lo + (hi - lo) * min(max(cpu, 0), 100) / 100, 2)


def sample(update=True):
    temp = round(int(_read("/sys/class/thermal/thermal_zone0/temp", "0")) / 1000, 1)
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0])
    used = 100 - round(mem.get("MemAvailable", 0) / max(mem.get("MemTotal", 1), 1) * 100)
    du = shutil.disk_usage("/")
    cpu = cpu_util(update)
    return {"ts": time.time(), "t": now_iso(), "temp": temp, "load": round(os.getloadavg()[0], 2),
            "cpu": cpu, "w": watts(cpu), "mem": used, "disk": round(du.used / du.total * 100), "tg": STATE["tg"], "ping": STATE.get("ping")}


def energy():
    """kWh šiandien ir per paskutines 24 val. (iš 5 min. matavimų), mėnesio prognozė ir kaina."""
    sm = [x for x in STATS["samples"] if "w" in x]
    price = float(SETTINGS.get("kwh_price", CFG.get("kwh_price", 0.25)))
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    kwh_today = kwh_24 = 0.0
    for a, b in zip(sm, sm[1:]):
        dt = min(b["ts"] - a["ts"], 900)                  # tarpai (Pi buvo išjungtas) neskaičiuojami
        e = (a["w"] + b["w"]) / 2 * dt / 3600 / 1000
        if b["ts"] >= midnight:
            kwh_today += e
        if b["ts"] >= time.time() - 86400:
            kwh_24 += e
    avg_w = sum(x["w"] for x in sm[-288:]) / len(sm[-288:]) if sm else watts(0)
    month = avg_w * 24 * 30 / 1000
    return {"kwh_today": round(kwh_today, 3), "kwh_24h": round(kwh_24, 3), "avg_w": round(avg_w, 2),
            "kwh_month": round(month, 2), "eur_month": round(month * price, 2), "price": price}


async def sampler(client):
    while True:
        STATE["tg"] = client.is_connected()
        smp = sample()
        smp["ping"] = await asyncio.to_thread(ping_ms)
        STATE["ping"] = smp["ping"]
        STATS["samples"].append(smp)
        save_stats()
        await asyncio.sleep(300)


SET_F = BASE / "settings.json"
try:
    SETTINGS = json.loads(SET_F.read_text("utf-8"))
except Exception:
    SETTINGS = {}
SETTINGS.setdefault("token", secrets.token_urlsafe(12))
SETTINGS.setdefault("guest_key", secrets.token_urlsafe(9))        # svečių (antras) prisijungimas
SETTINGS.setdefault("guest_topic", "gs-" + secrets.token_hex(8))  # svečių programėlės kodas (atskira ntfy tema)
SETTINGS.setdefault("guest_on", True)
for k, v in {"paused": False, "quiet": False, "quiet_from": "23:00", "quiet_to": "08:00",
             "kwh_price": float(CFG.get("kwh_price", 0.25)),
             "notify_guru": True,          # GURU G⚽AL kanalo signalai
             "follow": [],                 # pažymėtos grupės „g:<id>“ ir žmonės „m:<id>:<narys>“ – tik iš jų pranešimai
             "auto_proven": False,
             "consensus_min": 3}.items():  # 🔥 pranešimas, kai tą patį stato ≥N skirtingų šaltinių (0 – išjungta)  # ⭐ pranešimai ir apie nepažymėtas, bet statistiškai patikimas grupes
    SETTINGS.setdefault(k, v)


def save_settings():
    try:
        SET_F.write_text(json.dumps(SETTINGS, ensure_ascii=False), "utf-8")
    except Exception as e:
        log.info("settings: %s", e)


save_settings()


def quiet_now():
    if not SETTINGS.get("quiet"):
        return False
    try:
        now = datetime.now().strftime("%H:%M")
        f, t = SETTINGS["quiet_from"], SETTINGS["quiet_to"]
        return (f <= now < t) if f <= t else (now >= f or now < t)
    except Exception:
        return False


def net_bytes():
    rx = tx = 0
    for ln in _read("/proc/net/dev").splitlines()[2:]:
        name, data = ln.split(":", 1)
        if name.strip() == "lo":
            continue
        v = data.split()
        rx += int(v[0])
        tx += int(v[8])
    return rx, tx


_LIVE = {"cpu": None, "net": None}


def live_json():
    """Greitas matavimas puslapiui kas 2 s (skaičiuojama tik kai puslapis atidarytas)."""
    now = time.time()
    tot, idle = _cpu_times()
    rx, tx = net_bytes()
    pc, pn = _LIVE["cpu"], _LIVE["net"]
    cpu = round(100 * (1 - (idle - pc[1]) / (tot - pc[0])), 1) if pc and tot > pc[0] else cpu_util(False)
    if pn and now > pn[0]:
        rxs, txs = max(0, (rx - pn[1]) / (now - pn[0])), max(0, (tx - pn[2]) / (now - pn[0]))
    else:
        rxs = txs = 0
    _LIVE["cpu"], _LIVE["net"] = (tot, idle), (now, rx, tx)
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0])
    return {"ts": now, "cpu": max(cpu, 0), "w": watts(cpu), "load": round(os.getloadavg()[0], 2),
            "temp": round(int(_read("/sys/class/thermal/thermal_zone0/temp", "0")) / 1000, 1),
            "mem": 100 - round(mem.get("MemAvailable", 0) / max(mem.get("MemTotal", 1), 1) * 100),
            "rx": round(rxs), "tx": round(txs)}


def ping_ms():
    try:
        t0 = time.time()
        with urllib.request.urlopen(CFG.get("ntfy_server", "https://ntfy.sh").rstrip("/") + "/v1/health", timeout=8) as r:
            r.read()
        return round((time.time() - t0) * 1000)
    except Exception:
        return None


def api_json():
    cur = sample(update=False)
    up = float(_read("/proc/uptime", "0 0").split()[0])
    return {"now": now_iso(), "uptime_h": round(up / 3600, 1), "started": STATS.get("started"),
            "current": cur, "chats": STATE["chats"], "last_msg": STATS.get("last_msg"),
            "ai_fail": STATS.get("ai_fail", 0), "events": STATS["events"][-200:],
            "energy": energy(), "power": throttled(), "quiet_now": quiet_now(),
            "audit": _audit_summary(),
            "settings": {k: v for k, v in SETTINGS.items() if k != "token"},
            "samples": STATS["samples"][-600:]}


DASH_HTML = r"""<!doctype html><html lang="lt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d1912"><link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg"><link rel="apple-touch-icon" href="/icon.svg">
<meta name="apple-mobile-web-app-capable" content="yes"><title>Guru Pi</title>
<style>
/* Telefono stulpelis: gyva būsena -> skaičiai -> gyvas grafikas -> istorija -> signalai -> nustatymai */
:root{--bg:#eef3f0;--card:#fff;--fg:#13201a;--mut:#5b6e64;--line:#dbe5df;--acc:#16955b;--acc2:#2f6fd6;--warn:#c9781c;--bad:#d14343;
--f:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0d1912;--card:#14231b;--fg:#e5eee8;--mut:#8aa396;--line:#22372b;--acc:#39cc89;--acc2:#6aa2ff;color-scheme:dark}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 var(--f);
padding:max(14px,env(safe-area-inset-top)) max(16px,env(safe-area-inset-right)) max(24px,env(safe-area-inset-bottom)) max(16px,env(safe-area-inset-left))}
.wrap{max-width:560px;margin:0 auto}
h1{font-size:21px;margin:0;display:flex;gap:8px;align-items:center}
.row{display:flex;gap:10px;align-items:center;justify-content:space-between}
.pill{padding:4px 11px;border-radius:99px;font-weight:650;font-size:13px;color:#fff;background:var(--acc);white-space:nowrap}
.pill.bad{background:var(--bad)}.pill.warn{background:var(--warn)}
.live{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--mut);font-weight:600;letter-spacing:.05em}
.dot{width:8px;height:8px;border-radius:50%;background:var(--acc);box-shadow:0 0 0 0 var(--acc);animation:pulse 2s infinite}
.dot.off{background:var(--bad);animation:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--acc) 60%,transparent)}70%{box-shadow:0 0 0 8px transparent}100%{box-shadow:0 0 0 0 transparent}}
.sub{color:var(--mut);font-size:13px;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:14px 0 8px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 12px;min-width:0}
.tile b{display:block;font-size:23px;font-variant-numeric:tabular-nums;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile span{color:var(--mut);font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}
.tile i{font-style:normal;color:var(--mut);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin:10px 0}
.card h2{font-size:12.5px;margin:0 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;display:flex;justify-content:space-between;gap:8px}
.card h2 em{font-style:normal;text-transform:none;letter-spacing:0;color:var(--fg);font-weight:600}
svg{width:100%;height:auto;display:block;overflow:visible}
.legend{display:flex;gap:14px;font-size:12px;color:var(--mut);margin-top:4px}.legend b{display:inline-block;width:10px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.tabs button,.btn{font:inherit;font-size:13px;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:99px;padding:5px 11px;cursor:pointer}
.tabs button[aria-pressed=true]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.btn.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button:focus-visible,input:focus-visible{outline:2px solid var(--acc2);outline-offset:2px}
.ev{border-top:1px solid var(--line);padding:9px 0;font-size:14px;white-space:pre-wrap;overflow-wrap:anywhere}
.ev:first-child{border-top:0}.ev small{color:var(--mut)}.ev a{color:var(--acc);font-weight:600;text-decoration:none}
.tag{font-size:11px;font-weight:700;padding:1px 7px;border-radius:99px;margin-right:6px;color:#fff;background:var(--acc)}
.tag.skip{background:var(--mut)}.tag.error{background:var(--bad)}
.set{display:grid;gap:10px}.set label{display:flex;justify-content:space-between;align-items:center;gap:10px}
.set input[type=time],.set input[type=number]{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);width:7.5em}
.sw{appearance:none;width:44px;height:26px;border-radius:99px;background:var(--line);position:relative;cursor:pointer;flex:none}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:left .2s}
.sw:checked{background:var(--acc)}.sw:checked::after{left:21px}
.muted{color:var(--mut);font-size:12px}
table.rk{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
table.rk th{text-align:left;color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:4px 6px;border-bottom:1px solid var(--line)}
table.rk td{padding:6px;border-bottom:1px solid var(--line);white-space:nowrap}table.rk td:first-child{white-space:normal;min-width:110px}
.pos{color:var(--acc);font-weight:700}.neg{color:var(--bad);font-weight:700}.medal{margin-right:4px}.toast{position:fixed;left:50%;bottom:max(18px,env(safe-area-inset-bottom));transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:99px;font-size:14px}
@media (prefers-reduced-motion:reduce){.dot{animation:none}}
</style></head><body><div class="wrap">
<div class="row"><h1>⚽ Guru Pi</h1><span id="st" class="pill">…</span></div>
<div class="row"><div class="sub" id="sub">Jungiamasi…</div><span class="live"><span class="dot" id="dot"></span>LIVE</span></div>

<div class="grid">
<div class="tile"><span>Signalai šiandien</span><b id="t_sig">–</b><i id="t_sig2"></i></div>
<div class="tile"><span>Pranešimo greitis</span><b id="t_lat">–</b><i>vidurkis</i></div>
<div class="tile"><span>Procesorius</span><b id="t_cpu">–</b><i id="t_load"></i></div>
<div class="tile"><span>Temperatūra</span><b id="t_temp">–</b><i id="t_temp2"></i></div>
<div class="tile"><span>Elektra dabar</span><b id="t_w">–</b><i>apskaičiuota</i></div>
<div class="tile"><span>Per mėnesį</span><b id="t_eur">–</b><i id="t_kwh"></i></div>
<div class="tile"><span>Internetas</span><b id="t_net">–</b><i id="t_ping"></i></div>
<div class="tile"><span>Įjungtas</span><b id="t_up">–</b><i id="t_mem"></i></div>
</div>
<div class="muted" id="warn"></div>

<div class="card"><h2>Dabar · paskutinės 5 min. <em id="livestat"></em></h2><div id="c_live"></div>
<div class="legend"><span><b style="background:var(--acc)"></b>Procesorius %</span><span><b style="background:var(--acc2)"></b>Elektra W</span></div></div>

<div class="card"><h2>Istorija <em id="rng"></em></h2>
<div class="tabs" role="group" aria-label="Laikotarpis"><button data-h="6" id="h6">6 val.</button><button data-h="24" id="h24">24 val.</button><button data-h="168" id="h168">7 d.</button></div>
<div class="tabs" role="group" aria-label="Rodiklis"><button data-m="temp">Temperatūra</button><button data-m="w">Elektra</button><button data-m="cpu">Procesorius</button><button data-m="ping">Internetas</button></div>
<div id="c_hist"></div></div>

<div class="card"><h2>Signalai per 7 dienas <em id="sigsum"></em></h2><div id="c_days"></div></div>

<div class="card"><h2>Grupių reitingas <em id="aud_meta"></em></h2><div id="aud" style="overflow-x:auto"></div><div class="muted" id="aud_note"></div></div>

<div class="card"><h2>Žinutės iš kanalo</h2>
<div class="tabs" role="group" aria-label="Filtras"><button data-f="signal" id="f_signal">Signalai</button><button data-f="skip">Praleista</button><button data-f="error">Klaidos</button><button data-f="all">Visos</button></div>
<div id="evs"></div></div>

<div class="card"><h2>Nustatymai</h2><div class="set" id="set">
<label for="s_paused">Sustabdyti pranešimus<input type="checkbox" class="sw" id="s_paused"></label>
<label for="s_quiet">Tylos režimas naktį (be garso)<input type="checkbox" class="sw" id="s_quiet"></label>
<label for="s_qf">Tyla nuo<input type="time" id="s_qf" value="23:00"></label>
<label for="s_qt">Tyla iki<input type="time" id="s_qt" value="08:00"></label>
<label for="s_price">Elektros kaina, €/kWh<input type="number" id="s_price" step="0.01" min="0" max="2" value="0.25"></label>
<div class="row"><button class="btn" id="b_test" type="button">Siųsti bandomąjį pranešimą</button><button class="btn primary" id="b_save" type="button">Išsaugoti</button></div>
<div class="muted" id="setnote"></div></div></div>
<div class="muted" id="foot"></div>
</div>
<script>
const $=id=>document.getElementById(id),css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let K='';try{const m=location.hash.match(/k=([\w-]+)/);if(m){K=m[1];localStorage.setItem('gk',K);history.replaceState(null,'',location.pathname)}else K=localStorage.getItem('gk')||''}catch(e){}
const pref=(k,d)=>{try{return localStorage.getItem(k)||d}catch(e){return d}},spref=(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}};
let H=+pref('gh','24'),M=pref('gm','temp'),F=pref('gf','signal'),D=null,LIVE=[];
const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
/* skaičiai „bėga“ iki naujos vertės */
const cur={};function tween(id,val,dec,unit){const el=$(id);if(val==null||isNaN(val)){el.textContent='–';return}
const from=cur[id]??val;cur[id]=val;if(reduce||from===val){el.textContent=val.toFixed(dec)+unit;return}
const t0=performance.now(),dur=700;const step=t=>{const p=Math.min(1,(t-t0)/dur),e=1-Math.pow(1-p,3);el.textContent=(from+(val-from)*e).toFixed(dec)+unit;if(p<1)requestAnimationFrame(step)};requestAnimationFrame(step)}
function toast(t){const d=document.createElement('div');d.className='toast';d.textContent=t;document.body.appendChild(d);setTimeout(()=>d.remove(),2200)}
function fmtUp(h){return h<1?Math.round(h*60)+' min.':h<48?h.toFixed(1)+' val.':(h/24).toFixed(1)+' d.'}
function ago(iso){if(!iso)return'–';const s=(Date.now()-new Date(iso))/1000;return s<90?'ką tik':s<3600?Math.round(s/60)+' min.':s<86400?Math.round(s/3600)+' val.':Math.round(s/86400)+' d.'}
/* linijinis grafikas: viena skalė, tinklelis, paskutinis taškas paryškintas */
function chart(el,series,opt={}){const W=340,Hh=opt.h||130,L=30,R=8,T=8,B=20;
const all=series.flatMap(s=>s.pts.map(p=>p[1])).filter(v=>v!=null);if(series[0].pts.length<2||!all.length){el.innerHTML='<div class="muted">Renkami duomenys…</div>';return}
const xs=series.flatMap(s=>s.pts.map(p=>p[0])),x0=opt.x0??Math.min(...xs),x1=opt.x1??Math.max(...xs);
let lo=opt.min??Math.min(...all),hi=opt.max??Math.max(...all);if(hi-lo<(opt.span||1)){const m=(hi+lo)/2;hi=m+(opt.span||1)/2;lo=Math.max(opt.floor??-1e9,m-(opt.span||1)/2)}
const X=t=>L+(t-x0)/((x1-x0)||1)*(W-L-R),Y=v=>T+(hi-v)/((hi-lo)||1)*(Hh-T-B),mut=css('--mut'),ln=css('--line');
let g='';for(let i=0;i<=2;i++){const v=lo+(hi-lo)*i/2,y=Y(v);g+=`<line x1="${L}" x2="${W-R}" y1="${y}" y2="${y}" stroke="${ln}"/><text x="${L-4}" y="${y+3.5}" font-size="10" text-anchor="end" fill="${mut}">${(+v.toFixed(opt.dec??1))}</text>`}
const tl=t=>new Date(t*1000).toLocaleTimeString('lt',{hour:'2-digit',minute:'2-digit'}),dl=t=>new Date(t*1000).toLocaleDateString('lt',{month:'2-digit',day:'2-digit'});
const lab=(x1-x0)>2*86400?dl:tl;g+=`<text x="${L}" y="${Hh-4}" font-size="10" fill="${mut}">${lab(x0)}</text><text x="${W-R}" y="${Hh-4}" font-size="10" fill="${mut}" text-anchor="end">${opt.nowLabel||lab(x1)}</text>`;
for(const s of series){const pts=s.pts.filter(p=>p[1]!=null);if(pts.length<2)continue;const col=css(s.color);
const d=pts.map((p,i)=>(i?'L':'M')+X(p[0]).toFixed(1)+' '+Y(p[1]).toFixed(1)).join(' ');
if(s.fill)g+=`<path d="${d} L${X(pts.at(-1)[0]).toFixed(1)} ${Hh-B} L${X(pts[0][0]).toFixed(1)} ${Hh-B} Z" fill="${col}" opacity=".14"/>`;
g+=`<path d="${d}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round"/><circle cx="${X(pts.at(-1)[0])}" cy="${Y(pts.at(-1)[1])}" r="3.5" fill="${col}"/>`}
el.innerHTML=`<svg viewBox="0 0 ${W} ${Hh}" role="img" aria-label="${esc(opt.label||'grafikas')}">${g}</svg>`}
function bars(el,evs){const days=[];for(let i=6;i>=0;i--){days.push(new Date(Date.now()-i*864e5).toLocaleDateString('sv'))}
const c=days.map(d=>evs.filter(e=>e.kind==='signal'&&e.t.slice(0,10)===d).length),m=Math.max(1,...c),W=340,Hh=120,bw=(W-10)/7,acc=css('--acc'),mut=css('--mut'),fg=css('--fg');
$('sigsum').textContent='iš viso '+c.reduce((a,b)=>a+b,0);
el.innerHTML=`<svg viewBox="0 0 ${W} ${Hh}" role="img" aria-label="Signalai per dieną">`+c.map((v,i)=>{const h=Math.max(v?4:0,(Hh-36)*v/m);
return `<rect x="${5+i*bw+7}" y="${Hh-18-h}" width="${bw-14}" height="${h}" rx="4" fill="${acc}"/><text x="${5+i*bw+bw/2}" y="${Hh-22-h}" font-size="11" text-anchor="middle" fill="${fg}">${v}</text>
<text x="${5+i*bw+bw/2}" y="${Hh-4}" font-size="10" text-anchor="middle" fill="${mut}">${days[i].slice(5).replace('-','.')}</text>`}).join('')+'</svg>'}
const MET={temp:{k:'temp',u:'°C',color:'--warn',dec:1,span:2},w:{k:'w',u:' W',color:'--acc2',dec:2,span:.5,floor:0},cpu:{k:'cpu',u:'%',color:'--acc',dec:0,span:5,floor:0},ping:{k:'ping',u:' ms',color:'--acc2',dec:0,span:20,floor:0}};
function drawHist(){if(!D)return;const m=MET[M],now=Date.now()/1000,pts=D.samples.filter(s=>s.ts>now-H*3600&&s[m.k]!=null).map(s=>[s.ts,s[m.k]]);
const v=pts.map(p=>p[1]);$('rng').textContent=v.length?`min ${Math.min(...v).toFixed(m.dec)} · vid ${(v.reduce((a,b)=>a+b,0)/v.length).toFixed(m.dec)} · max ${Math.max(...v).toFixed(m.dec)}${m.u}`:'';
chart($('c_hist'),[{pts,color:m.color,fill:true}],{x0:now-H*3600,x1:now,span:m.span,floor:m.floor,dec:m.dec,nowLabel:'dabar',label:'istorija'});
document.querySelectorAll('[data-h]').forEach(b=>b.setAttribute('aria-pressed',+b.dataset.h===H));document.querySelectorAll('[data-m]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.m===M))}
function drawAudit(){const a=D&&D.audit;if(!a){$('aud').innerHTML='<div class="muted">Auditas pasileis per kelias minutes po Pi paleidimo</div>';return}
$('aud_meta').textContent=a.chats+' grupės · '+a.days+' d.';
const rows=a.groups.filter(g=>g.n>0);const med=['🥇','🥈','🥉'];let rank=0;
$('aud').innerHTML=rows.length?`<table class="rk"><tr><th>Grupė</th><th>Stat.</th><th>Pataikė</th><th>ROI</th><th>Koef.</th></tr>`+rows.slice(0,15).map(g=>{const ok=g.n_odds>=15;const m=ok&&rank<3&&g.roi>0?med[rank++]:'';
return `<tr><td>${m?`<span class="medal">${m}</span>`:''}${esc(g.chat)}${g.reliable?'':' <span class="muted">(mažai)</span>'}</td><td>${g.n}${g.pend?` <span class="muted">+${g.pend}</span>`:''}</td><td>${g.hit??'–'}%</td>
<td class="${g.roi>0?'pos':g.roi<0?'neg':''}">${g.roi==null?'–':(g.roi>0?'+':'')+g.roi+'%'}</td><td>${g.avg_odds??'–'}</td></tr>`}).join('')+'</table>':'<div class="muted">Renkami ir tikrinami signalai…</div>';
$('aud_note').textContent=`Tikrinama pagal tikrus rezultatus (LiveScore), ne pagal grupių „win“. Išanalizuota ${a.done_msgs} žinučių, eilėje ${a.queue}, statymų ${a.bets}. AI šiandien ${a.ai_used}/${a.ai_cap}${a.paused?' (pauzė – limitas)':''}. Patikima tik nuo 30 statymų.`}
function drawEvents(){if(!D)return;const ev=D.events.slice().reverse().filter(e=>F==='all'||e.kind===F).slice(0,30);
const nm={signal:'SIGNALAS',skip:'PRALEISTA',error:'KLAIDA'};
$('evs').innerHTML=ev.map(e=>`<div class="ev"><small><span class="tag ${e.kind}">${nm[e.kind]||e.kind}</span>${e.t.slice(5,16).replace('T',' ')}${e.kind==='signal'?' · per '+e.secs+' s':''}</small>
<div>${esc(e.text)}</div>${e.link?`<a href="${esc(e.link)}" target="_blank" rel="noopener">Atidaryti fs7 →</a>`:''}</div>`).join('')||'<div class="muted">Kol kas nieko</div>';
document.querySelectorAll('[data-f]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.f===F))}
function drawLive(){const now=Date.now()/1000;chart($('c_live'),[{pts:LIVE.map(p=>[p.ts,p.cpu]),color:'--acc',fill:true},{pts:LIVE.map(p=>[p.ts,p.w*10]),color:'--acc2'}],
{x0:now-300,x1:now,min:0,max:Math.max(30,...LIVE.map(p=>Math.max(p.cpu,p.w*10)))*1.1,dec:0,nowLabel:'dabar',label:'gyvas grafikas',h:120})}
async function live(){try{const r=await fetch('/live',{cache:'no-store'});const p=await r.json();LIVE.push(p);const cut=Date.now()/1000-300;LIVE=LIVE.filter(x=>x.ts>cut);
$('dot').className='dot';tween('t_cpu',p.cpu,0,'%');tween('t_temp',p.temp,1,' °C');tween('t_w',p.w,2,' W');
$('t_load').textContent='apkrova '+p.load;$('t_mem').textContent='RAM '+p.mem+'%';
const kb=(p.rx+p.tx)/1024;$('t_net').textContent=kb<1000?kb.toFixed(1)+' KB/s':(kb/1024).toFixed(2)+' MB/s';
$('livestat').textContent=`${p.cpu.toFixed(0)}% · ${p.w.toFixed(2)} W`;drawLive()}catch(e){$('dot').className='dot off'}}
async function full(){try{const r=await fetch('/api',{cache:'no-store'});D=await r.json();const c=D.current,today=new Date().toLocaleDateString('sv'),sig=D.events.filter(e=>e.kind==='signal');
const tg=c.tg!==false,s=D.settings||{};let st=tg?'Veikia':'Telegram atsijungęs',cls=tg?'':' bad';if(tg&&s.paused){st='Pranešimai sustabdyti';cls=' warn'}else if(tg&&D.quiet_now){st='Tylos režimas';cls=' warn'}
$('st').textContent=st;$('st').className='pill'+cls;$('sub').textContent=(D.chats.join(', ')||'–')+' · paskutinė žinutė prieš '+ago(D.last_msg);
const td=sig.filter(e=>e.t.slice(0,10)===today);$('t_sig').textContent=td.length;$('t_sig2').textContent='paskutinis prieš '+ago(sig.at(-1)?.t);
const l=sig.slice(-20).map(e=>e.secs);$('t_lat').textContent=l.length?(l.reduce((a,b)=>a+b,0)/l.length).toFixed(1)+' s':'–';
const en=D.energy||{};$('t_eur').textContent=en.eur_month!=null?en.eur_month.toFixed(2)+' €':'–';$('t_kwh').textContent=(en.kwh_month??0)+' kWh · šiandien '+(en.kwh_today??0).toFixed(3);
$('t_up').textContent=fmtUp(D.uptime_h);$('t_ping').textContent=c.ping!=null?'atsakas '+c.ping+' ms':'';
const lastT=D.samples.filter(x=>x.ts>Date.now()/1000-86400).map(x=>x.temp);$('t_temp2').textContent=lastT.length?'24 val. max '+Math.max(...lastT).toFixed(1)+' °C':'';
const pw=D.power||{},w=[];if(pw.now_low_voltage)w.push('⚠️ Per silpnas maitinimas – naudok oficialų 5V 3A bloką');else if(pw.ever_low_voltage)w.push('⚠️ Buvo per silpnas maitinimas');
if(pw.now_throttled)w.push('⚠️ Procesorius sulėtintas (karštis/maitinimas)');if(c.temp>75)w.push('🌡 Labai karšta');if(D.ai_fail)w.push('AI klaidų: '+D.ai_fail);$('warn').textContent=w.join(' · ');
if(!document.activeElement||!$('set').contains(document.activeElement)){$('s_paused').checked=!!s.paused;$('s_quiet').checked=!!s.quiet;$('s_qf').value=s.quiet_from||'23:00';$('s_qt').value=s.quiet_to||'08:00';$('s_price').value=s.kwh_price??0.25}
$('setnote').textContent=K?'':'Nustatymus keisti galima tik atidarius puslapį iš Raspberry pranešimo nuorodos.';['b_save','b_test'].forEach(id=>$(id).disabled=!K);
bars($('c_days'),D.events);drawAudit();drawHist();drawEvents();$('foot').textContent='Atnaujinta '+new Date().toLocaleTimeString('lt')+' · diskas '+c.disk+'% · Pi paleistas '+(D.started||'').replace('T',' ').slice(5,16);
}catch(e){$('st').textContent='Nepasiekiamas';$('st').className='pill bad';$('sub').textContent='Raspberry neatsako – gal išjungtas ar nėra interneto';$('dot').className='dot off'}}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,k:K})});if(!r.ok)throw new Error(await r.text());return r.json()}
$('b_save').onclick=async()=>{try{await post('/settings',{paused:$('s_paused').checked,quiet:$('s_quiet').checked,quiet_from:$('s_qf').value,quiet_to:$('s_qt').value,kwh_price:+$('s_price').value});toast('Išsaugota');full()}catch(e){toast('Nepavyko: '+e.message)}};
$('b_test').onclick=async()=>{try{await post('/test',{});toast('Išsiųsta – patikrink telefoną')}catch(e){toast('Nepavyko: '+e.message)}};
document.querySelectorAll('[data-h]').forEach(b=>b.onclick=()=>{H=+b.dataset.h;spref('gh',H);drawHist()});
document.querySelectorAll('[data-m]').forEach(b=>b.onclick=()=>{M=b.dataset.m;spref('gm',M);drawHist()});
document.querySelectorAll('[data-f]').forEach(b=>b.onclick=()=>{F=b.dataset.f;spref('gf',F);drawEvents()});
full();live();setInterval(live,2000);setInterval(full,20000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){full();live()}});
</script></body></html>"""

MANIFEST = {"name": "Lažybų analitika", "short_name": "Analitika", "start_url": "/", "display": "standalone",
            "background_color": "#111126", "theme_color": "#111126",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" shape-rendering="crispEdges"><rect width="16" height="16" fill="#111126"/>'
        '<rect x="2" y="9" width="3" height="5" fill="#39F58A"/><rect x="6" y="6" width="3" height="8" fill="#39F58A"/>'
        '<rect x="10" y="3" width="3" height="11" fill="#965CFF"/><rect x="1" y="14" width="14" height="1" fill="#38CFFF"/></svg>')


class Dash(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        k = (q.get("k") or [""])[0]
        owner = secrets.compare_digest(k, SETTINGS["token"])
        if path in ("/live", "/api") and not owner:
            return self._send(403, "neleidžiama – atidaryk puslapį iš Raspberry pranešimo", "text/plain; charset=utf-8")
        if path == "/live":
            self._send(200, json.dumps(live_json()), "application/json")
        elif path == "/api":
            self._send(200, json.dumps(api_json(), ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/gapi":
            g = (q.get("g") or [""])[0]
            if not SETTINGS.get("guest_on") or not secrets.compare_digest(g, SETTINGS["guest_key"]):
                return self._send(403, "neleidžiama", "text/plain; charset=utf-8")
            self._send(200, json.dumps(guest_json(), ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/guest":
            f = BASE / "guest.html"
            self._send(200, f.read_bytes() if f.exists() else "Svetainė ruošiama".encode(), "text/html; charset=utf-8")
        elif path == "/manifest.json":
            self._send(200, json.dumps(MANIFEST), "application/manifest+json")
        elif path == "/icon.svg":
            self._send(200, ICON, "image/svg+xml")
        elif path == "/":
            f = BASE / "dash.html"
            try:
                html = f.read_bytes() if f.exists() else DASH_HTML
            except Exception:
                html = DASH_HTML
            self._send(200, html, "text/html; charset=utf-8")
        else:
            self._send(404, "nerasta", "text/plain; charset=utf-8")


    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 10000)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, "blogas užklausos formatas", "text/plain; charset=utf-8")
        if not secrets.compare_digest(str(body.get("k", "")), SETTINGS["token"]):
            return self._send(403, "neleidžiama – atidaryk puslapį iš Raspberry pranešimo", "text/plain; charset=utf-8")
        if path == "/settings":
            for k in ("paused", "quiet", "notify_guru", "auto_proven"):
                if k in body:
                    SETTINGS[k] = bool(body[k])
            try:
                if "consensus_min" in body:
                    SETTINGS["consensus_min"] = min(max(int(body["consensus_min"]), 0), 6)
            except Exception:
                pass
            if "guest_on" in body:
                SETTINGS["guest_on"] = bool(body["guest_on"])
            if body.get("guest_regen"):                    # naujas svečių kodas – seni svečiai nebeprisijungs
                SETTINGS["guest_key"] = secrets.token_urlsafe(9)
                SETTINGS["guest_topic"] = "gs-" + secrets.token_hex(8)
            if isinstance(body.get("follow"), list):
                SETTINGS["follow"] = [str(x)[:160] for x in body["follow"] if re.fullmatch(r"[gm]:[^\n]{1,150}", str(x))][:300]
            for k in ("quiet_from", "quiet_to"):
                if re.fullmatch(r"\d{2}:\d{2}", str(body.get(k, ""))):
                    SETTINGS[k] = body[k]
            try:
                if "kwh_price" in body:
                    SETTINGS["kwh_price"] = min(max(float(body["kwh_price"]), 0), 2)
            except Exception:
                pass
            save_settings()
            log.info("⚙️ Nustatymai pakeisti: %s", {k: v for k, v in SETTINGS.items() if k != "token"})
            return self._send(200, json.dumps({"ok": True}), "application/json")
        if path == "/test":
            try:
                ntfy("✅ Guru Pi bandymas", "Pranešimai veikia. Taip atrodys signalas.", click=dash_link(), priority=5)
                return self._send(200, json.dumps({"ok": True}), "application/json")
            except Exception as e:
                return self._send(502, f"ntfy: {e}", "text/plain; charset=utf-8")
        self._send(404, "nerasta", "text/plain; charset=utf-8")


def guest_link():
    return f"{STATE['url']}/guest#g={SETTINGS['guest_key']}" if STATE.get("url") else None


def _gclean(d):
    """Rezultato aprašymas svečiams – be šaltinių ir grupių pavadinimų."""
    d = str(d or "")
    if re.search(r"(?i)grup|kanal|pažymėj", d):
        return ""
    return re.sub(r"\s*\([^)]*\)", "", d)[:90]


def guest_json():
    """Svečių vaizdas: tik ★ sekamų profilių statistika ir signalai, šaltiniai – „Šaltinis 1, 2…“."""
    a = AUDIT.get("a")
    s = a.summary() if a else {}
    follow = sorted(SETTINGS.get("follow") or [])
    alias = {k: f"Šaltinis {i + 1}" for i, k in enumerate(follow)}
    keep = ("n", "w", "l", "p", "pend", "units", "hit", "roi")
    mine = s.get("mine") or {}
    pick = lambda x: {k: v for k, v in (x or {}).items() if k in keep}
    bets = []
    for b in s.get("recent") or []:
        if not b.get("fo"):
            continue
        bets.append({"t": b["t"], "status": b["status"], "odds": b.get("odds"), "src": alias.get(b.get("fk"), "Šaltinis"),
                     "legs": [{"m": l.get("m"), "pick": l.get("pick"), "st": l.get("st"), "d": _gclean(l.get("d"))}
                              for l in b.get("legs") or []]})
        if len(bets) >= 200:
            break
    return {"now": now_iso(), "total": pick(mine.get("total")), "today": pick(mine.get("today")),
            "sources": [dict(name=alias.get(x["k"], "Šaltinis"), **pick(x)) for x in mine.get("items") or []],
            "bets": bets}


def guest_post(title, message, click=None, priority=4, topic=None):
    topic = topic or SETTINGS.get("guest_topic")
    if not topic:
        return
    body = {"topic": topic, "title": title[:200], "message": message[:3500], "priority": int(priority),
            "tags": ["moneybag"]}
    if click:
        body["click"] = click
    req = urllib.request.Request(CFG.get("ntfy_server", "https://ntfy.sh").rstrip("/"),
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20):
        pass


def dash_link():
    return f"{STATE['url']}/#k={SETTINGS['token']}" if STATE.get("url") else None


def start_dash():
    port = int(CFG.get("dash_port", 8080))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Dash)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("📊 Būsenos puslapis: http://guru-pi.local:%d", port)
    return port


CF = BASE / "cloudflared"
CF_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"


def tunnel(port):
    """Nemokamas Cloudflare tunelis – puslapis pasiekiamas iš bet kur (ir per mobilų internetą).
    Adresas pasikeičia po kiekvieno paleidimo – naujas atsiunčiamas į telefoną (ntfy). Nutrūkus – paleidžiamas iš naujo."""
    if not CFG.get("public_dash", True):
        return
    try:
        if not CF.exists():
            log.info("Parsisiunčiu cloudflared…")
            urllib.request.urlretrieve(CF_URL, str(CF) + ".part")
            os.chmod(str(CF) + ".part", 0o755)
            os.replace(str(CF) + ".part", CF)
    except Exception as e:
        log.error("cloudflared parsisiųsti nepavyko: %s", e)
        return
    while True:
        try:
            p = subprocess.Popen([str(CF), "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for ln in p.stdout:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", ln)
                if m and STATE["url"] != m.group(0):
                    STATE["url"] = m.group(0)
                    log.info("🌍 Būsenos puslapis telefonui: %s", STATE["url"])
                    try:
                        (BOOT / "puslapis.txt").write_text(STATE["url"] + "\n")
                    except Exception:
                        pass
                    try:
                        ntfy("📊 Raspberry būsenos puslapis", "Paspausk – atsidarys Guru Pi puslapis. "
                             "Naršyklėje: „Pridėti prie pagrindinio ekrano“, kad būtų kaip programėlė.",
                             click=dash_link(), priority=2)
                    except Exception as e:
                        log.info("ntfy: %s", e)
            p.wait()
            log.error("Tunelis nutrūko – paleidžiu iš naujo po 30 s")
        except Exception as e:
            log.error("Tunelis nepavyko: %s", e)
        STATE["url"] = None
        time.sleep(30)


def app_beacon():
    """Telefono programėlei: dabartinis puslapio adresas tyliai skelbiamas į atskirą ntfy temą „<tema>-app“
    (jos niekas neprenumeruoja – pranešimų nebus). Programėlė iš ten sužino, kur jungtis, net kai adresas pasikeičia."""
    last, sent = None, 0
    glast, gsent = None, 0
    dsent = time.time() - 840
    while True:
        try:
            gl = guest_link() if SETTINGS.get("guest_on") else None
            if gl and (gl != glast or time.time() - gsent > 4 * 3600):
                guest_post("guest-app", gl, priority=1, topic=SETTINGS["guest_topic"] + "-app")
                glast, gsent = gl, time.time()
        except Exception as e:
            log.info("svečių programėlės adresas: %s", e)
        try:
            link = dash_link()
            if link and (link != last or time.time() - sent > 4 * 3600):
                req = urllib.request.Request(NTFY + "-app", data=link.encode(), method="POST",
                                             headers={"Title": "guru-app", "Priority": "1", "Cache": "yes"})
                with urllib.request.urlopen(req, timeout=20):
                    pass
                last, sent = link, time.time()
        except Exception as e:
            log.info("programėlės adresas: %s", e)
        try:                                              # diagnostika – tyli tema, niekas neprenumeruoja
            a = AUDIT.get("a")
            if a and time.time() - dsent > 900:
                dsent = time.time()
                body = json.dumps(a.diag(), ensure_ascii=False, separators=(",", ":")).encode()[:3900]
                req = urllib.request.Request(NTFY + "-diag", data=body, method="POST",
                                             headers={"Title": "guru-diag", "Priority": "1", "Cache": "yes"})
                with urllib.request.urlopen(req, timeout=20):
                    pass
        except Exception as e:
            log.info("diagnostika: %s", e)
        time.sleep(60)


# ---------------------------------------------------------------- atnaujinimai iš GitHub (be kompiuterio)
import hashlib

UPDATE_URL = CFG.get("update_url") or "https://raw.githubusercontent.com/kziuri14-sudo/guru-pi/main/pi_guru.py"


UPDATE_FILES = ("pi_guru.py", "pi_audit.py", "results.py", "props.py", "dash.html", "selftest.py", "guest.html")
GH_REJECTED = set()                                # versijos, kurių testai nepraėjo – nebandom iš naujo


def _fetch_update(name="pi_guru.py"):
    try:
        url = UPDATE_URL.rsplit("/", 1)[0] + "/" + name
        req = urllib.request.Request(f"{url}?t={int(time.time())}", headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(2_000_000)
    except Exception:
        return None


def _selftest(new):
    """Nauja versija išbandoma atskirame aplanke (selftest.py) prieš ją įdiegiant. (ar_praėjo, išvestis)"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="guru_test_"))
    try:
        for name in UPDATE_FILES:
            data = new.get(name)
            if data is None:
                try:
                    data = (BASE / name).read_bytes()
                except Exception:
                    continue
            (tmp / name).write_bytes(data)
        if not (tmp / "selftest.py").exists():
            return True, "testų nėra"
        r = subprocess.run([sys.executable, "selftest.py"], cwd=tmp, capture_output=True, text=True, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out
    except Exception as e:
        return False, f"testų paleisti nepavyko: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


GH_NOW = asyncio.Event()


async def github_updater():
    """Kas 5 min. (arba iškart po „#pi_github“ Saved Messages) patikrina GitHub. Priimama tik sukompiliuojama versija."""
    await asyncio.sleep(60)
    while True:
        GH_NOW.clear()
        changed, new = [], {}
        for name in UPDATE_FILES:
            data = await asyncio.to_thread(_fetch_update, name)
            if not data:
                continue
            try:
                cur = (BASE / name).read_bytes()
            except Exception:
                cur = b""
            if hashlib.sha256(data).digest() == hashlib.sha256(cur).digest():
                continue
            try:
                if name.endswith(".py"):
                    compile(data, name, "exec")
                else:
                    assert b"<html" in data[:500].lower()
                if name == "pi_guru.py":
                    assert b"async def handle(" in data and b"def setup_remote(" in data
            except Exception as e:
                log.error("❗ GitHub %s atmestas: %s", name, e)
                continue
            new[name] = data
        if new:
            sig = hashlib.sha256(b"".join(new[n] for n in sorted(new))).hexdigest()
            if sig in GH_REJECTED:
                new = {}                                   # jau atmesta – laukiam naujos versijos
            ok, out = await asyncio.to_thread(_selftest, new) if new else (True, "")
            if not ok:
                GH_REJECTED.add(sig)
                log.error("❗ GitHub atnaujinimas atmestas – testai nepraėjo:\n%s", out[-1500:])
                try:
                    await asyncio.to_thread(ntfy, "⚠ Atnaujinimas sustabdytas",
                                            "Naujos versijos testai nepraėjo – palikta veikianti versija.\n" + out[-300:],
                                            None, None, 3)
                except Exception:
                    pass
                new = {}
            for name, data in new.items():
                _write(name, data)
                changed.append(name)
        if changed:
            log.info("🔄 Atnaujinta iš GitHub: %s – persikraunu", ", ".join(changed))
            try:
                await asyncio.to_thread(ntfy, "🔄 Raspberry atnaujintas", "Iš GitHub: " + ", ".join(changed), None, None, 2)
            except Exception:
                pass
            await asyncio.sleep(3)
            os._exit(0)
        try:
            await asyncio.wait_for(GH_NOW.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass


AUDIT = {"a": None}


def _audit_summary():
    try:
        return AUDIT["a"].summary() if AUDIT.get("a") else None
    except Exception:
        return None


async def start_audit(client):
    try:
        import pi_audit
        AUDIT["a"] = pi_audit.Auditor(client, CFG, log, BASE, BOOT)

        def _note(title, msg, legs):
            if SETTINGS.get("paused"):
                return
            link = search_link(msg, legs)
            try:
                ntfy(title, msg, click=link, priority=2 if quiet_now() else 4)
                add_event("signal", f"{title}\n{msg}", 0, link)
            except Exception as e:
                log.info("audito pranešimas: %s", e)
        AUDIT["a"].notify = _note
        AUDIT["a"].settings = SETTINGS              # pažymėti žmonės / grupės pranešimams

        def _gnote(title, msg, legs):                 # svečiams – tas pats signalas, be šaltinio
            if SETTINGS.get("paused") or not SETTINGS.get("guest_on"):
                return
            guest_post(title, msg, click=search_link(msg, legs), priority=4)
        AUDIT["a"].guest_notify = _gnote
        log.info("📈 Grupių auditas paleistas (fone)")
        await AUDIT["a"].run()
    except Exception as e:
        log.error("Auditas nepasileido: %s", e)


async def find_chats(client):
    want = [norm(g) for g in CFG["groups"]]
    found = {}
    from telethon import utils
    for u in CFG.get("usernames") or []:              # t.me/GuruGoal – patikimiausia
        try:
            ent = await client.get_entity(u)
            found[utils.get_peer_id(ent)] = getattr(ent, "title", u)
        except Exception as e:
            log.info("Nepavyko rasti @%s: %s", u, e)
    async for d in client.iter_dialogs():
        n = norm(d.name)
        if any(w and (w == n or w in n) for w in want):
            found[d.id] = d.name
    return found


async def main():
    client = TelegramClient(str(BASE / "tg_pi"), int(CFG["api_id"]), CFG["api_hash"],
                            connection_retries=-1, retry_delay=10, auto_reconnect=True)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("❗ Telegram neprisijungęs – reikia tg_pi.session (PI_LOGIN.bat kompiuteryje)")
        try:
            ntfy("❗ Raspberry: Telegram neprisijungęs", "Paleisk PI_LOGIN.bat kompiuteryje ir įkelk tg_pi.session.", priority=4)
        except Exception:
            pass
        await asyncio.sleep(3600)
        return
    setup_remote(client)
    STATE["tg"] = True
    port = start_dash()
    threading.Thread(target=tunnel, args=(port,), daemon=True).start()
    threading.Thread(target=app_beacon, daemon=True).start()
    asyncio.create_task(sampler(client))
    asyncio.create_task(github_updater())
    if CFG.get("audit", True):
        asyncio.create_task(start_audit(client))
    chats = {}
    warned = False
    while not chats:
        chats = await find_chats(client)
        if not chats:
            log.error("❗ Nerasta grupė %s – tikrinsiu kas 10 min.", CFG["groups"])
            if not warned:
                try:
                    ntfy("❗ Raspberry: grupė nerasta", f"Nerasta {', '.join(CFG['groups'])}. "
                         "Prisijunk prie grupės Telegram – Pi ją pagaus automatiškai.", priority=4)
                except Exception:
                    pass
                warned = True
            await asyncio.sleep(600)
    log.info("👀 Stebiu: %s", ", ".join(chats.values()))
    STATE["chats"] = list(chats.values())

    @client.on(events.NewMessage(chats=list(chats)))
    async def on_new(ev):
        try:
            await handle(ev.message, chats.get(ev.chat_id) or "GuruGoal")
        except Exception as e:
            log.exception("klaida: %s", e)

    try:
        ntfy("✅ Raspberry veikia", "Stebiu: " + ", ".join(chats.values()), click=dash_link(), priority=3)
    except Exception as e:
        log.error("ntfy: %s", e)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
