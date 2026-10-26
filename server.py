"""RuMio — ruscha suhbat sayti (alohida server): odam bilan va sun'iy intellekt ustoz bilan.
Bot bilan faqat imzolangan havola orqali bog'lanadi (TALK_SECRET). Ovoz serverdan o'tmaydi: odamdan odamga (WebRTC).
"""
import asyncio, base64, hashlib, hmac, json, os, re, sqlite3, time
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web, WSMsgType
from dotenv import load_dotenv

load_dotenv()
SECRET = os.getenv("TALK_SECRET", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()          # ixtiyoriy: adminga xabar va botga qaytarish uchun
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()]
ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()           # bloklash havolalari uchun maxfiy kalit
SITE_URL = os.getenv("SITE_URL", "").rstrip("/")         # admin havolalari uchun, masalan https://suhbat.example.com
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "suhbat.db")
FREE_TOTAL = int(os.getenv("HUMAN_FREE_TOTAL", "300"))   # tekin: umrida bir marta shuncha soniya
UNLIM = 10 ** 7                                          # VIP: odam bilan cheksiz
MATCH_WAIT = int(os.getenv("MATCH_WAIT", "30"))
CROSS_AFTER = 12                                         # shu soniyadan keyin boshqa daraja bilan ham juftlanadi
REPORTS_TO_BAN = 3
TZ = timezone(timedelta(hours=5))
if not SECRET:
    raise SystemExit(".env da TALK_SECRET yo'q (botdagi bilan bir xil bo'lsin)")

if DB_PATH != ":memory:":
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")
db.execute("PRAGMA busy_timeout=5000")
db.execute("CREATE TABLE IF NOT EXISTS use(user_id INTEGER, day TEXT, secs INTEGER DEFAULT 0, PRIMARY KEY(user_id, day))")
db.execute("CREATE TABLE IF NOT EXISTS ai_use(user_id INTEGER, day TEXT, secs INTEGER DEFAULT 0, PRIMARY KEY(user_id, day))")
db.execute("CREATE TABLE IF NOT EXISTS ban(user_id INTEGER PRIMARY KEY)")
db.execute("CREATE TABLE IF NOT EXISTS block(a INTEGER, b INTEGER, PRIMARY KEY(a, b))")
db.execute("CREATE TABLE IF NOT EXISTS report(id INTEGER PRIMARY KEY AUTOINCREMENT, by_id INTEGER, target_id INTEGER, ts INTEGER)")
db.commit()


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def day_key(vip):
    return today() if vip else "total"


def left_for(uid, vip):
    if vip:
        return UNLIM
    cap = FREE_TOTAL
    r = db.execute("SELECT secs FROM use WHERE user_id=? AND day=?", (uid, day_key(vip))).fetchone()
    return max(0, cap - (r[0] if r else 0))


def add_use(uid, vip, secs):
    db.execute("INSERT INTO use(user_id, day, secs) VALUES(?,?,?) ON CONFLICT(user_id, day) DO UPDATE SET secs=secs+?",
               (uid, day_key(vip), int(secs), int(secs)))
    db.execute("DELETE FROM use WHERE day != 'total' AND day < ?", ((datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d"),))
    db.commit()


def banned(uid):
    return db.execute("SELECT 1 FROM ban WHERE user_id=?", (uid,)).fetchone() is not None


def blocked(a, b):
    return db.execute("SELECT 1 FROM block WHERE (a=? AND b=?) OR (a=? AND b=?)", (a, b, b, a)).fetchone() is not None


def verify(token):
    """Bot bergan imzolangan havola -> {"u","l","v"} yoki None."""
    try:
        body, sig = (token or "").split(".", 1)
        good = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if d["e"] < time.time():
            return None
        return d
    except Exception:
        return None


class Peer:
    def __init__(self, uid, ws, level, vip):
        self.uid, self.ws, self.level, self.vip = uid, ws, level, vip
        self.room = None
        self.since = 0.0
        self.left = 0
        self.connected = False
        self.qtask = None


class Room:
    def __init__(self, a, b):
        self.a, self.b = a, b
        self.t0 = 0.0
        self.closed = False
        self.tasks = []

    def other(self, p):
        return self.b if p is self.a else self.a


queue = []
active = {}
recent = {}  # uid -> oxirgi suhbatdoshlar (Keyingisi bosilganda o'sha odam qayta chiqmasin)


async def send(p, obj):
    try:
        await p.ws.send_json(obj)
    except Exception:
        pass


def ice_servers():
    ice = [{"urls": ["stun:stun.l.google.com:19302"]}]
    turn = [u.strip() for u in os.getenv("TURN_URLS", "").split(",") if u.strip()]
    if turn:
        ice.append({"urls": turn, "username": os.getenv("TURN_USER", ""), "credential": os.getenv("TURN_PASS", "")})
    return ice


async def tg(method, **params):
    if not BOT_TOKEN:
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            await s.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params)
    except Exception as e:
        print("telegram:", e)


def notify_admins(text):
    async def go():
        for aid in ADMIN_IDS:
            await tg("sendMessage", chat_id=aid, text=text)
    asyncio.create_task(go())


def admin_links(uid):
    if not (SITE_URL and ADMIN_KEY):
        return f"ID: {uid}"
    return f"\n🚫 Bloklash: {SITE_URL}/admin/ban?id={uid}&key={ADMIN_KEY}\n✅ Qaytarish: {SITE_URL}/admin/unban?id={uid}&key={ADMIN_KEY}"


async def leave_queue(p):
    if p in queue:
        queue.remove(p)
    if p.qtask:
        p.qtask.cancel()
        p.qtask = None


async def end_room(room, reason, count=True):
    if room.closed:
        return
    room.closed = True
    cur = asyncio.current_task()
    for t in room.tasks:
        if t is not cur:
            t.cancel()
    used = int(time.time() - room.t0) if (room.t0 and count) else 0
    for p in (room.a, room.b):
        p.room = None
        p.connected = False
        if used > 0 and not p.vip:
            try:
                add_use(p.uid, p.vip, used)
            except sqlite3.Error as e:
                print("add_use:", e)
        await send(p, {"type": "ended", "reason": reason, "used": used, "left": left_for(p.uid, p.vip)})


async def room_timer(room, secs):
    await asyncio.sleep(secs)
    await end_room(room, "time")


async def setup_timeout(room):
    await asyncio.sleep(25)
    if not room.t0:
        await end_room(room, "failed", count=False)


async def nomatch(p):
    await asyncio.sleep(MATCH_WAIT)
    if p in queue:
        queue.remove(p)
        await send(p, {"type": "nomatch"})
        if not groq_keys():
            await tg("sendMessage", chat_id=p.uid, text="😕 Hozir bo'sh suhbatdosh topilmadi. «🗣 Suhbat» → «🤖 Bot bilan mashq qilish» ni bosing, ustoz bilan gaplashamiz.")


def pair_ok(a, b, t, strict):
    if a.uid == b.uid or blocked(a.uid, b.uid) or b.uid in recent.get(a.uid, ()) or a.uid in recent.get(b.uid, ()):
        return False
    if a.level == b.level:
        return True
    return (not strict) and t - min(a.since, b.since) >= CROSS_AFTER


async def match():
    t = time.time()
    for strict in (True, False):
        i = 0
        while i < len(queue):
            a = queue[i]
            b = next((x for x in queue[i + 1:] if pair_ok(a, x, t, strict)), None)
            if not b:
                i += 1
                continue
            for x in (a, b):
                queue.remove(x)
                if x.qtask:
                    x.qtask.cancel()
                    x.qtask = None
            room = Room(a, b)
            recent[a.uid] = (recent.get(a.uid, ()) + (b.uid,))[-4:]
            recent[b.uid] = (recent.get(b.uid, ()) + (a.uid,))[-4:]
            a.room = b.room = room
            room.tasks.append(asyncio.create_task(setup_timeout(room)))
            await send(a, {"type": "matched", "initiator": True, "level": b.level})
            await send(b, {"type": "matched", "initiator": False, "level": a.level})


async def matcher():
    while True:
        await asyncio.sleep(2)
        try:
            if len(queue) > 1:
                await match()
            if len(recent) > 5000:
                recent.clear()
        except Exception as e:
            print("matcher:", e)


async def do_report(p, block_only=False):
    room = p.room
    if not room:
        return
    q = room.other(p)
    db.execute("INSERT OR IGNORE INTO block(a, b) VALUES(?,?)", (p.uid, q.uid))
    if not block_only:
        db.execute("INSERT INTO report(by_id, target_id, ts) VALUES(?,?,?)", (p.uid, q.uid, int(time.time())))
        n = db.execute("SELECT COUNT(DISTINCT by_id) FROM report WHERE target_id=? AND ts>?", (q.uid, int(time.time()) - 30 * 86400)).fetchone()[0]
        if n >= REPORTS_TO_BAN:
            db.execute("INSERT OR IGNORE INTO ban(user_id) VALUES(?)", (q.uid,))
            notify_admins(f"🚫 {q.uid} {n} ta shikoyat sabab avtomatik bloklandi." + admin_links(q.uid))
        else:
            notify_admins(f"⚠️ Shikoyat: {p.uid} → {q.uid} ({n} ta)." + admin_links(q.uid))
    db.commit()
    await end_room(room, "blocked" if block_only else "reported")


async def cleanup(p):
    await leave_queue(p)
    if p.room:
        await end_room(p.room, "partner")
    if active.get(p.uid) is p:
        active.pop(p.uid, None)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
    await ws.prepare(request)
    p = None
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            t = d.get("type")
            if p is None:
                if t != "auth":
                    break
                info = verify(d.get("token"))
                if not info:
                    await ws.send_json({"type": "denied", "text": "Havola eskirgan. Botdagi «🗣 Suhbat» tugmasini qayta bosing."})
                    break
                uid = int(info["u"])
                if banned(uid):
                    await ws.send_json({"type": "denied", "text": "Siz suhbatdan bloklangansiz."})
                    break
                old = active.get(uid)
                if old:
                    await send(old, {"type": "denied", "text": "Boshqa oynada ochildi."})
                    try:
                        await old.ws.close()
                    except Exception:
                        pass
                    await cleanup(old)
                p = Peer(uid, ws, str(info.get("l") or "A1"), bool(info.get("v")))
                active[uid] = p
                await ws.send_json({"type": "hello", "ice": ice_servers(), "left": left_for(uid, p.vip), "vip": p.vip, "level": p.level, "ai": bool(groq_keys()), "ai_left": ai_left(uid)})
            elif t == "find":
                if p.room or p in queue:
                    continue
                p.left = left_for(p.uid, p.vip)
                if p.left < 20:
                    await send(p, {"type": "nolimit", "vip": p.vip})
                    continue
                p.since = time.time()
                queue.append(p)
                p.qtask = asyncio.create_task(nomatch(p))
                await send(p, {"type": "searching", "wait": MATCH_WAIT})
                await match()
            elif t == "cancel":
                await leave_queue(p)
                await send(p, {"type": "idle", "left": left_for(p.uid, p.vip)})
            elif t == "signal" and p.room and not p.room.closed:
                await send(p.room.other(p), {"type": "signal", "data": d.get("data")})
            elif t == "connected" and p.room and not p.room.closed:
                p.connected = True
                room = p.room
                if room.a.connected and room.b.connected and not room.t0:
                    room.t0 = time.time()
                    secs = max(10, min(room.a.left, room.b.left))
                    unlimited = secs >= UNLIM
                    if not unlimited:
                        room.tasks.append(asyncio.create_task(room_timer(room, secs)))
                    for x in (room.a, room.b):
                        await send(x, {"type": "started", "secs": 0 if unlimited else secs})
            elif t == "failed" and p.room:
                await end_room(p.room, "failed", count=False)
            elif t in ("hangup", "next") and p.room:
                await end_room(p.room, "hangup")
            elif t == "report":
                await do_report(p)
            elif t == "block":
                await do_report(p, block_only=True)
    except Exception as e:
        print("ws xato:", e)
    finally:
        if p:
            await cleanup(p)
    return ws


# ---------------- sun'iy intellekt ustoz (odam topilmasa yoki xohlasa): Groq + edge-tts ----------------
AI_DAILY = int(os.getenv("AI_DAILY", "300"))  # kuniga necha soniya
RU_VOICE = os.getenv("TALK_RU_VOICE", "ru-RU-SvetlanaNeural")
STT_MODELS = ["whisper-large-v3-turbo", "whisper-large-v3"]
LLM_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
GROQ = "https://api.groq.com/openai/v1"
ai_sessions = {}
ai_gate = asyncio.Semaphore(12)
key_bad = {}
key_rr = [0]
key_told = {}


class Busy(Exception):
    pass


def groq_keys():
    """Kalitlar: .env (GROQ_API_KEYS) + botning keys.json fayli (bot admin panelidan qo'shilgan; bir kompyuterda bo'lsa o'zi topadi)."""
    ks = [k.strip() for k in (os.getenv("GROQ_API_KEYS", "") + "," + os.getenv("GROQ_API_KEY", "")).split(",") if k.strip()]
    for path in (os.getenv("KEYS_PATH", ""), os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot", "keys.json")):
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    v = json.load(f).get("groq")
                ks += [v] if isinstance(v, str) else list(v or [])
            except (OSError, ValueError, AttributeError):
                pass
            break
    return list(dict.fromkeys(k for k in ks if isinstance(k, str) and k))


def ai_left(uid):
    r = db.execute("SELECT secs FROM ai_use WHERE user_id=? AND day=?", (uid, today())).fetchone()
    return max(0, AI_DAILY - (r[0] if r else 0))


def ai_add(uid, secs):
    db.execute("INSERT INTO ai_use(user_id, day, secs) VALUES(?,?,?) ON CONFLICT(user_id, day) DO UPDATE SET secs=secs+?",
               (uid, today(), int(secs), int(secs)))
    db.execute("DELETE FROM ai_use WHERE day < ?", ((datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d"),))
    db.commit()


TALK_RULES = {
    "A0": ("Foydalanuvchi rus tilini noldan o'rganyapti. Faqat eng oddiy so'z va 2-3 so'zli iboralar bilan so'ra (salomlashish, tanishuv, raqamlar).", 1),
    "A1": ("Faqat juda oddiy 3-5 so'zli savol ber, hozirgi zamonda (ism, oila, kun tartibi).", 1),
    "A2": ("Oddiy savol ber; o'tgan va kelasi zamonga ham o'tishing mumkin (xarid, yo'l, ovqat).", 1),
    "B1": ("Tabiiy, tushunarli savol ber: fikr va sabab so'ra ('почему').", 1),
    "B2": ("Erkin, boyroq tilda savol ber, fikrini asoslashni talab qil.", 2),
    "C1": ("Tabiiy tezlikda, murakkab tuzilma va iboralar bilan savol ber.", 2),
}
TALK_HELLO = {
    "A0": ("Привет! Меня зовут Анна. Как тебя зовут?", "Salom! Mening ismim Anna. Sizning ismingiz nima?"),
    "A1": ("Привет! Как тебя зовут?", "Salom! Ismingiz nima?"),
    "A2": ("Привет! Что ты делал сегодня?", "Salom! Bugun nima qildingiz?"),
    "B1": ("Привет! Расскажи, чем ты занимаешься?", "Salom! Nima bilan shug'ullanasiz, ayting."),
    "B2": ("Здравствуйте! Расскажите о вашей работе или учёбе.", "Assalomu alaykum! Ishingiz yoki o'qishingiz haqida gapirib bering."),
    "C1": ("Здравствуйте! Давайте поговорим о том, что вас волнует в последнее время.", "Assalomu alaykum! Oxirgi paytlarda sizni nima tashvishlantirayotgani haqida gaplashamiz."),
}
HELLO_TOPIC = ("Привет! Что ты узнал на уроке сегодня? Расскажи.", "Salom! Bugun darsda nimani bildingiz? Aytib bering.")


def ai_prompt(level, goal, topic):
    rule, n = TALK_RULES.get(level, TALK_RULES["A1"])
    t = (f"BUGUNGI DARS MAVZUSI: {topic}. Savollaringni shu mavzu bo'yicha ber va o'quvchi nimani tushunganini tekshir.\n" if topic else "")
    return (f"Sen rus tili ustozisan. O'quvchi o'zbek, ovozli xabar yuboryapti (matn ovozdan avtomatik olingan: tinish belgilari "
            f"va kichik noaniqliklarni xato deb hisoblama). Daraja: {level}. O'quvchining maqsadi: {goal or 'umumiy'}.\n"
            f"{t}{rule}\n\nJAVOB FORMATI (qat'iy, aynan shu uch qator):\n"
            f"RU: <rus tilida FAQAT SAVOL ({n} gap). Tushuntirma, izoh, tarjima yozma>\n"
            "TR: <RU qatordagi savolning o'zbekcha tarjimasi>\n"
            "UZ: <o'quvchi xato qilgan bo'lsa FAQAT BITTA qisqa o'zbekcha gap: «Siz: ... → To'g'risi: ...». Xato bo'lmasa faqat: ->\n\n"
            "Javob juda qisqa bo'lsin. Faqat rus tili mashqi haqida gapir; boshqa mavzu so'ralsa, muloyim rad etib savolga qayt. "
            "Belgilar, emoji va markdown ishlatma.")


def ai_parse(text):
    ru = re.search(r"RU:\s*(.+?)(?=\n\s*(?:TR|UZ):|\Z)", text, re.S)
    tr = re.search(r"TR:\s*(.+?)(?=\n\s*UZ:|\Z)", text, re.S)
    uz = re.search(r"UZ:\s*(.+)", text, re.S)
    ru = ru.group(1).strip() if ru else ""
    tr = tr.group(1).strip() if tr else ""
    uz = uz.group(1).strip() if uz else ""
    if uz in ("-", "—", "–"):
        uz = ""
    if not ru and not uz:
        ru = text.strip()
    return ru, uz, tr


async def groq_call(http, path, make):
    """Kalitlarni navbat bilan sinaydi. 429 bo'lsa kalit almashtirilmaydi (Busy)."""
    keys = groq_keys()
    if not keys:
        raise RuntimeError("Groq kaliti yo'q")
    key_rr[0] = (key_rr[0] + 1) % len(keys)
    keys = keys[key_rr[0]:] + keys[:key_rr[0]]
    live = [k for k in keys if time.time() - key_bad.get(k, 0) > 120] or keys
    last = "?"
    for k in live:
        opts = make()
        try:
            async with http.post(GROQ + path, headers={"Authorization": "Bearer " + k}, timeout=aiohttp.ClientTimeout(total=45), **opts) as r:
                if r.status == 429:
                    raise Busy()
                if r.status in (401, 402, 403) or r.status >= 500:
                    last = f"HTTP {r.status}"
                    key_bad[k] = time.time()
                    if time.time() - key_told.get(k, 0) > 3600:
                        key_told[k] = time.time()
                        notify_admins(f"⚠️ Groq kaliti (…{k[-4:]}) ishlamayapti: {last}")
                    continue
                if r.status != 200:
                    raise RuntimeError(f"Groq {r.status}: {(await r.text())[:200]}")
                key_bad.pop(k, None)
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = type(e).__name__
            key_bad[k] = time.time()
    raise RuntimeError("Hamma Groq kalitlari ishlamadi: " + last)


async def groq_models(http, path, models, make):
    for i, m in enumerate(models):
        try:
            return await groq_call(http, path, lambda: make(m))
        except Busy:
            if i == len(models) - 1:
                raise


async def ai_stt(raw, ctype, level):
    ext = "webm" if "webm" in ctype else "m4a" if ("mp4" in ctype or "aac" in ctype) else "ogg" if "ogg" in ctype else "wav" if "wav" in ctype else "webm"

    def make(model):
        form = aiohttp.FormData()
        form.add_field("file", raw, filename="a." + ext, content_type=ctype or "audio/webm")
        form.add_field("model", model)
        form.add_field("temperature", "0")
        if level != "A0":
            form.add_field("language", "ru")
        return {"data": form}
    async with aiohttp.ClientSession() as http:
        return ((await groq_models(http, "/audio/transcriptions", STT_MODELS, make)).get("text") or "").strip()


async def ai_llm(msgs, level):
    models = LLM_MODELS[::-1] if level in ("A0", "A1", "A2") else LLM_MODELS
    async with aiohttp.ClientSession() as http:
        res = await groq_models(http, "/chat/completions", models,
                                lambda m: {"json": {"model": m, "messages": msgs, "temperature": 0.5, "max_tokens": 130}})
    return res["choices"][0]["message"]["content"]


async def ai_tts(text, level):
    import edge_tts
    out = b""
    async for ch in edge_tts.Communicate(text, RU_VOICE, rate="-15%" if level in ("A0", "A1") else "+0%").stream():
        if ch["type"] == "audio":
            out += ch["data"]
    if not out:
        raise RuntimeError("TTS bo'sh")
    return out


def ai_session(info):
    t = time.time()
    for k in [k for k, v in ai_sessions.items() if t - v["ts"] > 3 * 3600]:
        ai_sessions.pop(k, None)
    uid = int(info["u"])
    ses = ai_sessions.get(uid)
    if not ses:
        ses = ai_sessions[uid] = {"hist": [], "level": str(info.get("l") or "A1"), "goal": info.get("g") or "", "topic": info.get("p") or "", "ts": t}
    ses["ts"] = t
    return ses


async def ai_start(request):
    try:
        info = verify((await request.json()).get("token"))
    except Exception:
        info = None
    if not info or banned(int(info["u"])):
        return web.json_response({"error": "Havola eskirgan yoki ruxsat yo'q."}, status=403)
    if not groq_keys():
        return web.json_response({"error": "Ustoz hozir vaqtincha ishlamayapti."}, status=503)
    uid = int(info["u"])
    ai_sessions.pop(uid, None)
    ses = ai_session(info)
    ru, tr = (HELLO_TOPIC if ses["topic"] and ses["level"] != "A0" else TALK_HELLO.get(ses["level"], TALK_HELLO["A1"]))
    ses["hist"].append({"role": "assistant", "content": "RU: " + ru})
    try:
        audio = await ai_tts(ru, ses["level"])
    except Exception as e:
        print("TTS:", e)
        audio = b""
    return web.json_response({"ru": ru, "tr": tr, "audio": base64.b64encode(audio).decode(), "left": ai_left(uid)})


async def ai_turn(request):
    info = verify(request.headers.get("X-Token"))
    if not info or banned(int(info["u"])):
        return web.json_response({"error": "Havola eskirgan yoki ruxsat yo'q."}, status=403)
    uid = int(info["u"])
    if ai_left(uid) <= 0:
        return web.json_response({"done": True, "left": 0})
    raw = await request.read()
    if not raw or len(raw) < 800:
        return web.json_response({"error": "Ovoz juda qisqa. Qayta urinib ko'ring."}, status=400)
    try:
        dur = max(1, min(30, int(float(request.headers.get("X-Dur", "5")))))
    except ValueError:
        dur = 5
    ses = ai_session(info)
    try:
        async with ai_gate:
            heard = await ai_stt(raw, request.headers.get("Content-Type", ""), ses["level"])
            if not heard:
                return web.json_response({"error": "Ovozni eshita olmadim. Aniqroq gapiring."}, status=422)
            ses["hist"] = ses["hist"][-4:] + [{"role": "user", "content": heard}]
            msgs = [{"role": "system", "content": ai_prompt(ses["level"], ses["goal"], ses["topic"])}] + ses["hist"]
            reply = await ai_llm(msgs, ses["level"])
            ru, uz, tr = ai_parse(reply)
            ses["hist"].append({"role": "assistant", "content": "RU: " + ru})
            audio = await ai_tts(ru, ses["level"]) if ru else b""
    except Busy:
        return web.json_response({"error": "Ustoz hozir band. Bir oz kutib, qayta urinib ko'ring."}, status=429)
    except Exception as e:
        print("AI xato:", type(e).__name__, str(e)[:200])
        return web.json_response({"error": "Xatolik bo'ldi, vaqtingizdan hisoblanmadi. Qayta urinib ko'ring."}, status=500)
    ai_add(uid, dur + min(20, len(ru) // 14 + 2))
    left = ai_left(uid)
    return web.json_response({"heard": heard, "ru": ru, "uz": uz, "tr": tr, "audio": base64.b64encode(audio).decode(), "left": left, "done": left <= 0})


async def ai_end(request):
    try:
        info = verify((await request.json()).get("token"))
    except Exception:
        info = None
    if info:
        ai_sessions.pop(int(info["u"]), None)
    return web.json_response({"ok": True})


HERE = os.path.dirname(os.path.abspath(__file__))


async def page(request):
    return web.FileResponse(os.path.join(HERE, "index.html"), headers={"Cache-Control": "no-store"})


async def admin(request):
    if not ADMIN_KEY or not hmac.compare_digest(request.query.get("key", ""), ADMIN_KEY):
        return web.Response(status=403, text="403")
    uid = request.query.get("id", "")
    if not uid.isdigit():
        return web.Response(status=400, text="id kerak")
    uid = int(uid)
    if request.path.endswith("unban"):
        db.execute("DELETE FROM ban WHERE user_id=?", (uid,))
        db.execute("DELETE FROM report WHERE target_id=?", (uid,))
        db.commit()
        return web.Response(text=f"✅ {uid} qaytarildi")
    db.execute("INSERT OR IGNORE INTO ban(user_id) VALUES(?)", (uid,))
    db.commit()
    p = active.get(uid)
    if p and p.room:
        await end_room(p.room, "banned")
    return web.Response(text=f"🚫 {uid} bloklandi")


async def stats(request):
    if not ADMIN_KEY or not hmac.compare_digest(request.query.get("key", ""), ADMIN_KEY):
        return web.Response(status=403, text="403")
    return web.json_response({"online": len(active), "waiting": len(queue), "talking": sum(1 for p in active.values() if p.room)})


def make_app():
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.router.add_post("/ai/start", ai_start)
    app.router.add_post("/ai/turn", ai_turn)
    app.router.add_post("/ai/end", ai_end)
    app.router.add_get("/", page)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    app.router.add_get("/admin/ban", admin)
    app.router.add_get("/admin/unban", admin)
    app.router.add_get("/admin/stats", stats)
    return app


async def main():
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"Suhbat sayti ishlayapti: port {PORT}")
    await matcher()


if __name__ == "__main__":
    asyncio.run(main())
