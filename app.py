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
      name TEXT NOT NULL,            -- người trả (tên đầy đủ)
      amount_k INTEGER NOT NULL,     -- nghìn VND
      note TEXT,
      participants TEXT,             -- ví dụ "a,b" (chữ cái của người cùng chia)
      ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS settings(
      chat_id INTEGER PRIMARY KEY,
      period_start TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS name_map(
      chat_id INTEGER NOT NULL,
      initial TEXT NOT NULL,         -- 'b','a','d' (lowercase)
      fullname TEXT NOT NULL,        -- 'Bình','An','Duy'
      PRIMARY KEY(chat_id, initial)
    )""")
    # đảm bảo cột participants tồn tại (nâng cấp DB cũ)
    try:
        con.execute("ALTER TABLE expenses ADD COLUMN participants TEXT")
    except sqlite3.OperationalError:
        pass
    con.commit(); con.close()

def get_period_start(chat_id: int):
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
    con.close(); return dt

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
    row = cur.fetchone(); con.close()
    return row[0] if row else None

def list_members(chat_id: int):
    con = db(); cur = con.cursor()
    cur.execute("SELECT initial, fullname FROM name_map WHERE chat_id=?", (chat_id,))
    rows = cur.fetchall(); con.close()
    return {ini: full for (ini, full) in rows}  # 'b'->'Bình', ...

def set_mapping(chat_id: int, pairs: list[tuple[str,str]]):
    con = db()
    for ini, full in pairs:
        con.execute("INSERT INTO name_map(chat_id,initial,fullname) VALUES(?,?,?) "
                    "ON CONFLICT(chat_id,initial) DO UPDATE SET fullname=excluded.fullname",
                    (chat_id, ini.lower(), full.strip()))
    con.commit(); con.close()

# === Parse helpers (chỉ dùng token nhóm, KHÔNG còn "| ..") ===
# Ví dụ: /ab 200 trua  | /b 120 sieu thi  | /bcd 300 gas
ENTRY_CMD_RE = re.compile(r"^/([a-zA-Z]{1,20})\s+(-?\d[\d.,]*)\s*(k|K)?\s*(.*)$")

def parse_entry_group_token(text: str):
    """
    Hỗ trợ:
      - /ab 200 trưa        -> payer='a', participants={'a','b'}
      - /b 120 siêu thị     -> payer='b', participants=None (TẤT CẢ)
      - /bcd 300 gas        -> payer='b', participants={'b','c','d'}
    Trả về: (payer_ini, amount_k, note, participants_inis or None)
    """
    m = ENTRY_CMD_RE.match(text.strip())
    if not m: return None
    token = m.group(1)
    payer_ini = token[0].lower()
    token_letters = [ch.lower() for ch in token]
    # loại trùng, giữ thứ tự
    token_participants = list(dict.fromkeys(token_letters))

    num_raw = (m.group(2) or "").replace(".","").replace(",","")
    if not re.fullmatch(r"-?\d+", num_raw): return None
    amount_k = int(num_raw)  # hiểu luôn là 'k'
    note = (m.group(4) or "").strip()

    participants_inis = token_participants if len(token_participants) >= 2 else None
    return payer_ini, amount_k, note, participants_inis

def fmt_k(nk: int): return f"{nk}k"
def fmt_date_dmy(dt: datetime): return dt.astimezone().strftime("%d-%m-%Y")

# === Tính cân bằng theo từng khoản (hỗ trợ nhóm con) ===
def compute_balances(chat_id: int, start_iso: str):
    members_map = list_members(chat_id)            # 'b'->'Bình'
    members_order = list(members_map.values())
    paid_sum = {name: 0 for name in members_order}
    net = {name: 0.0 for name in members_order}

    con = db(); cur = con.cursor()
    cur.execute("""SELECT name, amount_k, note, participants
                   FROM expenses
                   WHERE chat_id=? AND ts >= datetime(?)
                   ORDER BY id ASC""", (chat_id, start_iso))
    rows = cur.fetchall(); con.close()

    if not rows:
        return paid_sum, net, members_order

    for name, amount_k, _note, participants in rows:
        amount_k = int(amount_k or 0)
        if name not in paid_sum:
            paid_sum[name] = 0
            net[name] = 0.0
            if name not in members_order:
                members_order.append(name)
        paid_sum[name] += amount_k
        net[name] += amount_k

        if participants:  # có nhóm con
            inis = [s.strip().lower() for s in participants.split(",") if s.strip()]
            S = []
            for ch in inis:
                full = members_map.get(ch)
                if full:
                    if full not in net:
                        net[full] = 0.0
                        if full not in members_order:
                            members_order.append(full)
                    S.append(full)
            if not S:  # nếu toàn ký tự lạ → fallback tất cả
                S = members_order
        else:
            S = members_order  # mặc định tất cả

        if len(S) > 0:
            share = amount_k / float(len(S))
            for full in S:
                net[full] -= share

    return paid_sum, net, members_order

def settle_from_net(net: dict[str, float]):
    creditors = [(k, v) for k, v in net.items() if v > 0.5]
    debtors   = [(k,-v) for k, v in net.items() if v < -0.5]
    creditors.sort(key=lambda x: x[1], reverse=True)
    debtors.sort(key=lambda x: x[1], reverse=True)

    moves=[]; i=j=0
    while i < len(debtors) and j < len(creditors):
        dname, dneed = debtors[i]
        cname, cget  = creditors[j]
        pay = min(dneed, cget)
        pay_k = int(round(pay))
        moves.append((dname, cname, pay_k))
        dneed -= pay; cget -= pay
        if dneed <= 0.5: i += 1
        else: debtors[i] = (dname, dneed)
        if cget <= 0.5: j += 1
        else: creditors[j] = (cname, cget)
    return moves

# === Handlers ===
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Cách dùng (không còn '|'):\n"
        "• Chia TẤT CẢ:   /b 120 siêu thị  (b trả, chia đều mọi người)\n"
        "• Chia NHÓM CON: /ab 200 trưa     (a trả, chỉ a & b chia)\n"
        "                 /bcd 300 gas     (b trả, b/c/d chia)\n"
        "• /tongket  — tổng kết kỳ hiện tại\n"
        "• /batdau   — reset kỳ mới\n"
        "• /setmap b=Bình;a=An;d=Duy — map chữ cái → tên"
    )

async def setmap_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(ctx.args); pairs=[]
    for part in raw.split(";"):
        if "=" in part:
            ini, full = part.split("=",1)
            ini, full = ini.strip(), full.strip()
            if len(ini)==1 and full: pairs.append((ini, full))
    if not pairs:
        await update.message.reply_text("Cú pháp: /setmap b=Bình;a=An;d=Duy"); return
    set_mapping(update.effective_chat.id, pairs)
    await update.message.reply_text("✅ Đã cập nhật map")

async def entry_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    parsed = parse_entry_group_token(text)
    if not parsed: return

    payer_ini, amount_k, note, participants_inis = parsed
    chat_id = update.effective_chat.id
    set_default_map(chat_id)

    fullname = get_fullname(chat_id, payer_ini)
    if not fullname:
        await update.message.reply_text(f"Chưa biết '{payer_ini}'. Dùng /setmap {payer_ini}=Tên")
        return

    # Chuẩn hoá participants theo map hiện có
    participants_csv = None
    if participants_inis:
        mapped=[]; seen=set()
        members_map = list_members(chat_id)
        for ch in participants_inis:
            if ch in members_map and ch not in seen:
                mapped.append(ch); seen.add(ch)
        if mapped:
            participants_csv = ",".join(mapped)
        # nếu mapped rỗng → coi là tất cả

    con = db()
    con.execute("""INSERT INTO expenses(chat_id,name,amount_k,note,participants)
                   VALUES(?,?,?,?,?)""",
                (chat_id, fullname, amount_k, note, participants_csv))
    con.commit(); con.close()

    who = f"cho {note}" if note else "(không ghi chú)"
    suffix = f" — nhóm: {''.join(participants_inis).upper()}" if participants_inis and participants_csv else ""
    await update.message.reply_text(
        f"đã ghi nhận: \"{fullname} chi {fmt_k(amount_k)} {who}\"{suffix}"
    )

async def tongket_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_default_map(chat_id)
    start = get_period_start(chat_id)
    end = datetime.now(timezone.utc)

    paid_sum, net, members_order = compute_balances(chat_id, start.isoformat())
    if not any(paid_sum.values()):
        await update.message.reply_text(
            f"Chi tiêu từ ngày {fmt_date_dmy(start)} đến {fmt_date_dmy(end)}:\n- (chưa có khoản chi nào)"
        ); return

    moves = settle_from_net(net)

    lines = [f"Chi tiêu từ ngày {fmt_date_dmy(start)} đến {fmt_date_dmy(end)}:"]
    for name in members_order:
        lines.append(f"- {name} đã chi tiêu tổng cộng: {fmt_k(paid_sum.get(name,0))}")
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
# mọi command khác (ví dụ /b, /ab, /bcd ...) coi là ghi chi
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
