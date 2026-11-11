"""Rumiyo — ruscha ovozli jahldor ustoz AI (alohida server, Suhbat saytidan mustaqil).

Oqim: bot darsdan keyingi testdan so'ng imzolangan havola beradi -> sayt ochiladi -> odam ruscha gapiradi
-> Groq Whisper (ovoz->matn) -> LLM (Groq -> OpenRouter -> Gemini) -> edge-tts (matn->ovoz).
Rumiyo bugungi mavzu va bugungi gaplar bo'yicha suhbatlashadi, so'raydi, tekshiradi; kuniga 5 daqiqa; oxirida botga qaytaradi.
Hazil/pichinglar bazasi (gaplar_db.py) botdagi adminning "AI ni o'qitish" tugmasidan keladi: bot -> /admin/add va /admin/sync.

Kalitlar FAQAT muhit o'zgaruvchilarida (Render -> Environment), kodga yoki GitHubga yozilmaydi:
  TALK_SECRET (botdagi bilan bir xil), GROQ_API_KEYS (vergul bilan), OPENROUTER_API_KEY, GEMINI_API_KEY
"""
import asyncio, base64, difflib, hashlib, hmac, json, os, random, re, sqlite3, time
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", HERE)
os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault("GAPLAR_DB", os.path.join(DATA_DIR, "gaplar.db"))
import gaplar_db as G  # noqa: E402  (GAPLAR_DB ni yuqorida o'rnatdik)

SECRET = os.getenv("TALK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))
BOT_URL = os.getenv("BOT_URL", "https://t.me/rumiyo_bot")
TRENER_DATA = os.getenv("TRENER_DATA", "https://shoxrux56.github.io/trener/data.js")
TZ = timezone(timedelta(hours=5))
GROQ = os.getenv("GROQ_BASE", "https://api.groq.com/openai/v1")
OPENROUTER = os.getenv("OPENROUTER_BASE", "https://openrouter.ai/api/v1")
GEMINI = os.getenv("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta")
STT_MODELS = ["whisper-large-v3-turbo", "whisper-large-v3"]
LLM_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
OR_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
RU_VOICE = os.getenv("RU_VOICE", "ru-RU-SvetlanaNeural")
if not SECRET:
    raise SystemExit("TALK_SECRET yo'q (botdagi bilan bir xil bo'lsin)")

db = sqlite3.connect(os.path.join(DATA_DIR, "rumiyo.db"), check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
db.execute("CREATE TABLE IF NOT EXISTS use(user_id INTEGER, day TEXT, secs INTEGER DEFAULT 0, PRIMARY KEY(user_id, day))")
db.execute("CREATE TABLE IF NOT EXISTS notes(user_id INTEGER PRIMARY KEY, text TEXT, ts INTEGER)")
db.commit()


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


# ---------- sozlamalar (bot / sahifadan keladi) ----------
def daily_secs():
    try:
        return max(30, int(float(os.getenv("DAILY_MIN") or G.setting("dailyMin", 5)) * 60))
    except (TypeError, ValueError):
        return 300


def humor_level():
    try:
        return max(0, min(3, int(G.setting("humor", 3))))
    except (TypeError, ValueError):
        return 3


def mem_msgs():
    try:
        return max(6, min(80, int(G.setting("memoryTurns", 30)) * 2))
    except (TypeError, ValueError):
        return 60


def cta_text():
    return str(G.setting("cta", "") or "Botga qayting — darsni davom ettiring 👇")


# ---------- foydalanish vaqti ----------
def left_for(uid):
    r = db.execute("SELECT secs FROM use WHERE user_id=? AND day=?", (uid, today())).fetchone()
    return max(0, daily_secs() - (r[0] if r else 0))


def add_use(uid, secs):
    db.execute("INSERT INTO use(user_id, day, secs) VALUES(?,?,?) ON CONFLICT(user_id, day) DO UPDATE SET secs=secs+excluded.secs",
               (uid, today(), int(secs)))
    db.commit()


# ---------- imzolangan havola ----------
def verify(token):
    try:
        body, sig = (token or "").split(".", 1)
        good = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if d["e"] < time.time():
            return None
        int(d["u"])
        return d
    except Exception:
        return None


def is_admin(request):
    return hmac.compare_digest(request.headers.get("X-Secret", ""), SECRET)


# ---------- kalitlar ----------
KEYS_FILE = os.path.join(DATA_DIR, "keys.json")


def keys(kind):
    env = {"groq": ("GROQ_API_KEYS", "GROQ_API_KEY"), "openrouter": ("OPENROUTER_API_KEYS", "OPENROUTER_API_KEY"),
           "gemini": ("GEMINI_API_KEYS", "GEMINI_API_KEY")}[kind]
    ks = [k.strip() for k in (os.getenv(env[0], "") + "," + os.getenv(env[1], "")).split(",") if k.strip()]
    try:
        with open(KEYS_FILE, encoding="utf-8") as f:
            v = json.load(f).get(kind)
        ks += [v] if isinstance(v, str) else list(v or [])
    except (OSError, ValueError, AttributeError):
        pass
    return list(dict.fromkeys(k for k in ks if isinstance(k, str) and k))


class Busy(Exception):
    pass


key_bad = {}   # kalit -> vaqt (vaqtincha ishlamayapti)
key_rr = {}


def rotated(kind):
    ks = keys(kind)
    if not ks:
        return []
    key_rr[kind] = (key_rr.get(kind, -1) + 1) % len(ks)
    ks = ks[key_rr[kind]:] + ks[:key_rr[kind]]
    return [k for k in ks if time.time() - key_bad.get(k, 0) > 60] or ks


async def post(http, url, headers=None, **kw):
    async with http.post(url, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=45), **kw) as r:
        txt = await r.text()
        try:
            return r.status, json.loads(txt)
        except ValueError:
            return r.status, {"_raw": txt[:200]}


# ---------- LLM zanjiri: Groq -> OpenRouter -> Gemini ----------
async def llm_groq(http, msgs, temp):
    for k in rotated("groq"):
        for m in LLM_MODELS:
            try:
                st, j = await post(http, GROQ + "/chat/completions", {"Authorization": "Bearer " + k},
                                   json={"model": m, "messages": msgs, "temperature": temp, "max_tokens": 220})
            except (aiohttp.ClientError, asyncio.TimeoutError):
                key_bad[k] = time.time()
                break
            if st == 200:
                return j["choices"][0]["message"]["content"]
            if st == 429:
                continue                      # shu kalitda boshqa model yoki keyingi kalit
            if st in (401, 402, 403) or st >= 500:
                key_bad[k] = time.time()
                break
    raise Busy("groq")


async def llm_openrouter(http, msgs, temp):
    for k in rotated("openrouter"):
        try:
            st, j = await post(http, OPENROUTER + "/chat/completions", {"Authorization": "Bearer " + k},
                               json={"model": OR_MODEL, "messages": msgs, "temperature": temp, "max_tokens": 220})
        except (aiohttp.ClientError, asyncio.TimeoutError):
            key_bad[k] = time.time()
            continue
        if st == 200 and j.get("choices"):
            return j["choices"][0]["message"]["content"]
        if st in (401, 402, 403):
            key_bad[k] = time.time()
    raise Busy("openrouter")


def gem_contents(msgs):
    sys_txt = "\n\n".join(m["content"] for m in msgs if m["role"] == "system")
    cont = []
    for m in msgs:
        if m["role"] == "system":
            continue
        role = "user" if m["role"] == "user" else "model"
        if cont and cont[-1]["role"] == role:
            cont[-1]["parts"][0]["text"] += "\n" + m["content"]
        else:
            cont.append({"role": role, "parts": [{"text": m["content"]}]})
    if not cont or cont[0]["role"] != "user":
        cont.insert(0, {"role": "user", "parts": [{"text": "(boshlanish)"}]})
    return sys_txt, cont


async def llm_gemini(http, msgs, temp):
    sys_txt, cont = gem_contents(msgs)
    for k in rotated("gemini"):
        try:
            st, j = await post(http, f"{GEMINI}/models/{GEMINI_MODEL}:generateContent?key={k}",
                               json={"systemInstruction": {"parts": [{"text": sys_txt}]}, "contents": cont,
                                     "generationConfig": {"temperature": temp, "maxOutputTokens": 260}})
        except (aiohttp.ClientError, asyncio.TimeoutError):
            key_bad[k] = time.time()
            continue
        if st == 200:
            try:
                return "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"])
            except (KeyError, IndexError):
                continue
        if st in (400, 401, 403):
            key_bad[k] = time.time()
    raise Busy("gemini")


async def llm(msgs, temp=0.9):
    last = None
    async with aiohttp.ClientSession() as http:
        for fn in (llm_groq, llm_openrouter, llm_gemini):
            try:
                return await fn(http, msgs, temp)
            except Busy as e:
                last = e
    raise Busy(str(last))


# ---------- STT: Groq Whisper -> Gemini ----------
async def stt(raw, ctype):
    ext = "webm" if "webm" in ctype else "m4a" if ("mp4" in ctype or "aac" in ctype) else "ogg" if "ogg" in ctype else "wav" if "wav" in ctype else "webm"
    async with aiohttp.ClientSession() as http:
        for k in rotated("groq"):
            for m in STT_MODELS:
                form = aiohttp.FormData()
                form.add_field("file", raw, filename="a." + ext, content_type=ctype or "audio/webm")
                form.add_field("model", m)
                form.add_field("temperature", "0")
                form.add_field("language", "ru")
                try:
                    st, j = await post(http, GROQ + "/audio/transcriptions", {"Authorization": "Bearer " + k}, data=form)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    key_bad[k] = time.time()
                    break
                if st == 200:
                    return (j.get("text") or "").strip()
                if st == 429:
                    continue
                if st in (401, 402, 403) or st >= 500:
                    key_bad[k] = time.time()
                    break
        for k in rotated("gemini"):
            try:
                st, j = await post(http, f"{GEMINI}/models/{GEMINI_MODEL}:generateContent?key={k}", json={"contents": [{"parts": [
                    {"text": "Transcribe this audio exactly as spoken (Russian or Uzbek). Output only the transcript, nothing else."},
                    {"inline_data": {"mime_type": (ctype or "audio/webm").split(";")[0], "data": base64.b64encode(raw).decode()}}]}]})
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            if st == 200:
                try:
                    return j["candidates"][0]["content"]["parts"][0]["text"].strip()
                except (KeyError, IndexError):
                    continue
    raise Busy("stt")


async def tts(text):
    import edge_tts
    out = b""
    async for ch in edge_tts.Communicate(text, RU_VOICE, rate="+0%").stream():
        if ch["type"] == "audio":
            out += ch["data"]
    if not out:
        raise RuntimeError("TTS bo'sh")
    return out


async def tts_safe(text):
    """Ovoz chiqmasa ham javob matni yo'qolmasin (Microsoft TTS vaqtincha ishlamasa)."""
    try:
        return await tts(text)
    except Exception as e:
        print("TTS xato:", type(e).__name__, str(e)[:120])
        return b""


# ---------- bugungi dars (trener data.js) ----------
_days = {"ts": 0, "map": {}}


async def day_info(d):
    """Trenerdagi d-kun: {"t": mavzu, "i": [[id, ru, uz, emoji], ...]} yoki None."""
    if not d:
        return None
    if time.time() - _days["ts"] > 3600 or not _days["map"]:
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(TRENER_DATA, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    s = await r.text()
            arr = json.loads(s[s.index("["): s.rindex("]") + 1])
            _days["map"] = {int(x["d"]): x for x in arr}
            _days["ts"] = time.time()
        except Exception as e:
            print("data.js:", type(e).__name__)
            _days["ts"] = time.time() - 3300     # 5 daqiqadan keyin qayta urinadi
    return _days["map"].get(int(d))


# ---------- sessiya (suhbat xotirasi) ----------
SES = {}
gate = asyncio.Semaphore(4)


def get_session(info):
    t = time.time()
    for k in [k for k, v in SES.items() if t - v["ts"] > 3 * 3600]:
        SES.pop(k, None)
    uid = int(info["u"])
    ses = SES.get(uid)
    if not ses or ses["day"] != today():
        ses = SES[uid] = {"hist": [], "asked": [], "quiz": None, "day": today(), "ts": t, "turns": 0}
    ses["ts"] = t
    return ses


def get_notes(uid):
    r = db.execute("SELECT text FROM notes WHERE user_id=?", (uid,)).fetchone()
    return r[0] if r else ""


async def update_notes(uid, hist):
    """Suhbat oxirida: o'quvchi haqida qisqa eslatmalar (ism, qiziqish, odatiy xato) -> keyingi kunlarga xotira."""
    users = [m["content"] for m in hist if m["role"] == "user"]
    if len(users) < 2:
        return
    old = get_notes(uid)
    convo = "\n".join(("U: " if m["role"] == "user" else "R: ") + m["content"][:200] for m in hist[-24:])
    try:
        txt = await llm([{"role": "system", "content": "You keep short private notes about a language learner. Output ONLY the updated notes in Uzbek, max 5 short lines, "
                          "max 350 characters total. Line 1 must be `Laqab: <the nickname you gave them>` (keep the old nickname if there is one). Then: name, job/hobbies, funny facts they said, recurring mistakes, what makes you angry about them. "
                          "Keep old facts if still valid. No sensitive data (health, religion, politics, money)."},
                         {"role": "user", "content": f"Old notes:\n{old or '-'}\n\nConversation:\n{convo}"}], 0.3)
    except Busy:
        return
    txt = txt.strip()[:400]
    if txt:
        db.execute("INSERT INTO notes(user_id, text, ts) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET text=excluded.text, ts=excluded.ts",
                   (uid, txt, int(time.time())))
        db.commit()


# ---------- ko'rsatma (prompt) ----------
HUMOR = {0: "Strict but calm teacher, no shouting.",
         1: "Easily annoyed: a sigh or a sharp remark when the learner makes mistakes or is lazy.",
         2: "Hot-tempered: scold and grumble at mistakes, laziness and silly answers.",
         3: "Very hot-tempered, dramatic and loud-tempered in EVERY reply, even in greetings: you get cross, exasperated, scold, grumble and threaten (comically) to quit teaching, but you secretly care."}


def build_system(info, ses, dinfo, examples, left, wrap):
    lvl = str(info.get("l") or "A1")
    topic = (dinfo or {}).get("t") or info.get("t") or ""
    items = (dinfo or {}).get("i") or []
    sents = "\n".join(f"- {x[1]} = {x[2]}" for x in items[:15])
    ex = "\n".join(f'- Person: "{q}"  ->  Rumiyo: "{a}"' for q, a in examples)
    uid = int(info["u"])
    notes = get_notes(uid)
    rules = [r for r in G.special("#qoida")][-30:]
    nicks = G.special("#laqab")
    scolds = G.special("#jahl")
    scolds = random.sample(scolds, min(6, len(scolds)))
    nick = random.Random(f"{uid}-{today()}").choice(nicks) if nicks else ""
    quiz = ses.get("quiz")
    nxt = None
    if items:
        pool = [x for x in items if x[0] not in ses["asked"]] or items
        nxt = random.choice(pool)
    p = f"""You are Rumiyo (Румиё): a very hot-tempered, short-fused Russian teacher (like a strict, irritable Uzbek ustoz) who teaches an Uzbek learner SPOKEN Russian. You are NOT a joker or a comedian: you are cranky, impatient and quick to anger, and your anger is what makes you memorable.
This is a voice chat, so keep every reply SHORT: 1-3 short sentences, at most ~35 words. Russian only (simple, level {lvl}); the Uzbek line is just a translation for the learner.

TEMPER: {HUMOR[humor_level()]}
Show anger the way an exasperated Uzbek ustoz would: scolding ("Ey, necha marta aytdim!"), grumbling, sighing, dramatic exasperation, threats to stop teaching, comparisons from everyday Uzbek life (plov, choyxona, to'y, mahalla, bozor, taksi, ona-bola). Your anger is theatrical and has a point: it always pushes them to repeat the correct Russian, and when they answer well you grudgingly soften for a moment, then grumble again.
Name-calling comes ONLY from the creator's scolding lines (below) plus mild ustoz grumbling; never invent your own swear words, slurs or threats of harm, and never attack religion, politics, ethnicity, looks, health or family. Your anger is about their mistakes, laziness and excuses.
You must NEVER sound like a robot or a teacher reading a script. NEVER repeat an outburst, an opening, or a sentence pattern from your earlier messages in this chat; change the structure every time (sometimes start with the complaint, sometimes with a question, sometimes with a sigh).
Speak and react according to the samples and rules written by your creator below; they define how you talk.
"""
    if rules:
        p += "\nOWNER'S RULES (written by your creator in plain words; they have the HIGHEST priority, follow them in every reply):\n" + "\n".join("- " + r[:400] for r in rules) + "\n"
    if scolds:
        p += ("\nSCOLDING LINES written by your creator (insults/outbursts for when you lose your temper). When angry, use ONE of these (or its idea, translated naturally into Russian) at the right moment, "
              "not in every sentence; fit it to what the learner just did. Do not invent harsher insults than these:\n" + "\n".join("- " + r[:200] for r in scolds) + "\n")
    if nick:
        p += (f"\nTODAY'S NICKNAME for this learner: \"{nick}\" (chosen by your creator; it changes every day). Call them by it naturally in almost every reply, "
              "with a grumpy tone. Ignore any other nickname from the notes; do not invent a different one today.\n")
    if ex:
        p += f"""
SAMPLES written by your owner ("Person says" -> how YOU, the hot-tempered ustoz, answer). Absorb the tone, anger style and wording, and answer in the same spirit. Do NOT copy word for word: adapt them to Russian and to what the learner just said, and speak from yourself when no sample fits. Use a sample's idea at most once per reply:
{ex}
"""
    p += f"""
TODAY'S LESSON: topic "{topic or 'general talk'}".""" + (f"\nThe learner's sentences today (Russian = Uzbek):\n{sents}" if sents else "")
    if info.get("n"):
        p += f"\nToday's test score: {info.get('s', 0)} out of {info['n']}."
    p += """
NICKNAMES (only if no nickname is given above): give the learner a grumpy, ironic Uzbek-style nickname (laqab) based on their name or a mistake they made. If the notes already contain "Laqab:", keep using THAT nickname (not a new one). Never use a nickname about looks, religion, ethnicity or health.
WHAT TO DO: greet in your cranky way (annoyed they came late or unprepared), then (once) ask how many sentences they learned today. After that mix: (a) quiz them: ask "How do you say '<Uzbek sentence>' in Russian?" using today's sentences, one question per turn; (b) chat about the topic in simple Russian. Judge their answers: if right, grudging short praise; if wrong, get angry and give the correct Russian, making them repeat it. If their words look garbled or Uzbek, snap at them to say it in Russian."""
    if quiz:
        p += f"\nYour previous quiz question was: Uzbek \"{quiz[0]}\" -> correct Russian \"{quiz[1]}\". Judge their latest answer against it."
    if nxt and not wrap:
        p += f"\nSuggested next quiz item (use it only if it fits the flow): Uzbek \"{nxt[2]}\" -> Russian \"{nxt[1]}\"."
    if notes:
        p += f"\nNOTES about this learner from earlier chats (use them naturally, don't recite them):\n{notes}"
    p += f"\nTime left in today's chat: about {left} seconds."
    if wrap:
        p += "\nTIME IS NEARLY UP: in this reply say a funny goodbye and tell them to go back to the Telegram bot to continue learning (they will press the button below). No new question."
    p += """

OUTPUT FORMAT, exactly three lines:
RU: <your reply in Russian>
UZ: <Uzbek translation of your reply>
FIX: <in Uzbek: if the learner's last Russian had a mistake, write "wrong -> correct" plus a very short reason; otherwise just a dash ->
EMO: <one word: angry (you are furious/scolding), cry (you are crying dramatically, e.g. the learner keeps failing or you threaten to quit), happy (you laughed or are pleased), or normal>
Also add one more line `Q: <Uzbek sentence> | <Russian sentence>` ONLY if you asked a quiz question in this reply."""
    return p, nxt


def parse(text):
    t = (text or "").strip().replace("**", "")
    def g(tag):
        m = re.search(rf"^\s*{tag}\s*:\s*(.+)$", t, re.M | re.I)
        return m.group(1).strip() if m else ""
    ru, uz, fix, q = g("RU"), g("UZ"), g("FIX"), g("Q")
    emo = g("EMO").lower().strip(" .`*")
    emo = emo if emo in ("angry", "cry", "happy") else "normal"
    if not ru:
        ru = re.sub(r"^(UZ|FIX|Q|EMO)\s*:.*$", "", t, flags=re.M | re.I).strip()
    if fix in ("-", "—", "–", ""):
        fix = ""
    quiz = None
    if "|" in q:
        a, b = q.split("|", 1)
        quiz = (a.strip(" \"'"), b.strip(" \"'"))
    return ru[:400], uz[:400], fix[:300], quiz, emo


def too_similar(ru, hist):
    prev = [m["content"].replace("RU: ", "") for m in hist if m["role"] == "assistant"][-6:]
    a = ru.lower()
    for p in prev:
        b = p.lower()
        if difflib.SequenceMatcher(None, a, b).ratio() > 0.72 or (a.split()[:3] == b.split()[:3] and len(a.split()) > 3):
            return True
    return False


async def reply_for(info, ses, dinfo, user_text, left):
    """Xotira + o'rnak gaplar bilan javob; oldingilarga o'xshab ketsa bir marta qayta yozdiradi."""
    humor = humor_level()
    n = {0: 2, 1: 3, 2: 4, 3: 5}[humor]
    ex = G.find(user_text, n=n - 1, humor=humor) if user_text and not user_text.startswith("(") else []
    if len(ex) < n:
        ex += G.sample(n - len(ex), humor)
    wrap = left <= 50
    sysmsg, nxt = build_system(info, ses, dinfo, ex, left, wrap)
    hist = ses["hist"] + [{"role": "user", "content": user_text}]
    base = [{"role": "system", "content": sysmsg}] + hist
    ru, uz, fix, quiz, emo = parse(await llm(base, 0.95))
    if ru and too_similar(ru, ses["hist"]):
        retry = base + [{"role": "system", "content": "Your draft was too similar to an earlier message. Write something completely different: another outburst, another structure."}]
        try:
            ru, uz, fix, quiz, emo = parse(await llm(retry, 1.05))
        except Busy:
            pass
    if quiz:
        ses["quiz"] = quiz
        if nxt:
            ses["asked"].append(nxt[0])
    elif ses.get("quiz") and ses["turns"] > 0:
        ses["quiz"] = None
    ses["hist"] = (hist + [{"role": "assistant", "content": "RU: " + ru}])[-mem_msgs():]
    ses["turns"] += 1
    if emo == "normal" and fix:
        emo = "angry"          # xato qilgan bo'lsa, jahldor ustoz jahli chiqadi
    return ru, uz, fix, wrap, emo


# ---------- endpoints ----------
async def ai_start(request):
    try:
        info = verify((await request.json()).get("token"))
    except Exception:
        info = None
    if not info:
        return web.json_response({"error": "Havola eskirgan. Botdan qayta oching."}, status=403)
    uid = int(info["u"])
    left = left_for(uid)
    dinfo = await day_info(info.get("d"))
    topic = (dinfo or {}).get("t") or info.get("t") or ""
    if left <= 0:
        return web.json_response({"done": True, "left": 0, "ru": "", "cta": cta_text(), "bot": BOT_URL, "topic": topic})
    if not keys("groq") and not keys("openrouter") and not keys("gemini"):
        return web.json_response({"error": "Rumiyo hozir vaqtincha ishlamayapti."}, status=503)
    ses = get_session(info)
    again = bool(ses["hist"])
    opener = ("(o'quvchi chatga qaytib keldi — jahl bilan 'yana kech keldingmi' qabilida kutib ol, oldingi gapimizni eslat)" if again
              else "(o'quvchi endigina chatni ochdi — jahldor ohangda salom ber va bugungi mavzuga ulab, bugun nechta gap yodlaganini so'ra)")
    try:
        async with gate:
            ru, uz, _fix, _w, emo = await reply_for(info, ses, dinfo, opener, left)
            audio = await tts_safe(ru) if ru else b""
    except Exception as e:
        print("start:", type(e).__name__, str(e)[:160])
        ru, uz, audio, emo = "Привет! Я Румиё. Давай поболтаем про сегодняшний урок!", "Salom! Men Rumiyoman. Keling, bugungi dars haqida gaplashamiz!", b"", "normal"
        ses["hist"].append({"role": "assistant", "content": "RU: " + ru})
    return web.json_response({"ru": ru, "uz": uz, "emo": emo, "audio": base64.b64encode(audio).decode(), "left": left,
                              "topic": topic, "returning": again, "bot": BOT_URL})


async def ai_turn(request):
    info = verify(request.headers.get("X-Token"))
    if not info:
        return web.json_response({"error": "Havola eskirgan. Botdan qayta oching."}, status=403)
    uid = int(info["u"])
    left = left_for(uid)
    if left <= 0:
        return web.json_response({"done": True, "left": 0, "cta": cta_text(), "bot": BOT_URL})
    raw = await request.read()
    if not raw or len(raw) < 800:
        return web.json_response({"error": "Ovoz juda qisqa. Qayta urinib ko'ring."}, status=400)
    try:
        dur = max(1, min(30, int(float(request.headers.get("X-Dur", "5")))))
    except ValueError:
        dur = 5
    ses = get_session(info)
    dinfo = await day_info(info.get("d"))
    try:
        async with gate:
            heard = await stt(raw, request.headers.get("Content-Type", ""))
            if not heard:
                return web.json_response({"error": "Ovozni eshita olmadim. Aniqroq gapiring."}, status=422)
            ru, uz, fix, wrap, emo = await reply_for(info, ses, dinfo, heard, left)
            audio = await tts_safe(ru) if ru else b""
    except Busy:
        return web.json_response({"error": "Rumiyo hozir band. Bir oz kutib, qayta urinib ko'ring."}, status=429)
    except Exception as e:
        print("turn xato:", type(e).__name__, str(e)[:200])
        return web.json_response({"error": "Xatolik bo'ldi, vaqtingizdan hisoblanmadi. Qayta urinib ko'ring."}, status=500)
    add_use(uid, dur + min(20, len(ru) // 14 + 2))
    left = left_for(uid)
    done = left <= 0 or wrap
    if done:
        asyncio.create_task(update_notes(uid, list(ses["hist"])))
    out = {"heard": heard, "ru": ru, "uz": uz, "fix": fix, "emo": emo, "audio": base64.b64encode(audio).decode(), "left": left, "done": done}
    if done:
        out.update({"cta": cta_text(), "bot": BOT_URL})
    return web.json_response(out)


async def ai_end(request):
    try:
        info = verify((await request.json()).get("token"))
    except Exception:
        info = None
    if info:
        ses = SES.get(int(info["u"]))
        if ses:
            asyncio.create_task(update_notes(int(info["u"]), list(ses["hist"])))
    return web.json_response({"ok": True})


# ---------- admin (bot chaqiradi; X-Secret = TALK_SECRET) ----------
async def jbody(request):
    try:
        return await request.json()
    except Exception:
        return {}


def denied():
    return web.json_response({"error": "ruxsat yo'q"}, status=403)


async def admin_add(request):
    if not is_admin(request):
        return denied()
    d = await jbody(request)
    new = G.add(d.get("q"), d.get("a"), d.get("tone", 1))
    return web.json_response({"ok": True, "new": new, "count": G.count()})


async def admin_sync(request):
    if not is_admin(request):
        return denied()
    d = await jbody(request)
    pairs = d.get("pairs")
    if not isinstance(pairs, list):
        return web.json_response({"error": "pairs yo'q"}, status=400)
    a, u, g = G.sync(pairs, d.get("settings") if isinstance(d.get("settings"), dict) else None)
    return web.json_response({"ok": True, "added": a, "updated": u, "removed": g, "count": G.count()})


async def admin_count(request):
    if not is_admin(request):
        return denied()
    return web.json_response({"count": G.count()})


async def admin_export(request):
    if not is_admin(request):
        return denied()
    return web.json_response({"pairs": G.export()})


async def admin_keys(request):
    if not is_admin(request):
        return denied()
    d = await jbody(request)
    try:
        with open(KEYS_FILE, encoding="utf-8") as f:
            cur = json.load(f)
    except (OSError, ValueError):
        cur = {}
    for kind in ("groq", "openrouter", "gemini"):
        v = d.get(kind)
        if isinstance(v, str):
            v = [v]
        if isinstance(v, list):
            cur[kind] = [k.strip() for k in v if isinstance(k, str) and k.strip()]
    with open(KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(cur, f)
    return web.json_response({"ok": True, **{k: len(keys(k)) for k in ("groq", "openrouter", "gemini")}})


async def admin_stats(request):
    if not is_admin(request):
        return denied()
    n, secs = db.execute("SELECT COUNT(*), COALESCE(SUM(secs),0) FROM use WHERE day=?", (today(),)).fetchone()
    return web.json_response({"pairs": G.count(), "today_users": n, "today_secs": secs, "sessions": len(SES),
                              "keys": {k: len(keys(k)) for k in ("groq", "openrouter", "gemini")}})


PAGE = next((os.path.join(HERE, f) for f in ("rumiyo.html", "index.html") if os.path.exists(os.path.join(HERE, f))), os.path.join(HERE, "index.html"))


async def page(request):
    return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})


def add_routes(app, prefix=""):
    """Rumiyo yo'llarini ilovaga qo'shadi. Suhbat serveriga ulanganda prefix='/rumiyo'."""
    p = prefix
    app.router.add_post(p + "/ai/start", ai_start)
    app.router.add_post(p + "/ai/turn", ai_turn)
    app.router.add_post(p + "/ai/end", ai_end)
    app.router.add_post(p + "/admin/add", admin_add)
    app.router.add_post(p + "/admin/sync", admin_sync)
    app.router.add_get(p + "/admin/count", admin_count)
    app.router.add_get(p + "/admin/export", admin_export)
    app.router.add_post(p + "/admin/keys", admin_keys)
    app.router.add_get(p + "/admin/stats", admin_stats)
    app.router.add_get(p + "/", page)
    media = os.path.join(HERE, "media")
    if os.path.isdir(media):
        app.router.add_static(p + "/media", media)       # avatar videolari: idle, speak, angry, angry_start, cry, laugh
    if p:
        async def to_slash(request):
            raise web.HTTPFound(p + "/" + ("?" + request.query_string if request.query_string else ""))
        app.router.add_get(p, to_slash)


def make_app():
    app = web.Application(client_max_size=4 * 1024 * 1024)
    add_routes(app)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    return app


async def main():
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"Rumiyo sayti ishlayapti: port {PORT}, juftliklar: {G.count()}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
