"""OneXBet Mini App backend. Подключение к боту (aiogram 3) в README.txt.
Всё считает сервер: баланс, иксы, исходы игр. Клиент только рисует."""
import asyncio, hashlib, hmac, json, math, os, secrets, sqlite3, time, uuid
from pathlib import Path
from urllib.parse import parse_qsl
from aiohttp import web, WSMsgType
from aiogram import Router, F, Bot
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery

# ====== НАСТРОЙКИ (впиши свои) ======
OWNER_ID = int(os.getenv("OWNER_ID", "0"))          # <-- твой id из start.py
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "")    # <-- твой @юзернейм без @
DB_PATH = os.getenv("MINIAPP_DB", "miniapp.db")
EDGE = 0.05            # маржа казино (5%)
MIN_BET, MAX_BET = 1, 10000
MINES_MAX_X = 30.0     # потолок икса в минах
UPG_MIN_X, UPG_MAX_X = 1.2, 100.0
CRASH_SPEED = 0.11     # m(t)=exp(SPEED*t), t в секундах
BETTING_SEC, CRASHED_SEC = 7, 4
STATIC = Path(__file__).parent / "static"

rnd = secrets.SystemRandom()
db = sqlite3.connect(DB_PATH, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT, balance INTEGER DEFAULT 0,
  wagered INTEGER DEFAULT 0, won INTEGER DEFAULT 0, deposited INTEGER DEFAULT 0, banned INTEGER DEFAULT 0, created INTEGER);
CREATE TABLE IF NOT EXISTS deposits(charge_id TEXT PRIMARY KEY, uid INTEGER, amount INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS mines(uid INTEGER PRIMARY KEY, bet INTEGER, cnt INTEGER, pos TEXT, opened TEXT);
CREATE TABLE IF NOT EXISTS crash_pending(uid INTEGER PRIMARY KEY, bet INTEGER);
""")
# рестарт посреди раунда краша: возвращаем ставки
for r in db.execute("SELECT * FROM crash_pending").fetchall():
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (r["bet"], r["uid"]))
db.execute("DELETE FROM crash_pending")

def user(uid): return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
def add_bal(uid, d): db.execute("UPDATE users SET balance=balance+? WHERE id=?", (d, uid))
def stat(uid, bet=0, payout=0):
    db.execute("UPDATE users SET wagered=wagered+?, won=won+? WHERE id=?", (bet, max(0, payout - bet), uid))

# ====== initData ======
def check_init(init_data: str, token: str, max_age=86400):
    d = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    got = d.pop("hash")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    sec = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest(), got): raise ValueError
    if time.time() - int(d["auth_date"]) > max_age: raise ValueError
    return json.loads(d["user"])

def auth(request, init_data=None):
    try:
        u = check_init(init_data or request.headers.get("X-Init", ""), request.app["bot"].token)
    except Exception:
        raise web.HTTPUnauthorized(text="bad initData")
    uid = u["id"]
    name = (u.get("first_name") or "Игрок")[:24]
    if not user(uid):
        db.execute("INSERT INTO users(id,name,username,created) VALUES(?,?,?,?)", (uid, name, u.get("username"), int(time.time())))
    else:
        db.execute("UPDATE users SET name=?, username=? WHERE id=?", (name, u.get("username"), uid))
    row = user(uid)
    if row["banned"]: raise web.HTTPForbidden(text="banned")
    return row

def is_owner(row):
    return OWNER_ID and row["id"] == OWNER_ID or (OWNER_USERNAME and (row["username"] or "").lower() == OWNER_USERNAME.lower().lstrip("@"))

def jr(**k): return web.json_response(k)
def err(msg, code=400): return web.json_response({"error": msg}, status=code)
def bet_of(data):
    try: b = int(data.get("bet"))
    except Exception: return None
    return b if MIN_BET <= b <= MAX_BET else None

# ====== Апгрейд ======
async def upgrade(request):
    u = auth(request); data = await request.json()
    b = bet_of(data)
    try: x = round(float(data.get("x")), 2)
    except Exception: x = 0
    if not b: return err(f"Ставка {MIN_BET}-{MAX_BET}")
    if not UPG_MIN_X <= x <= UPG_MAX_X: return err(f"Икс {UPG_MIN_X}-{UPG_MAX_X}")
    if u["balance"] < b: return err("Недостаточно звёзд")
    chance = (1 - EDGE) / x
    roll = rnd.random()
    win = roll < chance
    payout = int(b * x) if win else 0
    add_bal(u["id"], payout - b); stat(u["id"], b, payout)
    return jr(win=win, chance=round(chance * 100, 2), roll=roll, payout=payout, balance=user(u["id"])["balance"])

# ====== Мины ======
def mines_mult(k, picks):
    m = 1.0
    for i in range(picks): m *= (25 - i) / (25 - k - i)
    return round(min(m * (1 - EDGE), MINES_MAX_X), 2) if picks else 1.0

async def mines_start(request):
    u = auth(request); data = await request.json()
    b = bet_of(data)
    try: k = int(data.get("mines"))
    except Exception: k = 0
    if not b or not 1 <= k <= 8: return err("Неверные параметры")
    if db.execute("SELECT 1 FROM mines WHERE uid=?", (u["id"],)).fetchone(): return err("Игра уже идёт")
    if u["balance"] < b: return err("Недостаточно звёзд")
    add_bal(u["id"], -b)
    pos = rnd.sample(range(25), k)
    db.execute("INSERT INTO mines VALUES(?,?,?,?,?)", (u["id"], b, k, json.dumps(pos), "[]"))
    return jr(balance=user(u["id"])["balance"], mult=1.0, next=mines_mult(k, 1))

def mines_end(uid, g, payout):
    db.execute("DELETE FROM mines WHERE uid=?", (uid,))
    add_bal(uid, payout); stat(uid, g["bet"], payout)

async def mines_open(request):
    u = auth(request); data = await request.json()
    g = db.execute("SELECT * FROM mines WHERE uid=?", (u["id"],)).fetchone()
    if not g: return err("Нет активной игры")
    try: c = int(data.get("cell"))
    except Exception: return err("cell")
    pos, op = json.loads(g["pos"]), json.loads(g["opened"])
    if not 0 <= c < 25 or c in op: return err("Клетка недоступна")
    if c in pos:
        mines_end(u["id"], g, 0)
        return jr(boom=True, mines=pos, balance=user(u["id"])["balance"])
    op.append(c)
    m = mines_mult(g["cnt"], len(op))
    if m >= MINES_MAX_X or len(op) == 25 - g["cnt"]:
        pay = int(g["bet"] * m); mines_end(u["id"], g, pay)
        return jr(done=True, mult=m, payout=pay, mines=pos, opened=op, balance=user(u["id"])["balance"])
    db.execute("UPDATE mines SET opened=? WHERE uid=?", (json.dumps(op), u["id"]))
    return jr(mult=m, next=mines_mult(g["cnt"], len(op) + 1), opened=op)

async def mines_cash(request):
    u = auth(request)
    g = db.execute("SELECT * FROM mines WHERE uid=?", (u["id"],)).fetchone()
    op = json.loads(g["opened"]) if g else []
    if not g or not op: return err("Откройте хотя бы одну клетку")
    m = mines_mult(g["cnt"], len(op)); pay = int(g["bet"] * m)
    mines_end(u["id"], g, pay)
    return jr(payout=pay, mult=m, mines=json.loads(g["pos"]), opened=op, balance=user(u["id"])["balance"])

async def mines_state(request):
    u = auth(request)
    g = db.execute("SELECT * FROM mines WHERE uid=?", (u["id"],)).fetchone()
    if not g: return jr(active=False)
    op = json.loads(g["opened"])
    return jr(active=True, bet=g["bet"], cnt=g["cnt"], opened=op, mult=mines_mult(g["cnt"], len(op)), next=mines_mult(g["cnt"], len(op) + 1))

# ====== Краш (общий раунд для всех) ======
class Crash:
    def __init__(self):
        self.phase, self.rid, self.t0, self.until = "betting", 0, 0.0, time.time() + BETTING_SEC
        self.point, self.bets, self.hist, self.wss = 1.0, {}, [], {}

    @staticmethod
    def gen():  # P(x>=m) ~ 0.95/m  =>  >=40x примерно в 2.4% раундов
        r = rnd.random()
        return min(1000.0, max(1.0, math.floor(100 * (1 - EDGE) / (1 - r)) / 100))

    def mult(self):
        return min(self.point, math.exp(CRASH_SPEED * (time.time() - self.t0))) if self.phase == "flying" else (self.point if self.phase == "crashed" else 1.0)

    def snap(self, uid=None):
        s = {"phase": self.phase, "rid": self.rid, "hist": self.hist[-12:],
             "players": [{"name": b["name"], "bet": b["bet"], "x": b.get("x")} for b in self.bets.values()]}
        if self.phase == "betting": s["left"] = max(0, int((self.until - time.time()) * 1000))
        if self.phase == "flying": s["el"] = int((time.time() - self.t0) * 1000)
        if self.phase == "crashed": s["point"] = self.point
        b = self.bets.get(uid)
        if b: s["me"] = {"bet": b["bet"], "x": b.get("x"), "auto": b.get("auto")}
        return s

    async def push(self):
        for ws, uid in list(self.wss.items()):
            try: await ws.send_json(self.snap(uid))
            except Exception: self.wss.pop(ws, None)

    def cash(self, uid, x):
        b = self.bets.get(uid)
        if not b or b.get("x"): return 0
        b["x"] = x; pay = int(b["bet"] * x)
        db.execute("DELETE FROM crash_pending WHERE uid=?", (uid,))
        add_bal(uid, pay); stat(uid, b["bet"], pay)
        return pay

    async def loop(self):
        while True:
            self.phase, self.rid, self.bets = "betting", self.rid + 1, {}
            self.until = time.time() + BETTING_SEC; await self.push()
            await asyncio.sleep(BETTING_SEC)
            self.point = self.gen(); self.t0 = time.time(); self.phase = "flying"; await self.push()
            end = self.t0 + math.log(self.point) / CRASH_SPEED
            while time.time() < end:
                m = self.mult()
                changed = False
                for uid, b in self.bets.items():
                    if not b.get("x") and b.get("auto") and m >= b["auto"]:
                        self.cash(uid, b["auto"]); changed = True
                if changed: await self.push()
                await asyncio.sleep(0.05)
            self.phase = "crashed"
            for uid, b in self.bets.items():
                if not b.get("x"):
                    stat(uid, b["bet"], 0); db.execute("DELETE FROM crash_pending WHERE uid=?", (uid,))
            self.hist.append(self.point); await self.push()
            await asyncio.sleep(CRASHED_SEC)

crash = Crash()

async def crash_bet(request):
    u = auth(request); data = await request.json(); b = bet_of(data)
    if crash.phase != "betting": return err("Приём ставок закрыт")
    if not b or u["id"] in crash.bets: return err("Неверная ставка")
    if u["balance"] < b: return err("Недостаточно звёзд")
    try: auto = float(data.get("auto") or 0)
    except Exception: auto = 0
    add_bal(u["id"], -b); db.execute("INSERT OR REPLACE INTO crash_pending VALUES(?,?)", (u["id"], b))
    crash.bets[u["id"]] = {"bet": b, "name": u["name"], "auto": auto if auto >= 1.01 else None}
    await crash.push()
    return jr(balance=user(u["id"])["balance"])

async def crash_cash(request):
    u = auth(request)
    if crash.phase != "flying": return err("Поздно")
    m = crash.mult()
    if m >= crash.point: return err("Ракета уже взорвалась")
    m = math.floor(m * 100) / 100
    pay = crash.cash(u["id"], m)
    if not pay: return err("Нет ставки")
    await crash.push()
    return jr(payout=pay, x=m, balance=user(u["id"])["balance"])

async def crash_ws(request):
    ws = web.WebSocketResponse(heartbeat=20); await ws.prepare(request)
    uid = None
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try: uid = auth(request, json.loads(msg.data)["init"])["id"]
            except Exception: uid = None
            crash.wss[ws] = uid; await ws.send_json(crash.snap(uid))
    crash.wss.pop(ws, None)
    return ws

# ====== Профиль, топ, пополнение ======
async def me(request):
    u = auth(request)
    return jr(id=u["id"], name=u["name"], balance=u["balance"], admin=bool(is_owner(u)),
              limits=dict(min=MIN_BET, max=MAX_BET, edge=EDGE, ux=[UPG_MIN_X, UPG_MAX_X]))

async def top(request):
    auth(request)
    col = {"turnover": "wagered", "balance": "balance", "wins": "won"}.get(request.query.get("kind"), "wagered")
    rows = db.execute(f"SELECT name,{col} v FROM users WHERE {col}>0 ORDER BY {col} DESC LIMIT 20").fetchall()
    return jr(rows=[{"name": r["name"], "v": r["v"]} for r in rows])

async def invoice(request):
    u = auth(request); data = await request.json()
    try: amt = int(data.get("amount"))
    except Exception: amt = 0
    if not 1 <= amt <= 100000: return err("Сумма 1-100000")
    link = await request.app["bot"].create_invoice_link(
        title="Пополнение баланса", description=f"{amt} звёзд на игровой счёт",
        payload=f"dep:{u['id']}:{uuid.uuid4().hex[:8]}", currency="XTR", prices=[LabeledPrice(label=f"{amt} ⭐", amount=amt)])
    return jr(link=link)

# ====== Админка (только владелец) ======
def owner(request):
    u = auth(request)
    if not is_owner(u): raise web.HTTPForbidden(text="owner only")
    return u

async def adm_stats(request):
    owner(request)
    s = db.execute("SELECT COUNT(*) n, COALESCE(SUM(balance),0) bal, COALESCE(SUM(wagered),0) wag, COALESCE(SUM(deposited),0) dep FROM users").fetchone()
    return jr(users=s["n"], balances=s["bal"], wagered=s["wag"], deposited=s["dep"])

async def adm_find(request):
    owner(request); q = request.query.get("q", "").strip().lstrip("@")
    r = db.execute("SELECT * FROM users WHERE id=? OR lower(username)=lower(?) LIMIT 1", (int(q) if q.isdigit() else -1, q)).fetchone()
    return jr(user=dict(r) if r else None)

async def adm_act(request):
    owner(request); d = await request.json()
    try: uid, val = int(d["uid"]), int(d.get("value", 0))
    except Exception: return err("bad params")
    if not user(uid): return err("Нет такого юзера")
    a = d.get("action")
    if a == "add": add_bal(uid, val)
    elif a == "set": db.execute("UPDATE users SET balance=? WHERE id=?", (max(0, val), uid))
    elif a in ("ban", "unban"): db.execute("UPDATE users SET banned=? WHERE id=?", (a == "ban", uid))
    else: return err("action")
    return jr(user=dict(user(uid)))

# ====== Платежи (aiogram router) ======
router = Router()

@router.pre_checkout_query(F.invoice_payload.startswith("dep:"))
async def _pre(q: PreCheckoutQuery): await q.answer(ok=True)

@router.message(F.successful_payment, F.successful_payment.invoice_payload.startswith("dep:"))
async def _paid(m: Message):
    sp = m.successful_payment; uid = int(sp.invoice_payload.split(":")[1])
    if db.execute("SELECT 1 FROM deposits WHERE charge_id=?", (sp.telegram_payment_charge_id,)).fetchone(): return
    if not user(uid): db.execute("INSERT INTO users(id,name,created) VALUES(?,?,?)", (uid, m.from_user.first_name, int(time.time())))
    db.execute("INSERT INTO deposits VALUES(?,?,?,?)", (sp.telegram_payment_charge_id, uid, sp.total_amount, int(time.time())))
    add_bal(uid, sp.total_amount); db.execute("UPDATE users SET deposited=deposited+? WHERE id=?", (sp.total_amount, uid))
    await m.answer(f"✅ Баланс пополнен на {sp.total_amount} ⭐")

# ====== Запуск ======
async def start_web(bot: Bot, host="0.0.0.0", port=8080):
    app = web.Application(); app["bot"] = bot
    app.add_routes([
        web.get("/", lambda r: web.FileResponse(STATIC / "index.html")),
        web.get("/api/me", me), web.get("/api/top", top), web.post("/api/invoice", invoice),
        web.post("/api/upgrade", upgrade),
        web.get("/api/mines/state", mines_state), web.post("/api/mines/start", mines_start),
        web.post("/api/mines/open", mines_open), web.post("/api/mines/cash", mines_cash),
        web.post("/api/crash/bet", crash_bet), web.post("/api/crash/cash", crash_cash), web.get("/api/crash/ws", crash_ws),
        web.get("/api/admin/stats", adm_stats), web.get("/api/admin/find", adm_find), web.post("/api/admin/act", adm_act),
    ])
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, host, port).start()
    asyncio.create_task(crash.loop())
    return runner
