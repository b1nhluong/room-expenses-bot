import os, re, sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# === ENV ===
TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret123")

app = FastAPI()
application = Application.builder().token(TOKEN).build()

DB = "expenses.db"

# === DB helpers ===
def db():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys = ON")
    return con

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS expenses(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      amount_k INTEGER NOT NULL,   -- đơn vị 'k'
      note TEXT,
      ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS settings(
      chat_id INTEGER PRIMARY KEY,
      period_start TEXT            -- ISO timestamp
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS name_map(
      chat_id INTEGER NOT NULL,
      initial TEXT NOT NULL,       -- 'b','a','d' (lowercase)
      fullname TEXT NOT NULL,      -- 'Bình','An','Duy'
      PRIMARY KEY(chat_id, initial)
    )""")
    con.commit(); con.close()

def get_period_start(chat_id: int) -> datetime:
    con = db(); cur = con.cursor()
    cur.execute("SELECT period_start FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if row and row[0]:
        dt = datetime.fromisoformat(row[0])
    else:
        dt = datetime.now(timezone.utc)
        cur.execute("INSERT INTO settings(chat_id,period_start) VALUES(?,?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET period_start=excluded.period_start",
                    (chat_id, dt.isoformat()))
        con.commit()
    con.close()
    return dt

def set_period_start(chat_id: int, dt: datetime):
    con = db()
    con.execute("INSERT INTO settings(chat_id,period_start) VALUES(?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET period_start=excluded.period_start",
                (chat_id, dt.isoformat()))
    con.commit(); con.close()

def clear_expenses(chat_id: int):
    con = db(); con.execute("DELETE FROM expenses WHERE chat_id=?", (chat_id,))
    con.commit(); con.close()

def set_default_map(chat_id: int):
    con = db(); cur = con.cursor()
    cur.execute("SELECT COUNT(1) FROM name_map WHERE chat_id=?", (chat_id,))
    if cur.fetchone()[0] == 0:
        cur.executemany("INSERT INTO name_map(chat_id,initial,fullname) VALUES(?,?,?)", [
            (chat_id, "b", "Bình"),
            (chat_id, "a", "An"),
            (chat_id, "d", "Duy"),
        ])
        con.commit()
    con.close()

def get_fullname(chat_id: int, initial: str):
    con = db(); cur = con.cursor()
    cur.execute("SELECT fullname FROM name_map WHERE chat_id=? AND initial=?",
                (chat_id, initial.lower()))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None

def set_mapping(chat_id: int, pairs: list[tuple[str,str]]):
    con = db()
    for ini, full in pairs:
        con.execute("INSERT INTO name_map(chat_id,initial,fullname) VALUES(?,?,?) "
                    "ON CONFLICT(chat_id,initial) DO UPDATE SET fullname=excluded.fullname",
                    (chat_id, ini.lower(), full.strip()))
    con.commit(); con.close()

# === Parsers/formatters ===
ENTRY_RE = re.compile(r"^/([a-zA-Z])\s+(-?\d[\d.,]*)\s*(k|K)?\s*(.*)$")
def parse_entry(text: str):
    m = ENTRY_RE.match(text.strip())
    if not m: return None
    ini = m.group(1)                     # ký tự đầu
    num = m.group(2).replace(".","").replace(",","")
    note = (m.group(4) or "").strip()
    if not re.fullmatch(r"-?\d+", num): return None
    amount_k = int(num)                  # hiểu luôn là 'k'; "120k" -> 120; "120" -> 120
    return ini, amount_k, note

def fmt_k(nk: int): return f"{nk}k"
def fmt_date_dmy(dt: datetime): return dt.astimezone().strftime("%d-%m-%Y")

def settle_k(spent: dict[str,int], members: list[str]):
    n = len(members)
    total = sum(spent.get(m,0) for m in members)
    share = total / n if n else 0.0
    creditors, debtors = [], []
    for m in members:
        diff = spent.get(m,0) - share
        if diff > 0.5: creditors.append([m, diff])
        elif diff < -0.5: debtors.append([m, -diff])
    creditors.sort(key=lambda x:x[1], reverse=True)
    debtors.sort(key=lambda x:x[1], reverse=True)
    moves=[]; i=j=0
    while i<len(debtors) and j<len(creditors):
        dn,dneed = debtors[i]; cn,cget = creditors[j]
        pay = min(dneed, cget); pay_k = int(round(pay))
        moves.append((dn, cn, pay_k))
        dneed -= pay; cget -= pay
        if dneed <= 0.5: i += 1
        else: debtors[i] = [dn, dneed]
        if cget <= 0.5: j += 1
        else: creditors[j] = [cn, cget]
    return total, int(round(share)), moves

# === Handlers ===
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Cách dùng:\n"
        "/b 120 siêu thị\n/a 317 ăn\n/d 134 tiền điện\n\n"
        "/tongket → tổng kết kỳ hiện tại\n/batdau → reset kỳ mới\n"
        "/setmap b=Bình;a=An;d=Duy → đặt map chữ cái → tên"
    )

async def setmap_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(ctx.args)
    pairs=[]
    for part in raw.split(";"):
        if "=" in part:
            ini, full = part.split("=",1)
            ini, full = ini.strip(), full.strip()
            if len(ini)==1 and full: pairs.append((ini, full))
    if not pairs:
        await update.message.reply_text("Cú pháp: /setmap b=Bình;a=An;d=Duy")
        return
    set_mapping(update.effective_chat.id, pairs)
    await update.message.reply_text("✅ Đã cập nhật map")

async def entry_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Bắt mọi command KHÔNG thuộc {start, setmap, tongket, batdau}
    text = update.message.text or ""
    parsed = parse_entry(text)
    if not parsed: return
    ini, amount_k, note = parsed
    chat_id = update.effective_chat.id
    set_default_map(chat_id)
    fullname = get_fullname(chat_id, ini)
    if not fullname:
        await update.message.reply_text(f"Chưa biết '{ini}'. Dùng /setmap {ini}=Tên")
        return
    con = db()
    con.execute("INSERT INTO expenses(chat_id,name,amount_k,note) VALUES(?,?,?,?)",
                (chat_id, fullname, amount_k, note))
    con.commit(); con.close()
    await update.message.reply_text(
        f"đã ghi nhận: \"{fullname} chi {fmt_k(amount_k)} cho {note or '(không ghi chú)'}\""
    )

async def tongket_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_default_map(chat_id)
    start = get_period_start(chat_id)
    end = datetime.now(timezone.utc)
    con = db(); cur = con.cursor()
    cur.execute("""SELECT name, SUM(amount_k) FROM expenses
                   WHERE chat_id=? AND ts >= datetime(?)
                   GROUP BY name""", (chat_id, start.isoformat()))
    data = dict(cur.fetchall()); con.close()
    if not data:
        await update.message.reply_text(
            f"Chi tiêu từ ngày {fmt_date_dmy(start)} đến {fmt_date_dmy(end)}:\n- (chưa có khoản chi nào)"
        ); return
    con = db(); cur = con.cursor()
    cur.execute("SELECT fullname FROM name_map WHERE chat_id=?", (chat_id,))
    members = [r[0] for r in cur.fetchall()]; con.close()
    total, share, moves = settle_k(data, members)
    lines = [f"Chi tiêu từ ngày {fmt_date_dmy(start)} đến {fmt_date_dmy(end)}:"]
    for m in members:
        lines.append(f"- {m} đã chi tiêu tổng cộng: {fmt_k(data.get(m,0))}")
    if moves:
        lines += [f"{a} trả {b} {fmt_k(k)}" for a,b,k in moves]
    else:
        lines.append("✅ Đã cân bằng")
    await update.message.reply_text("\n".join(lines))

async def batdau_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    clear_expenses(chat_id)
    now = datetime.now(timezone.utc); set_period_start(chat_id, now)
    await update.message.reply_text(f"✅ Đã bắt đầu kỳ mới từ {fmt_date_dmy(now)}.")

# === Wire handlers ===
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("setmap", setmap_cmd))
application.add_handler(CommandHandler("tongket", tongket_cmd))
application.add_handler(CommandHandler("batdau", batdau_cmd))
# Mọi command khác (ví dụ /b, /a, /d) coi như entry chi tiêu
application.add_handler(
    MessageHandler(
        filters.COMMAND & ~filters.Regex(r"^/(start|setmap|tongket|batdau)\b"),
        entry_cmd
    )
)

# === FastAPI lifecycle & routes ===
@app.get("/")
async def root():
    return {"status": "ok"}

@app.on_event("startup")
async def on_startup():
    init_db()
    # Quan trọng: khởi động Application để xử lý update từ queue
    await application.initialize()
    await application.start()

@app.on_event("shutdown")
async def on_shutdown():
    await application.stop()
    await application.shutdown()

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    data = await request.json()
    await application.update_queue.put(Update.de_json(data, application.bot))
    return {"ok": True}
