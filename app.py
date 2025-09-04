import os, re, sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret123")

app = FastAPI()
bot = Application.builder().token(TOKEN).build()

DB = "expenses.db"

# ---------- DB ----------
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
      amount_k INTEGER NOT NULL,
      note TEXT,
      ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS settings(
      chat_id INTEGER PRIMARY KEY,
      period_start TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS name_map(
      chat_id INTEGER NOT NULL,
      initial TEXT NOT NULL,
      fullname TEXT NOT NULL,
      PRIMARY KEY(chat_id, initial)
    )""")
    con.commit(); con.close()

def get_period_start(chat_id):
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

def set_period_start(chat_id, dt):
    con = db()
    con.execute("INSERT INTO settings(chat_id,period_start) VALUES(?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET period_start=excluded.period_start",
                (chat_id, dt.isoformat()))
    con.commit(); con.close()

def clear_expenses(chat_id):
    con = db()
    con.execute("DELETE FROM expenses WHERE chat_id=?", (chat_id,))
    con.commit(); con.close()

def set_default_map(chat_id):
    con = db(); cur = con.cursor()
    cur.execute("SELECT COUNT(1) FROM name_map WHERE chat_id=?", (chat_id,))
    if cur.fetchone()[0] == 0:
        cur.executemany("INSERT INTO name_map(chat_id,initial,fullname) VALUES(?,?,?)", [
            (chat_id,"b","Bình"),
            (chat_id,"a","An"),
            (chat_id,"d","Duy"),
        ])
        con.commit()
    con.close()

def get_fullname(chat_id, initial):
    con = db(); cur = con.cursor()
    cur.execute("SELECT fullname FROM name_map WHERE chat_id=? AND initial=?",
                (chat_id, initial.lower()))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None

def set_mapping(chat_id, pairs):
    con = db()
    for ini, full in pairs:
        con.execute("INSERT INTO name_map(chat_id,initial,fullname) VALUES(?,?,?) "
                    "ON CONFLICT(chat_id,initial) DO UPDATE SET fullname=excluded.fullname",
                    (chat_id, ini.lower(), full.strip()))
    con.commit(); con.close()

# ---------- Helpers ----------
ENTRY_RE = re.compile(r"^/([a-zA-Z])\s+(-?\d[\d.,]*)\s*(k|K)?\s*(.*)$")

def parse_entry(text):
    m = ENTRY_RE.match(text.strip())
    if not m: return None
    ini = m.group(1)
    num = m.group(2).replace(".","").replace(",","")
    note = (m.group(4) or "").strip()
    if not num.isdigit(): return None
    amt = int(num)  # hiểu luôn là 'k'
    return ini, amt, note

def fmt_k(n): return f"{n}k"
def fmt_date(dt): return dt.astimezone().strftime("%d-%m-%Y")

def settle(spent, members):
    n = len(members)
    total = sum(spent.get(m,0) for m in members)
    share = total / n if n else 0
    creditors, debtors = [], []
    for m in members:
        diff = spent.get(m,0) - share
        if diff > 0.5: creditors.append([m,diff])
        elif diff < -0.5: debtors.append([m,-diff])
    creditors.sort(key=lambda x:x[1], reverse=True)
    debtors.sort(key=lambda x:x[1], reverse=True)
    moves=[]; i=j=0
    while i<len(debtors) and j<len(creditors):
        dn,dneed=debtors[i]; cn,cget=creditors[j]
        pay=min(dneed,cget); pay_k=int(round(pay))
        moves.append((dn,cn,pay_k))
        dneed-=pay; cget-=pay
        if dneed<=0.5: i+=1
        else: debtors[i]=[dn,dneed]
        if cget<=0.5: j+=1
        else: creditors[j]=[cn,cget]
    return total,int(round(share)),moves

# ---------- Handlers ----------
async def start_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Nhập chi tiêu dạng:\n"
        "/b 120 siêu thị\n/a 317 ăn\n/d 134 tiền điện\n\n"
        "/tongket → tổng kết\n/batdau → reset kỳ mới\n/setmap b=Bình;a=An;d=Duy → đặt map"
    )

async def setmap_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    raw=" ".join(ctx.args)
    pairs=[]
    for part in raw.split(";"):
        if "=" in part:
            ini,full=part.split("=",1)
            pairs.append((ini.strip(), full.strip()))
    if not pairs:
        await update.message.reply_text("Cú pháp: /setmap b=Bình;a=An;d=Duy")
        return
    set_mapping(update.effective_chat.id,pairs)
    await update.message.reply_text("✅ Đã cập nhật map")

async def entry_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    text=update.message.text
    parsed=parse_entry(text)
    if not parsed: return
    ini,amt,note=parsed
    chat_id=update.effective_chat.id
    set_default_map(chat_id)
    fullname=get_fullname(chat_id,ini)
    if not fullname:
        await update.message.reply_text(f"Chưa biết '{ini}'. Dùng /setmap {ini}=Tên")
        return
    con=db()
    con.execute("INSERT INTO expenses(chat_id,name,amount_k,note) VALUES(?,?,?,?)",
                (chat_id,fullname,amt,note))
    con.commit(); con.close()
    await update.message.reply_text(
        f"đã ghi nhận: \"{fullname} chi {fmt_k(amt)} cho {note or '(không ghi chú)'}\""
    )

async def tongket_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    chat_id=update.effective_chat.id
    set_default_map(chat_id)
    start=get_period_start(chat_id)
    end=datetime.now(timezone.utc)
    con=db(); cur=con.cursor()
    cur.execute("SELECT name,SUM(amount_k) FROM expenses WHERE chat_id=? AND ts>=datetime(?) GROUP BY name",
                (chat_id,start.isoformat()))
    data=dict(cur.fetchall()); con.close()
    if not data:
        await update.message.reply_text("Chưa có khoản chi nào.")
        return
    con=db(); cur=con.cursor()
    cur.execute("SELECT fullname FROM name_map WHERE chat_id=?", (chat_id,))
    members=[r[0] for r in cur.fetchall()]; con.close()
    total,share,moves=settle(data,members)
    lines=[f"Chi tiêu từ {fmt_date(start)} đến {fmt_date(end)}:"]
    for m in members:
        lines.append(f"- {m} đã chi tiêu tổng cộng: {fmt_k(data.get(m,0))}")
    if moves:
        for a,b,k in moves:
            lines.append(f"{a} trả {b} {fmt_k(k)}")
    else:
        lines.append("✅ Đã cân bằng")
    await update.message.reply_text("\n".join(lines))

async def batdau_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    chat_id=update.effective_chat.id
    clear_expenses(chat_id)
    now=datetime.now(timezone.utc); set_period_start(chat_id,now)
    await update.message.reply_text(f"✅ Đã bắt đầu kỳ mới từ {fmt_date(now)}.")

# ---------- Wire ----------
bot.add_handler(CommandHandler("start", start_cmd))
bot.add_handler(CommandHandler("setmap", setmap_cmd))
bot.add_handler(CommandHandler("tongket", tongket_cmd))
bot.add_handler(CommandHandler("batdau", batdau_cmd))
# mọi command dạng /b, /a, /d → ghi chi tiêu
bot.add_handler(MessageHandler(filters.COMMAND, entry_cmd))

@app.on_event("startup")
async def on_startup(): init_db()

@app.post("/webhook/{secret}")
async def telegram_webhook(secret:str, request:Request):
    if secret!=WEBHOOK_SECRET: raise HTTPException(status_code=403)
    data=await request.json()
    await bot.update_queue.put(Update.de_json(data, bot.bot))
    return {"ok":True}
