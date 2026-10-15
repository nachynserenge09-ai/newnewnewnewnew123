import asyncio, hashlib, hmac, json, os, sqlite3, time, urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo,
)
from aiogram.filters import CommandStart

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "kmt.db"
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GAS_URL = os.getenv("GAS_URL", "")
DEMO = os.getenv("DEMO", "0") == "1"
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
REFRESH_SECONDS = 30 * 60

BELLS = [
    {"n": 1, "start": "08:30", "end": "10:00"},
    {"n": 2, "start": "10:30", "end": "12:00"},
    {"n": 3, "start": "12:20", "end": "13:50"},
    {"n": 4, "start": "14:00", "end": "15:30"},
    {"n": 5, "start": "15:40", "end": "17:10"},
]

# порядок дней — для достройки даты, когда лист пустой
WEEKDAY_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

SCHEDULE = {"updated": None, "dates": [], "groups": [], "data": {}}

# --- мусор, который не надо показывать как "предмет/преподаватель"
NON_TEACHER_TITLES = {
    "разговоры о важном", "ин-яз", "ин. яз", "ин яз",
    "нет занятия", "нет занятий",
}

def parse_schedule(raw):
    """JSON -> {dates, groups, data[date][group] = [lessons]}.

    Если у листа нет 'дата' (как у Вт/Ср/Чт), вычисляем её от ближайшего
    известного понедельника, сдвигая на нужное число дней.
    """
    # сначала найдём "опорную" дату: первый лист с датой
    base_iso = None
    base_idx = None
    for idx, sheet in enumerate(raw):
        d = str(sheet.get("дата") or "").strip()
        if not d:
            continue
        try:
            base_iso = datetime.strptime(d, "%d.%m.%Y")
            base_idx = idx
            break
        except ValueError:
            continue

    dates, data, groups = [], {}, set()

    for idx, sheet in enumerate(raw):
        name = str(sheet.get("лист") or "").strip()
        d = str(sheet.get("дата") or "").strip()

        if d:
            try:
                dt = datetime.strptime(d, "%d.%m.%Y")
            except ValueError:
                continue
        elif base_iso is not None and name in WEEKDAY_ORDER:
            # достраиваем дату от опорной
            offset = WEEKDAY_ORDER.index(name) - WEEKDAY_ORDER.index(
                WEEKDAY_ORDER[base_idx % 7] if base_idx is not None else "Понедельник")
            # если name идёт до опорного дня — берём следующую неделю
            if offset < 0:
                offset += 7
            dt = base_iso + timedelta(days=offset)
        else:
            continue

        iso = dt.strftime("%Y-%m-%d")
        by_group = {}
        for row in sheet.get("данные", []):
            group = str(row.get("Группа") or "").strip()
            if not group:
                continue
            lessons = []
            for i in range(1, 6):
                title = str(row.get(f"{i} пара") or "").strip()
                room = str(row.get(f"ауд{i}") or "").strip()
                if not title or title.lower().startswith("нет занят"):
                    continue
                if room.lower() == "нет":
                    room = ""
                lessons.append({"num": i, "title": title, "room": room})
            by_group[group] = lessons
            groups.add(group)

        data[iso] = by_group
        dates.append({"date": iso, "iso": iso, "weekday": name, "display": dt.strftime("%d.%m")})

    dates.sort(key=lambda x: x["iso"])
    # уберём дубли по дате (если вдруг)
    seen, uniq = set(), []
    for d in dates:
        if d["date"] in seen:
            continue
        seen.add(d["date"])
        uniq.append(d)

    return {"dates": uniq, "data": data, "groups": sorted(groups)}

def fetch_schedule():
    req = urllib.request.Request(GAS_URL, headers={"User-Agent": "kmt-miniapp"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = json.loads(r.read().decode("utf-8"))
    SCHEDULE.update(parse_schedule(raw))
    SCHEDULE["updated"] = int(time.time())

async def refresher():
    while True:
        try:
            if GAS_URL:
                await asyncio.to_thread(fetch_schedule)
            else:
                print("GAS_URL не задан: расписание не загружается")
        except Exception as e:
            print("Не удалось обновить расписание:", e)
        await asyncio.sleep(REFRESH_SECONDS if SCHEDULE["dates"] else 60)

# ---------- БД ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS students(
        telegram_id INTEGER PRIMARY KEY, name TEXT, group_name TEXT);
    CREATE TABLE IF NOT EXISTS grades(
        id INTEGER PRIMARY KEY, telegram_id INTEGER, subject TEXT,
        grade INTEGER, date TEXT);
    """)
    con.commit(); con.close()

init_db()

# ---------- aiogram ----------
BOT = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
DP = Dispatcher() if BOT else None

def student_by_tg(tg_id: int):
    con = db()
    s = con.execute("SELECT * FROM students WHERE telegram_id=?", (tg_id,)).fetchone()
    con.close()
    return s

def save_student(tg_id: int, name: str, group: str):
    con = db()
    con.execute("INSERT OR REPLACE INTO students VALUES(?,?,?)", (tg_id, name[:60], group))
    con.commit(); con.close()

def schedule_for_group(group: str):
    return [{**d, "lessons": SCHEDULE["data"].get(d["date"], {}).get(group, [])}
            for d in SCHEDULE["dates"]]

def teachers_from_schedule():
    """ФИО -> список групп. Отфильтровываем 'Разговоры о важном', 'Ин-яз' и т.п."""
    res = {}
    for _date, by_group in SCHEDULE["data"].items():
        for group, lessons in by_group.items():
            for l in lessons:
                title = l["title"].strip()
                if title.lower() in NON_TEACHER_TITLES:
                    continue
                # грубая проверка, что это похоже на ФИО:
                # есть хотя бы одна точка и хотя бы одна заглавная буква
                if "." not in title or not any(ch.isupper() for ch in title):
                    continue
                res.setdefault(title, set()).add(group)
    return {k: sorted(v) for k, v in sorted(res.items())}

# ---------- клавиатуры ----------
def main_kb():
    rows = [
        [InlineKeyboardButton(text="📚 Расписание уроков", callback_data="sch:today")],
        [InlineKeyboardButton(text="🔔 Расписание звонков", callback_data="bells")],
        [InlineKeyboardButton(text="👨‍🏫 Преподаватели", callback_data="teachers")],
        [InlineKeyboardButton(text="📊 Мои оценки", callback_data="grades")],
    ]
    if WEBAPP_URL:
        rows.append([InlineKeyboardButton(
            text="🌐 Открыть приложение",
            web_app=WebAppInfo(url=WEBAPP_URL))])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]
    ])

def days_kb(group: str):
    ds = schedule_for_group(group)
    rows, row = [], []
    for d in ds:
        label = f"{d['weekday'][:2]} {d.get('display', d['date'][5:])}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"sch:{d['date']}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def groups_kb():
    gs = SCHEDULE["groups"]
    rows, row = [], []
    for g in gs:
        row.append(InlineKeyboardButton(text=g, callback_data=f"reg:{g}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ---------- хендлеры ----------
@DP.message(CommandStart())
async def cmd_start(m: Message):
    s = student_by_tg(m.from_user.id)
    if not s:
        if not SCHEDULE["groups"]:
            await m.answer("Расписание ещё не загружено. Попробуй позже.")
            return
        await m.answer(
            "Привет! Я помогу с расписанием, звонками и оценками.\n"
            "Сначала выбери свою группу:",
            reply_markup=groups_kb(),
        )
        return
    await m.answer(
        f"Привет, {s['name']}! Группа {s['group_name']}.\nЧто посмотрим?",
        reply_markup=main_kb(),
    )

@DP.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await c.message.edit_text("Главное меню:", reply_markup=main_kb())
    await c.answer()

@DP.callback_query(F.data.startswith("reg:"))
async def cb_reg(c: CallbackQuery):
    group = c.data.split(":", 1)[1]
    if group not in SCHEDULE["groups"]:
        await c.answer("Такой группы нет", show_alert=True); return
    save_student(c.from_user.id, c.from_user.full_name, group)
    await c.message.edit_text(
        f"Готово! Ты в группе {group}.\nЧто посмотрим?",
        reply_markup=main_kb(),
    )
    await c.answer()

@DP.callback_query(F.data == "bells")
async def cb_bells(c: CallbackQuery):
    text = "🔔 Расписание звонков:\n\n" + "\n".join(
        f"{b['n']} пара: {b['start']}–{b['end']}" for b in BELLS)
    await c.message.edit_text(text, reply_markup=back_kb())
    await c.answer()

@DP.callback_query(F.data == "teachers")
async def cb_teachers(c: CallbackQuery):
    t = teachers_from_schedule()
    if not t:
        await c.message.edit_text("Данных о преподавателях пока нет.", reply_markup=back_kb())
        await c.answer(); return
    lines = []
    for name, groups in t.items():
        # показываем не больше 6 групп, чтобы не растягивать
        gtxt = ", ".join(groups[:6]) + ("…" if len(groups) > 6 else "")
        lines.append(f"• {name} — {gtxt}")
    text = "👨‍🏫 Преподаватели и группы:\n\n" + "\n".join(lines)
    # разбиваем, если > 4000 символов
    await c.message.edit_text(text[:4000], reply_markup=back_kb())
    await c.answer()

@DP.callback_query(F.data == "grades")
async def cb_grades(c: CallbackQuery):
    con = db()
    rows = con.execute(
        "SELECT subject,grade FROM grades WHERE telegram_id=? ORDER BY date",
        (c.from_user.id,)).fetchall()
    con.close()
    if not rows:
        await c.message.edit_text("Оценок пока нет.", reply_markup=back_kb())
        await c.answer(); return
    by = {}
    for r in rows:
        by.setdefault(r["subject"], []).append(r["grade"])
    text = "📊 Твои оценки:\n\n" + "\n".join(
        f"{subj}: {', '.join(map(str, gs))}  (ср. {round(sum(gs)/len(gs), 2)})"
        for subj, gs in by.items())
    await c.message.edit_text(text, reply_markup=back_kb())
    await c.answer()

@DP.callback_query(F.data == "sch:today")
async def cb_sch_today(c: CallbackQuery):
    s = student_by_tg(c.from_user.id)
    if not s:
        await c.answer("Сначала выбери группу: /start", show_alert=True); return
    if not SCHEDULE["dates"]:
        await c.answer("Расписание ещё не загружено", show_alert=True); return
    await c.message.edit_text("Выбери день:", reply_markup=days_kb(s["group_name"]))
    await c.answer()

@DP.callback_query(F.data.startswith("sch:"))
async def cb_sch_day(c: CallbackQuery):
    date = c.data.split(":", 1)[1]
    if date == "today":
        return
    s = student_by_tg(c.from_user.id)
    if not s:
        await c.answer("Сначала /start", show_alert=True); return
    days = schedule_for_group(s["group_name"])
    day = next((d for d in days if d["date"] == date), None)
    if not day:
        await c.answer("Нет данных", show_alert=True); return
    if not day["lessons"]:
        text = f"📚 {day['weekday']} {day['date']}:\n\nПар нет."
    else:
        bells = {b["n"]: b for b in BELLS}
        lines = []
        for l in day["lessons"]:
            if not l["room"]:
                room = ""
            elif l["room"] == "ДО":
                room = "дистанционно"
            else:
                room = f"каб. {l['room']}"
            b = bells.get(l["num"])
            t = f" {b['start']}–{b['end']}" if b else ""
            lines.append(f"{l['num']}. {l['title']} — {room}{t}")
        text = f"📚 {day['weekday']} {day['date']}:\n\n" + "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Дни", callback_data="sch:today")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="menu")],
    ])
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()

# ---------- lifespan ----------
@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(refresher())]
    if BOT and DP:
        tasks.append(asyncio.create_task(DP.start_polling(BOT)))
    yield
    for t in tasks:
        t.cancel()

app = FastAPI(lifespan=lifespan)

# ---------- проверка подписи Telegram ----------
def get_user(init_data: str) -> dict:
    if not BOT_TOKEN:
        return {"id": 1, "first_name": "Тест"}
    try:
        data = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            raise ValueError("bad hash")
        if time.time() - int(data["auth_date"]) > 86400:
            raise ValueError("expired")
        return json.loads(data["user"])
    except Exception:
        raise HTTPException(401, "Неверные данные Telegram")

# ---------- API ----------
@app.get("/api/me")
def me(x_init_data: str = Header(default="")):
    user = get_user(x_init_data)
    con = db()
    s = con.execute("SELECT * FROM students WHERE telegram_id=?", (user["id"],)).fetchone()
    con.close()
    return {"registered": bool(s), "name": s["name"] if s else user.get("first_name", ""),
            "group": s["group_name"] if s else None, "groups": SCHEDULE["groups"]}

class Reg(BaseModel):
    name: str
    group: str

DEMO_SUBJECTS = ["Математика", "Информатика", "Русский язык", "История"]

@app.post("/api/register")
def register(r: Reg, x_init_data: str = Header(default="")):
    user = get_user(x_init_data)
    if r.group not in SCHEDULE["groups"]:
        raise HTTPException(400, "Такой группы нет в расписании")
    con = db()
    con.execute("INSERT OR REPLACE INTO students VALUES(?,?,?)",
                (user["id"], r.name.strip()[:60], r.group))
    if DEMO and not con.execute("SELECT 1 FROM grades WHERE telegram_id=?", (user["id"],)).fetchone():
        vals = [(user["id"], sub, g, f"2026-10-0{i+1}")
                for sub in DEMO_SUBJECTS for i, g in enumerate([5, 4, 4])]
        con.executemany("INSERT INTO grades(telegram_id,subject,grade,date) VALUES(?,?,?,?)", vals)
    con.commit(); con.close()
    return {"ok": True}

@app.get("/api/schedule")
def schedule(x_init_data: str = Header(default="")):
    user = get_user(x_init_data)
    con = db()
    s = con.execute("SELECT group_name FROM students WHERE telegram_id=?", (user["id"],)).fetchone()
    con.close()
    if not s:
        raise HTTPException(404, "Сначала зарегистрируйтесь")
    g = s["group_name"]
    dates = [{**d, "lessons": SCHEDULE["data"].get(d["date"], {}).get(g, [])} for d in SCHEDULE["dates"]]
    return {"dates": dates, "bells": BELLS, "updated": SCHEDULE["updated"]}

@app.get("/api/grades")
def grades(x_init_data: str = Header(default="")):
    user = get_user(x_init_data)
    con = db()
    rows = con.execute("SELECT subject,grade,date FROM grades WHERE telegram_id=? ORDER BY date",
                       (user["id"],)).fetchall()
    con.close()
    by = {}
    for r in rows:
        by.setdefault(r["subject"], []).append(r["grade"])
    items = [{"subject": k, "grades": v, "avg": round(sum(v) / len(v), 2)} for k, v in by.items()]
    allg = [g for v in by.values() for g in v]
    return {"items": items, "gpa": round(sum(allg) / len(allg), 2) if allg else None}

@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "index.html").read_text(encoding="utf-8")