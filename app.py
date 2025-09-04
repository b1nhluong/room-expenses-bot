import os, re, sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret123")  # phải khớp URL webhook

app = FastAPI()
bot = Application.builder().token(TOKEN).build()

DB = "expenses.db"

# ---------- DB helpers ----------
def db():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys = ON")
    return con

def init_db():
    con = db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS expenses(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL,
          name TEXT NOT NULL,         -- Bình / An / Duy ...
          amount_k INTEGER NOT NULL,  -- đơn vị 'k' (nghìn VND)
          note TEXT,
          ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS settings(
          chat_id INTEGER PRIMARY KEY,
          period_start TEXT            -- ISO timestamp: mốc bắt đầu kỳ hiện tại
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS name_map(
          chat_id INTEGER NOT NULL,
          initial TEXT NOT NULL,       -- 'b','a','d' (lowercase)
          fullname TEXT NOT NULL,      -- 'Bình','An','Duy'
          PRIMARY KEY (chat_id, initial)
        )
    """)
    con.commit()
    con.close()

def get_period_start(chat_id: int) -> datetime:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT period_start FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if row and row[0]:
        dt = datetime.fromisoformat(row[0])
    else:
        # Nếu chưa có, mặc định ngay bây giờ (và lưu lại)
        dt = datetime.now(timezone.utc)
        cur.execute("INSERT INTO settings(chat_id, period_start) VALUES(?,?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET period_start=excluded.period_start",
                    (chat_id, dt.isoformat()))
        con.commit()
    con.close()
    return dt

def set_period_start(chat_id: int, dt: datetime):
    con = db()
    con.execute("INSERT INTO settings(chat_id, period_start) VALUES(?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET period_start=excluded.period_start",
                (chat_id, dt.isoformat()))
    con.commit(); con.close()

def clear_expenses(chat_id: int):
    con = db()
    con.execute("DELETE FROM expenses WHERE chat_id=?", (chat_id,))
    con.commit(); con.close()

def set_default_map_if_empty(chat_id: int):
    """Nếu chưa đặt map, tạo mặc định: b->Bình; a->An; d->Duy."""
    con = db(); cur = con.cursor()
    cur.execute("SELECT COUNT(1) FROM name_map WHERE chat_id=?", (chat_id,))
    (cnt,) = cur.fetchone()
    if cnt == 0:
        cur.executemany("INSERT INTO name_map(chat_id, initial, fullname) VALUES(?,?,?)", [
            (chat_id, "b", "Bình"),
            (chat_id, "a", "An"),
            (chat_id, "d", "Duy"),
        ])
        con.commit()
    con.close()

def get_fullname(chat_id: int, initial: str) -> str | None:
    con = db(); cur = con.cursor()
    cur.execute("SELECT fullname FROM name_map WHERE chat_id=? AND initial=?",
                (chat_id, initial.lower()))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None

def set_mapping(chat_id: int, pairs: list[tuple[str, str]]):
    con = db(); cur = con.cursor()
    for ini, full in pairs:
        cur.execute("INSERT INTO name_map(chat_id,initial,fullname) VALUES(?,?,?) "
                    "ON CONFLICT(chat_id,initial) DO UPDATE SET fullname=excluded.fullname",
                    (chat_id, ini.lower(), full.strip()))
    con.commit(); con.close()

# ---------- Parse helpers ----------
ENTRY_RE = re.compile(
    r"^\s*([a-zA-Z])\s+(-?\d[\d.,]*)\s*(k|K)?\s*(.*)$"
)
# Ví dụ khớp: "b 120 sieu thi", "A 317 ăn", "D 134k tiền điện"

def parse_entry(text: str):
    m = ENTRY_RE.match(text)
    if not m:
        return None
    ini = m.group(1)           # ký tự đầu (b/a/d ...)
    num_raw = m.group(2)       # số
    has_k = bool(m.group(3))   # có 'k' hay không (không ảnh hưởng, chỉ bỏ qua)
    note = (m.group(4) or "").strip()

    # Chuẩn hoá số: bỏ . và ,
    num_clean = num_raw.replace(".", "").replace(",", "")
    if not re.fullmatch(r"-?\d+", num_clean):
        return None
    amount_k = int(num_clean)  # luôn hiểu là 'k' (nghìn VND). "120k" -> 120; "120" -> 120
    return ini, amount_k, note

def fmt_k(nk: int) -> str:
    return f"{nk}k"

def fmt_date_dmy(dt: datetime) -> str:
    local = dt.astimezone()  # dùng TZ server; với Render/UTC vẫn OK hiển thị
    return local.strftime("%d-%m-%Y")

# ---------- Settlement ----------
def settle_k(spent: dict[str, int], members: list[str]):
    # spent: {'Bình': 120, 'An': 60, 'Duy': 120} (đơn vị k)
    n = len(members)
    total = sum(spent.get(name, 0) for name in members)
    share = total / n if n else 0.0

    creditors = []  # (name, +k)
    debtors = []    # (name, +k thiếu)
    for name in members:
        diff = spent.get(name, 0) - share
        if diff > 0.5:
            creditors.append((name, diff))
        elif diff < -0.5:
            debtors.append((name, -diff))

    creditors.sort(key=lambda x: x[1], reverse=True)
    debtors.sort(key=lambda x: x[1], reverse=True)

    moves = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        dname, dneed = debtors[i]
        cname, cget = creditors[j]
        pay = min(dneed, cget)
        # làm tròn tới đơn vị 'k'
        pay_k = int(round(pay))
        moves.append((dname, cname, pay_k))
        dneed -= pay; cget -= pay
        if dneed <= 0.5: i += 1
        else: debtors[i] = (dname, dneed)
        if cget <= 0.5: j += 1
        else: creditors[j] = (cname, cget)

    return total, int(round(share)), moves

# ---------- Handlers ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot quản lý chi tiêu theo định dạng:\n"
        "• Ghi chi:  b 120 siêu thị  |  a 317 ăn  |  d 134 tiền điện\n"
        "  (chữ cái đầu là viết tắt tên, số là 'k'; '120k' cũng hiểu là 120)\n"
        "• /tongket  — tổng kết kỳ hiện tại\n"
        "• /batdau   — xóa dữ liệu cũ, bắt đầu kỳ mới (mốc thời gian mới)\n"
        "• /setmap b=Bình;a=An;d=Duy — đặt/đổi map chữ cái → tên đầy đủ"
    )

async def cmd_setmap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Cú pháp: /setmap b=Bình;a=An;d=Duy
    raw = " ".join(ctx.args)
    pairs = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            await update.message.reply_text("Cú pháp: /setmap b=Bình;a=An;d=Duy")
            return
        ini, full = part.split("=", 1)
        ini = ini.strip()
        full = full.strip()
        if not ini or not full or len(ini) != 1:
            await update.message.reply_text("Mỗi cặp dạng x=Tên (x là 1 ký tự).")
            return
        pairs.append((ini, full))
    if not pairs:
        await update.message.reply_text("Cú pháp: /setmap b=Bình;a=An;d=Duy")
        return

    chat_id = update.effective_chat.id
    set_mapping(chat_id, pairs)
    show = ", ".join([f"{a}→{b}" for a,b in pairs])
    await update.message.reply_text(f"✅ Đã cập nhật map: {show}")

async def on_text_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Bắt mọi tin nhắn TEXT không phải command, parse theo mẫu "b 120 ghi chú"
    text = update.message.text or ""
    parsed = parse_entry(text)
    if not parsed:
        return  # bỏ qua tin nhắn không khớp định dạng
    ini, amount_k, note = parsed
    chat_id = update.effective_chat.id

    # đảm bảo có name map
    set_default_map_if_empty(chat_id)
    fullname = get_fullname(chat_id, ini)
    if not fullname:
        await update.message.reply_text(
            f"Chưa biết ký tự '{ini}'. Hãy đặt map bằng /setmap {ini}=Tên đầy đủ"
        )
        return

    # ghi DB
    con = db()
    con.execute(
        "INSERT INTO expenses(chat_id, name, amount_k, note) VALUES(?,?,?,?)",
        (chat_id, fullname, amount_k, note)
    )
    con.commit(); con.close()

    # phản hồi
    quoted_note = note if note else "(không ghi chú)"
    await update.message.reply_text(
        f"đã ghi nhận: \"{fullname} chi {fmt_k(amount_k)} cho {quoted_note}\""
    )

async def cmd_tongket(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # đảm bảo có map mặc định
    set_default_map_if_empty(chat_id)

    # mốc thời gian kỳ hiện tại
    start_dt = get_period_start(chat_id)
    start_str = fmt_date_dmy(start_dt)
    end_dt = datetime.now(timezone.utc)
    end_str = fmt_date_dmy(end_dt)

    # tổng chi theo người (trong toàn bộ DB kể từ period_start)
    con = db(); cur = con.cursor()
    cur.execute("""
        SELECT name, SUM(amount_k) FROM expenses
        WHERE chat_id=? AND ts >= datetime(?)
        GROUP BY name
    """, (chat_id, start_dt.isoformat()))
    data = dict(cur.fetchall())
    con.close()

    # Nếu chưa có chi tiêu nào
    if not data:
        await update.message.reply_text(
            f"Chi tiêu từ ngày {start_str} đến {end_str}:\n"
            f"- (chưa có khoản chi nào)\n"
        )
        return

    # danh sách thành viên từ map (đảm bảo kể cả người chưa chi)
    con = db(); cur = con.cursor()
    cur.execute("SELECT fullname FROM name_map WHERE chat_id=?", (chat_id,))
    members = [r[0] for r in cur.fetchall()]
    con.close()

    total, share, moves = settle_k(data, members)

    # render kết quả
    lines = [f"Chi tiêu từ ngày {start_str} đến {end_str}:"]
    for name in members:
        lines.append(f"- {name} đã chi tiêu tổng cộng: {fmt_k(data.get(name, 0))}")
    if moves:
        transfers = ", ".join([f"{a} trả {b} {fmt_k(k)}" for a,b,k in moves])
        lines.append(transfers)
    else:
        lines.append("✅ Đã cân bằng chi tiêu giữa các thành viên.")
    await update.message.reply_text("\n".join(lines))

async def cmd_batdau(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # xoá dữ liệu cũ + đặt mốc thời gian mới
    clear_expenses(chat_id)
    now = datetime.now(timezone.utc)
    set_period_start(chat_id, now)
    await update.message.reply_text(
        f"✅ Đã bắt đầu kỳ mới từ ngày {fmt_date_dmy(now)}. "
        f"Dữ liệu kỳ trước đã xoá."
    )

# ---------- Wire up handlers ----------
bot.add_handler(CommandHandler("start", cmd_start))
bot.add_handler(CommandHandler("setmap", cmd_setmap))
bot.add_handler(CommandHandler("tongket", cmd_tongket))
bot.add_handler(CommandHandler("batdau", cmd_batdau))
# tin nhắn thường (không phải command)
bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_entry))

# ---------- FastAPI lifecycle & webhook ----------
@app.on_event("startup")
async def on_startup():
    init_db()

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    data = await request.json()
    await bot.update_queue.put(Update.de_json(data, bot.bot))
    return {"ok": True}
