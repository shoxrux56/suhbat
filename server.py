"""Suhbat + rolli o'yin.
  • Oddiy suhbat: ikki odam juftlanadi, ovoz ularning o'rtasida to'g'ridan-to'g'ri ketadi (server faqat signal tashiydi).
  • O'yin: ikkalasi «O'yin» ni bosadi -> rol beriladi -> navbat bilan 20 soniya gapiradi -> AI to'g'ri/xatoni aniqlaydi.
Baza yo'q: hammasi xotirada. 100 ta vaziyat scenarios.json faylida."""
import asyncio, hmac, json, os, random, re, time
import aiohttp
from aiohttp import web, WSMsgType

PORT = int(os.getenv("PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))
TURN_SECS = int(os.getenv("TURN_SECS", "20"))      # bitta navbatda gapirish vaqti
HEARTS = 3                                          # har kimga yurakcha
MAX_TURNS = int(os.getenv("MAX_TURNS", "12"))       # o'yinda jami navbat (har kimga yarmi)
GROQ = os.getenv("GROQ_BASE", "https://api.groq.com/openai/v1")
STT_MODELS = [m for m in os.getenv("STT_MODELS", "whisper-large-v3-turbo,whisper-large-v3").split(",") if m]
LLM_MODELS = [m for m in os.getenv("LLM_MODELS", "llama-3.3-70b-versatile,llama-3.1-8b-instant").split(",") if m]
GATE = asyncio.Semaphore(int(os.getenv("GROQ_PARALLEL", "6")))  # bir vaqtda Groq'ga ketadigan so'rovlar (server qotmasin)
MAX_AUDIO = 700_000

try:
    with open(os.path.join(HERE, "scenarios.json"), encoding="utf-8") as f:
        SCENARIOS = json.load(f)
except (OSError, ValueError):  # fayl yuklanmagan bo'lsa ham sayt va oddiy suhbat ishlayveradi
    print("scenarios.json topilmadi: o'yin o'chiq")
    SCENARIOS = []


# ---------------- ICE (ovoz ulanishi) ----------------
def ice_servers():
    ice = [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302", "stun:stun.cloudflare.com:3478"]}]
    urls = [u.strip() for u in os.getenv("TURN_URLS", "").split(",") if u.strip()]
    if urls:
        ice.append({"urls": urls, "username": os.getenv("TURN_USER", ""), "credential": os.getenv("TURN_PASS", "")})
    else:  # ochiq sinov TURN (ishlamay qolishi mumkin; barqaror ishlashi uchun o'zingiznikini yozing)
        ice.append({"urls": ["turn:openrelay.metered.ca:80", "turn:openrelay.metered.ca:443",
                             "turns:openrelay.metered.ca:443?transport=tcp"],
                    "username": "openrelayproject", "credential": "openrelayproject"})
    return ice


# ---------------- Groq: ovozdan matn + hakam ----------------
class Busy(Exception):
    pass


HTTP = None
key_cool, key_rr = {}, [0]


PUSHED = []  # bot yuborgan kalitlar (faqat xotirada; bot har 5 daqiqada qayta yuboradi)


def groq_keys():
    ks = (os.getenv("GROQ_API_KEYS", "") + "," + os.getenv("GROQ_API_KEY", "")).split(",") + PUSHED
    return list(dict.fromkeys(k.strip() for k in ks if k.strip()))


async def keys_in(request):
    """Bot kalitlarni shu yerga yuboradi: POST /keys, sarlavha X-Secret = TALK_SECRET."""
    secret = os.getenv("TALK_SECRET", "").strip()
    if not secret or not hmac.compare_digest(request.headers.get("X-Secret", ""), secret):
        return web.Response(status=403, text="no")
    try:
        d = await request.json()
    except ValueError:
        return web.Response(status=400, text="bad")
    ks = [k for k in (d.get("groq") or []) if isinstance(k, str) and k.startswith("gsk_") and len(k) < 200][:20]
    PUSHED[:] = ks
    return web.Response(text=str(len(ks)))


async def groq(path, make):
    keys = groq_keys()
    if not keys:
        raise RuntimeError("Groq kaliti yo'q")
    key_rr[0] = (key_rr[0] + 1) % len(keys)
    keys = keys[key_rr[0]:] + keys[:key_rr[0]]
    now = time.time()
    live = [k for k in keys if key_cool.get(k, 0) <= now] or keys
    for k in live:
        try:
            async with HTTP.post(GROQ + path, headers={"Authorization": "Bearer " + k}, timeout=aiohttp.ClientTimeout(total=25), **make()) as r:
                if r.status == 429:
                    key_cool[k] = time.time() + 20
                    continue
                if r.status in (401, 402, 403):
                    key_cool[k] = time.time() + 600
                    continue
                if r.status != 200:
                    raise RuntimeError(f"Groq {r.status}: {(await r.text())[:150]}")
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            key_cool[k] = time.time() + 10
    raise Busy()


async def groq_models(path, models, make):
    for m in models:
        try:
            return await groq(path, lambda: make(m))
        except Busy:
            continue
    raise Busy()


HALLU = re.compile(r"субтитр|продолжение следует|спасибо за просмотр|редактор субтитров|dimatorzok|подписывайтесь", re.I)


async def stt(raw, ext):
    ctype = {"webm": "audio/webm", "mp4": "audio/mp4", "ogg": "audio/ogg"}.get(ext, "audio/webm")

    def make(model):
        form = aiohttp.FormData()
        form.add_field("file", raw, filename="a." + ext, content_type=ctype)
        form.add_field("model", model)
        form.add_field("language", "ru")
        form.add_field("temperature", "0")
        return {"data": form}
    res = await groq_models("/audio/transcriptions", STT_MODELS, make)
    t = (res.get("text") or "").strip()
    return "" if HALLU.search(t) or len(re.sub(r"[\W_]+", "", t)) < 2 else t


JUDGE_SYS = (
    "Sen rus tili o'rgatuvchi o'yinning hakamisan. Ikki o'quvchi (o'zbeklar) rus tilida rolli suhbat qilyapti. "
    "Senga vaziyat, o'quvchining roli, oldingi gaplar va uning hozirgi gapi (ovozdan matnga o'girilgan) beriladi.\n"
    "QOIDALAR:\n"
    "- ok=false FAQAT shu hollarda: (1) gapda ANIQ grammatik xato bor (so'z shakli, qo'shimcha, jins, kelishik, tuslanish, "
    "so'z tartibi, noto'g'ri so'z tanlash), yoki (2) gap vaziyatga, rolga yoki oldingi gapga umuman mos kelmaydi "
    "(mavzudan tashqari, ma'nosiz), yoki (3) gap rus tilida emas.\n"
    "- Matn ovozdan avtomatik olingan: tinish belgilari, bosh harf, kichik eshitish noaniqliklari, to'ldiruvchi so'zlar "
    "(э-э, ну) va oddiy og'zaki qisqartmalar xato EMAS. Aniq ishonching komil bo'lmasa ok=true.\n"
    "- Qisqa tabiiy javoblar («Да», «Нет, спасибо», «До свидания») to'g'ri.\n"
    "- Sen faqat o'quvchining gapini baholaysan, suhbatdoshning gapini emas.\n"
    "- fix: ok=false bo'lsa, o'quvchining gapini imkon qadar kam o'zgartirib to'g'rilangan rus gapi; ok=true bo'lsa bo'sh.\n"
    "- why: ok=false bo'lsa, xatoni o'zbek tilida bitta qisqa gapda tushuntir; ok=true bo'lsa bo'sh.\n"
    'FAQAT JSON qaytar: {"ok":true,"fix":"","why":""}')


async def judge(g, i, text):
    sc, me, other = g.sc, g.sc["a"] if i == 0 else g.sc["b"], g.sc["b"] if i == 0 else g.sc["a"]
    ctx = "\n".join(f"{(g.sc['a'] if w == 0 else g.sc['b'])['ru']}: {t}" for w, t in g.log[-4:]) or "(suhbat endi boshlandi)"
    user = (f"Vaziyat: {sc['title']} ({sc['place']}). O'quvchining roli: {me['ru']} ({me['uz']}), maqsadi: {me['goal']}. "
            f"Suhbatdosh roli: {other['ru']}.\nOldingi gaplar:\n{ctx}\n\nO'quvchining hozirgi gapi: «{text}»")
    res = await groq_models("/chat/completions", LLM_MODELS, lambda m: {"json": {
        "model": m, "temperature": 0, "max_tokens": 220, "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": JUDGE_SYS}, {"role": "user", "content": user}]}})
    raw = res["choices"][0]["message"]["content"]
    try:
        d = json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.S)
        d = json.loads(m.group(0)) if m else {"ok": True}
    ok = bool(d.get("ok", True))
    return ok, ("" if ok else str(d.get("fix") or "")[:200]), ("" if ok else str(d.get("why") or "")[:240])


# ---------------- ulanishlar va navbatlar ----------------
class Peer:
    def __init__(self, ws):
        self.ws, self.partner, self.game, self.gi, self.ext, self.prop = ws, None, None, 0, "webm", False
        self.relay, self.avoid = False, set()  # relay: ovoz server orqali ketadi (to'g'ridan-to'g'ri ulanmasa); avoid: yaqinda juftlangan odamlar


peers, waiting = set(), []


async def send(p, obj):
    try:
        await p.ws.send_json(obj)
    except Exception:
        pass


class Game:
    def __init__(self, a, b):
        self.p = [a, b]
        self.sc = random.choice(SCENARIOS)
        self.hearts, self.stars, self.ready = [HEARTS, HEARTS], [0, 0], [False, False]
        self.turn, self.state, self.over, self.log = 0, "ready", False, []
        self.task = None

    def cancel(self):
        if self.task and self.task is not asyncio.current_task() and not self.task.done():
            self.task.cancel()


async def both(g, obj):
    for p in g.p:
        await send(p, obj)


async def end_game(g, winner, reason):
    """O'yin tugadi: odamlar suhbatda qoladi (ovoz ulanishi buzilmaydi)."""
    if g.over:
        return
    g.over = True
    g.cancel()
    await both(g, {"t": "end", "winner": winner, "hearts": g.hearts, "stars": g.stars, "reason": reason})
    for p in g.p:
        p.game, p.prop = None, False


async def unpair(p):
    if p in waiting:
        waiting.remove(p)
    p.prop = False
    g = p.game
    if g and not g.over:
        g.over = True
        g.cancel()
        for x in g.p:
            x.game = None
    q = p.partner
    p.relay = False
    if q:
        p.partner = q.partner = None
        q.prop = q.relay = False
        await send(q, {"t": "left"})


async def join(p):
    await unpair(p)
    waiting.append(p)
    await match()
    if p in waiting:
        await send(p, {"t": "wait", "n": len(peers)})


def pick():
    """Yangi odamni afzal ko'radi: yaqinda gaplashgan juftlikni qayta ulamaydi. Boshqa odam bo'lmasa, o'shalarni ham ulaydi."""
    w = waiting
    for i, a in enumerate(w):
        for b in w[i + 1:]:
            if id(b) not in a.avoid and id(a) not in b.avoid:
                return a, b
    return w[0], w[1]


async def match():
    while True:
        waiting[:] = [x for x in waiting if not x.ws.closed]
        if len(waiting) < 2:
            return
        a, b = pick()
        waiting.remove(a)
        waiting.remove(b)
        a.partner, b.partner = b, a
        a.avoid.add(id(b)); b.avoid.add(id(a))
        if len(a.avoid) > 30: a.avoid.clear()
        if len(b.avoid) > 30: b.avoid.clear()
        await send(a, {"t": "match", "init": True})
        await send(b, {"t": "match", "init": False})


async def start_game(a, b):
    """Ikki suhbatdosh o'yinga rozi bo'ldi: ovoz o'sha ulanishda davom etadi, faqat rol beriladi."""
    if random.random() < 0.5:
        a, b = b, a
    g = Game(a, b)
    a.game, b.game, a.gi, b.gi = g, g, 0, 1
    a.prop = b.prop = False
    for p, i in ((a, 0), (b, 1)):
        me, ot = (g.sc["a"], g.sc["b"]) if i == 0 else (g.sc["b"], g.sc["a"])
        await send(p, {"t": "gstart", "me": i, "secs": TURN_SECS, "hearts": HEARTS, "turns": MAX_TURNS,
                       "sc": {"em": g.sc["em"], "title": g.sc["title"], "place": g.sc["place"]},
                       "you": me, "other": {"ru": ot["ru"], "uz": ot["uz"]},
                       "first": i == 0, "open": g.sc["open"] if i == 0 else ""})
    g.task = asyncio.create_task(ready_timeout(g))


async def ready_timeout(g):
    try:
        await asyncio.sleep(45)
    except asyncio.CancelledError:
        return
    if g.state == "ready" and not g.over:
        g.over = True
        for x in g.p:
            x.game = None
            await send(x, {"t": "gcancel"})


# ---------------- o'yin yuragi ----------------
async def begin_turn(g):
    if g.over:
        return
    idx = g.turn % 2
    g.state = "listen"
    for p in g.p:
        await send(p, {"t": "turn", "n": g.turn + 1, "mine": p.gi == idx})
    g.task = asyncio.create_task(turn_timeout(g, g.turn))


async def turn_timeout(g, n):
    try:
        await asyncio.sleep(TURN_SECS + 7)  # 7 s: yozib olingan ovoz yuborilishi uchun
    except asyncio.CancelledError:
        return
    if g.turn == n and g.state == "listen" and not g.over:
        g.state = "judge"
        await settle(g, n % 2, False, "", "", "Vaqt tugadi: hech narsa aytilmadi.")


async def on_audio(p, raw):
    g = p.game
    if not g or g.over or g.state != "listen" or p.gi != g.turn % 2 or len(raw) > MAX_AUDIO:
        return
    g.state = "judge"
    g.cancel()
    n, i = g.turn, p.gi
    await both(g, {"t": "judging"})
    g.task = asyncio.create_task(run_judge(g, n, i, raw, p.ext))


async def run_judge(g, n, i, raw, ext):
    try:
        async with GATE:
            text = await asyncio.wait_for(stt(raw, ext), 25)
            if not text:
                return await settle(g, i, False, "", "", "Hech narsa eshitilmadi. Balandroq va aniqroq gapiring.")
            ok, fix, why = await asyncio.wait_for(judge(g, i, text), 25)
        g.log.append((i, text))
        await settle(g, i, ok, text, fix, why)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # AI band / xato: o'yinchi jazolanmaydi
        print("hakam xatosi:", repr(e))
        await settle(g, i, None, "", "", "AI hozir band, bu navbat hisobga olinmadi.")


async def settle(g, i, ok, said, fix, why):
    if g.over:
        return
    if ok is True:
        g.stars[i] += 1
    elif ok is False:
        g.hearts[i] -= 1
    await both(g, {"t": "result", "who": i, "ok": ok, "said": said, "fix": fix, "why": why, "hearts": g.hearts, "stars": g.stars})
    g.turn += 1
    if g.hearts[i] <= 0:
        await asyncio.sleep(3)
        return await end_game(g, 1 - i, "hearts")
    if g.turn >= MAX_TURNS:
        await asyncio.sleep(3)
        w = -1
        if g.stars[0] != g.stars[1]:
            w = 0 if g.stars[0] > g.stars[1] else 1
        elif g.hearts[0] != g.hearts[1]:
            w = 0 if g.hearts[0] > g.hearts[1] else 1
        return await end_game(g, w, "turns")
    g.state = "pause"
    await asyncio.sleep(5)
    if not g.over:
        await begin_turn(g)


# ---------------- WebSocket ----------------
async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=MAX_AUDIO + 1024)
    await ws.prepare(request)
    p = Peer(ws)
    peers.add(p)
    await send(p, {"t": "hello", "ice": ice_servers(), "game": bool(groq_keys() and SCENARIOS), "n": len(peers)})
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                d0 = msg.data
                if d0[:1] == b"\x01":  # jonli ovoz (relay): sherikka o'tkazamiz
                    if p.relay and p.partner:
                        try:
                            await p.partner.ws.send_bytes(d0)
                        except Exception:
                            pass
                elif d0[:1] == b"\x00":  # o'yin: ovoz yozuvi (hakam uchun)
                    await on_audio(p, d0[1:])
                continue
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            t = d.get("t")
            if t == "join":
                await join(p)
            elif t == "leave":
                await unpair(p)
            elif t in ("offer", "answer", "ice") and p.partner:
                await send(p.partner, {"t": t, "d": d.get("d")})
            elif t == "gprop":  # suhbatdosh o'yinga taklif
                q = p.partner
                if not q or p.game or q.game:
                    continue
                if not (groq_keys() and SCENARIOS):
                    await send(p, {"t": "nogame"})
                elif q.prop:  # u ham taklif qilgan edi: darhol boshlaymiz
                    await start_game(q, p)
                else:
                    p.prop = True
                    await send(q, {"t": "gprop"})
            elif t == "gacc" and p.partner and p.partner.prop and not p.game and not p.partner.game:
                await start_game(p.partner, p)
            elif t == "gno" and p.partner and p.partner.prop:
                p.partner.prop = False
                await send(p.partner, {"t": "gno"})
            elif t == "relay" and p.partner and not p.relay:  # to'g'ridan-to'g'ri ulanmadi: ovozni server orqali o'tkazamiz
                p.relay = p.partner.relay = True
                await send(p, {"t": "relay"})
                await send(p.partner, {"t": "relay"})
            elif t == "rec":
                p.ext = d.get("ext") if d.get("ext") in ("webm", "mp4", "ogg") else "webm"
            elif t == "ready" and p.game and p.game.state == "ready" and not p.game.over:
                g = p.game
                g.ready[p.gi] = True
                if all(g.ready):
                    g.cancel()
                    await begin_turn(g)
                else:
                    await send(g.p[1 - p.gi], {"t": "peer_ready"})
    except Exception as e:
        print("ws xato:", e)
    finally:
        peers.discard(p)
        await unpair(p)
    return ws


async def online_loop():
    while True:
        await asyncio.sleep(4)
        n = len(peers)
        for p in list(peers):
            await send(p, {"t": "online", "n": n})


async def page(request):
    return web.FileResponse(os.path.join(HERE, "index.html"), headers={"Cache-Control": "no-cache"})


async def on_start(app):
    global HTTP
    HTTP = aiohttp.ClientSession()
    app["loop"] = asyncio.create_task(online_loop())


async def on_stop(app):
    await HTTP.close()


def make_app():
    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    app.router.add_post("/keys", keys_in)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
