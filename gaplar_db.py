"""Rumiyo gaplar bazasi (SQLite): "odam shu desa, Rumiyo shu desin" juftliklari.
Ishlatish:
  python gaplar_db.py import rumiyo_baza.json   # sahifadan yuklab olingan fayl -> baza (qayta ishlatsa ham ikkilanmaydi)
  python gaplar_db.py show                      # nechta juftlik bor
  python gaplar_db.py find "men bilmayman"      # sinab ko'rish: shu gapga qaysi javoblar mos keladi
Serverda:
  import gaplar_db as G
  G.find("men bilmayman", n=2, humor=1)  # -> [("Men bilmayman", "Hech qisi yo'q..."), ...]  (AI ga o'rnak qilib beriladi)
  G.setting("memorySec", 60)             # sahifadagi "AI qoidalari"dan qiymat
"""
import json, os, re, sqlite3, sys, time

DB_PATH = os.getenv("GAPLAR_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gaplar.db"))

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
db.execute("""CREATE TABLE IF NOT EXISTS juftlar(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    q TEXT NOT NULL,                 -- odam aytadigan gap
    a TEXT NOT NULL,                 -- Rumiyo javobi
    tone INTEGER NOT NULL DEFAULT 0, -- 1 = hazil
    used INTEGER NOT NULL DEFAULT 0,
    last_used INTEGER NOT NULL DEFAULT 0,
    UNIQUE(q, a))""")
db.execute("CREATE INDEX IF NOT EXISTS ix_juftlar_tone ON juftlar(tone, used, last_used)")
# Matn bo'yicha qidirish (indeks): ruscha va o'zbekcha so'zlar bo'yicha tez topadi
db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS juftlar_q USING fts5(q, tokenize='unicode61 remove_diacritics 2')")
db.execute("CREATE TABLE IF NOT EXISTS sozlamalar(k TEXT PRIMARY KEY, v TEXT)")
db.commit()


def _words(s):
    return [w for w in re.findall(r"\w+", (s or "").lower()) if len(w) > 1][:12]


def sync(pairs, settings=None):
    """To'liq tenglashtirish: ro'yxatdagi yangi juftlik qo'shiladi, ro'yxatda yo'g'i o'chadi, ishlatilish soni saqlanadi."""
    keep, add, upd = set(), 0, 0
    for p in pairs or []:
        q, a = str(p.get("q") or "").strip(), str(p.get("a") or "").strip()
        if not q or not a:
            continue
        tone = 1 if p.get("tone") else 0
        r = db.execute("SELECT id, tone FROM juftlar WHERE q=? AND a=?", (q, a)).fetchone()
        if r:
            if r[1] != tone:
                db.execute("UPDATE juftlar SET tone=? WHERE id=?", (tone, r[0]))
                upd += 1
            keep.add(r[0])
        else:
            keep.add(db.execute("INSERT INTO juftlar(q, a, tone) VALUES(?,?,?)", (q, a, tone)).lastrowid)
            add += 1
    gone = 0
    for (i,) in db.execute("SELECT id FROM juftlar").fetchall():
        if i not in keep:
            db.execute("DELETE FROM juftlar WHERE id=?", (i,))
            gone += 1
    db.execute("DELETE FROM juftlar_q")  # qidiruv indeksini qayta quramiz
    db.execute("INSERT INTO juftlar_q(rowid, q) SELECT id, q FROM juftlar")
    for k, v in (settings or {}).items():
        db.execute("INSERT INTO sozlamalar(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v, ensure_ascii=False)))
    db.commit()
    return add, upd, gone


def import_json(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return sync(d.get("pairs", []), d.get("settings"))


def add(q, a, tone=1):
    """Bitta juftlik qo'shadi (bor bo'lsa tegmaydi). True = yangi qo'shildi."""
    q, a = str(q or "").strip(), str(a or "").strip()
    if not q or not a:
        return False
    if db.execute("SELECT 1 FROM juftlar WHERE q=? AND a=?", (q, a)).fetchone():
        return False
    i = db.execute("INSERT INTO juftlar(q, a, tone) VALUES(?,?,?)", (q, a, 1 if tone else 0)).lastrowid
    db.execute("INSERT INTO juftlar_q(rowid, q) VALUES(?,?)", (i, q))
    db.commit()
    return True


def count():
    return db.execute("SELECT COUNT(*) FROM juftlar").fetchone()[0]


def export():
    return [{"q": q, "a": a, "tone": t} for q, a, t in db.execute("SELECT q, a, tone FROM juftlar ORDER BY id")]


def sample(n=3, humor=1):
    """Kam ishlatilgan tasodifiy n ta juftlik: salomlashish va o'z-o'zidan hazil uchun o'rnak."""
    rows = db.execute("SELECT id, q, a FROM juftlar WHERE q NOT LIKE '#%'" + (" AND tone=0" if humor <= 0 else "")
                      + " ORDER BY used + (abs(random()) % 4), last_used LIMIT ?", (max(1, int(n)),)).fetchall()
    now = int(time.time())
    for i, _, _ in rows:
        db.execute("UPDATE juftlar SET used=used+1, last_used=? WHERE id=?", (now, i))
    db.commit()
    return [(a, b) for _, a, b in rows]


def find(text, n=2, humor=1):
    """Odam gapiga eng mos n ta juftlik [(odam_gapi, javob), ...]. Mos topilmasa bo'sh ro'yxat: AI o'zi javob beradi.
    Bir xil mos kelsa, kamroq ishlatilgani oldin keladi. humor=0 bo'lsa hazil juftliklar olinmaydi."""
    ws = _words(text)
    if not ws:
        return []
    m = " OR ".join('"%s"' % w.replace('"', "") for w in ws)
    q = ("SELECT j.id, j.q, j.a FROM juftlar_q f JOIN juftlar j ON j.id=f.rowid WHERE juftlar_q MATCH ?"
         + " AND j.q NOT LIKE '#%'" + (" AND j.tone=0" if humor <= 0 else "") + " ORDER BY bm25(juftlar_q), j.used, j.last_used LIMIT 8")
    rows = db.execute(q, (m,)).fetchall()
    rows = rows[: max(1, int(n))]
    now = int(time.time())
    for i, _, _ in rows:
        db.execute("UPDATE juftlar SET used=used+1, last_used=? WHERE id=?", (now, i))
    db.commit()
    return [(a, b) for _, a, b in rows]


def special(kind):
    """Maxsus yozuvlar: kind='#qoida' (AI ga ko'rsatma) yoki '#laqab' (laqablar ro'yxati). Matnlar ro'yxati."""
    return [a for (a,) in db.execute("SELECT a FROM juftlar WHERE q=? ORDER BY id", (kind,))]


def setting(k, default=None):
    r = db.execute("SELECT v FROM sozlamalar WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def show():
    n, h = db.execute("SELECT COUNT(*), COALESCE(SUM(tone),0) FROM juftlar").fetchone()
    print(f"jami: {n} ta juftlik (hazil: {h})")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "import":
        a, u, g = import_json(sys.argv[2])
        print(f"Qo'shildi: {a}, yangilandi: {u}, o'chirildi: {g}")
        show()
    elif len(sys.argv) >= 3 and sys.argv[1] == "find":
        for q, a in find(" ".join(sys.argv[2:]), n=3):
            print(f"  odam: {q}\n  Rumiyo: {a}\n")
    elif len(sys.argv) >= 2 and sys.argv[1] == "show":
        show()
    else:
        print(__doc__)
