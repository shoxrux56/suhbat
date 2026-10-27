"""Eng sodda ovozli suhbat: kirgan odam darhol navbatdagi boshqa odam bilan juftlanadi.
Tarjima yo'q, AI yo'q, limit yo'q. Server faqat 'signal' tashiydi, ovoz odamlar orasida to'g'ridan-to'g'ri ketadi."""
import asyncio, json, os
from aiohttp import web, WSMsgType

PORT = int(os.getenv("PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))


def ice_servers():
    ice = [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302", "stun:stun.cloudflare.com:3478"]}]
    urls = [u.strip() for u in os.getenv("TURN_URLS", "").split(",") if u.strip()]
    if urls:  # o'zingizning TURN serveringiz (tavsiya)
        ice.append({"urls": urls, "username": os.getenv("TURN_USER", ""), "credential": os.getenv("TURN_PASS", "")})
    else:  # ochiq sinov TURN (ishlamay qolishi mumkin; barqaror ishlashi uchun o'zingiznikini yozing)
        ice.append({"urls": ["turn:openrelay.metered.ca:80", "turn:openrelay.metered.ca:443",
                             "turns:openrelay.metered.ca:443?transport=tcp"],
                    "username": "openrelayproject", "credential": "openrelayproject"})
    return ice


class Peer:
    def __init__(self, ws):
        self.ws, self.partner = ws, None


peers, waiting = set(), []


async def send(p, obj):
    try:
        await p.ws.send_json(obj)
    except Exception:
        pass


async def unpair(p):
    q = p.partner
    if q:
        p.partner = q.partner = None
        await send(q, {"t": "left"})
    if p in waiting:
        waiting.remove(p)


async def join(p):
    await unpair(p)
    waiting.append(p)
    await match()
    if p in waiting:
        await send(p, {"t": "wait", "n": len(peers)})


async def match():
    while True:
        alive = [x for x in waiting if not x.ws.closed]
        waiting[:] = alive
        if len(waiting) < 2:
            return
        a, b = waiting.pop(0), waiting.pop(0)  # kimda kutgani uzun bo'lsa, o'sha taklif yuboradi
        a.partner, b.partner = b, a
        await send(a, {"t": "match", "init": True})
        await send(b, {"t": "match", "init": False})


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=64 * 1024)
    await ws.prepare(request)
    p = Peer(ws)
    peers.add(p)
    await send(p, {"t": "hello", "ice": ice_servers()})
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            t = d.get("t")
            if t == "join":
                await join(p)
            elif t in ("offer", "answer", "ice") and p.partner:
                await send(p.partner, {"t": t, "d": d.get("d")})
    except Exception as e:
        print("ws xato:", e)
    finally:
        peers.discard(p)
        await unpair(p)
    return ws


async def online_loop():
    while True:
        await asyncio.sleep(4)
        for p in list(waiting):
            await send(p, {"t": "wait", "n": len(peers)})


async def page(request):
    return web.FileResponse(os.path.join(HERE, "index.html"), headers={"Cache-Control": "no-cache"})


async def on_start(app):
    app["loop"] = asyncio.create_task(online_loop())


def make_app():
    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    app.on_startup.append(on_start)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
