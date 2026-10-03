"""
Бот «подушка безопасности» + Telegram Mini App.

Один процесс делает две вещи:
  1) Telegram-бот (polling): /start присылает кнопку, которая открывает Mini App;
  2) веб-сервер (aiohttp): отдаёт webapp/index.html и JSON API для него.

Все данные лежат в SQLite (DB_PATH). На Railway подключи Volume и задай
DB_PATH=/data/cushion.db, иначе база сбрасывается при каждом редеплое.

Переменные окружения:
  BOT_TOKEN   — токен от BotFather (обязательно)
  DB_PATH     — путь к базе, по умолчанию cushion.db
  WEBAPP_URL  — https-адрес Mini App; если не задан, берётся RAILWAY_PUBLIC_DOMAIN
  PORT        — порт веб-сервера (Railway задаёт сам)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "cushion.db")
PORT = int(os.getenv("PORT", "8080"))
_public_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
WEBAPP_URL = (os.getenv("WEBAPP_URL") or (f"https://{_public_domain}" if _public_domain else "")).strip().rstrip("/")
if WEBAPP_URL and not WEBAPP_URL.startswith("https://"):
    logging.getLogger("cushion").error("WEBAPP_URL должен начинаться с https:// — сейчас %r, кнопка приложения отключена", WEBAPP_URL)
    WEBAPP_URL = ""
INIT_DATA_MAX_AGE = 7 * 24 * 3600  # сколько секунд подпись Telegram считается свежей

BASE_DIR = Path(__file__).resolve().parent
# index.html ищется в нескольких местах: в папке webapp (основной вариант) или рядом с ботом
INDEX_CANDIDATES = [
    BASE_DIR / "webapp" / "index.html",
    BASE_DIR / "index.html",
    Path.cwd() / "webapp" / "index.html",
    Path.cwd() / "index.html",
]


def find_index():
    for path in INDEX_CANDIDATES:
        if path.is_file():
            return path
    return None

DEFAULT_STAGES = [100_000, 500_000, 1_000_000]
DEFAULT_CURRENCY = "AMD"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cushion")
router = Router()


# ---------- db ----------

def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    parent = Path(DB_PATH).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    with closing(db()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                tg_id INTEGER PRIMARY KEY,
                rate REAL NOT NULL DEFAULT 20,
                want_rate REAL NOT NULL DEFAULT 10,
                windfall_rate REAL NOT NULL DEFAULT 50,
                monthly_expense REAL NOT NULL DEFAULT 0,
                stages_json TEXT NOT NULL DEFAULT '[100000,500000,1000000]',
                currency TEXT NOT NULL DEFAULT 'AMD'
            )
        """)
        for stmt in (
            "ALTER TABLE config ADD COLUMN stages_json TEXT NOT NULL DEFAULT '[100000,500000,1000000]'",
            "ALTER TABLE config ADD COLUMN currency TEXT NOT NULL DEFAULT 'AMD'",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                kind TEXT NOT NULL,
                saved REAL NOT NULL,
                want REAL NOT NULL DEFAULT 0,
                spend REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                position INTEGER NOT NULL,
                purchased INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


@dataclass
class Config:
    rate: float
    want_rate: float
    windfall_rate: float
    monthly_expense: float
    stages: list
    currency: str


def get_config(tg_id: int) -> Config:
    with closing(db()) as conn, conn:
        row = conn.execute(
            "SELECT rate, want_rate, windfall_rate, monthly_expense, stages_json, currency FROM config WHERE tg_id=?",
            (tg_id,),
        ).fetchone()
        if row is None:
            conn.execute("INSERT INTO config (tg_id) VALUES (?)", (tg_id,))
            return Config(20.0, 10.0, 50.0, 0.0, list(DEFAULT_STAGES), DEFAULT_CURRENCY)
        rate, want_rate, windfall_rate, expense, stages_json, currency = row
        try:
            stages_list = json.loads(stages_json)
        except (TypeError, ValueError):
            stages_list = list(DEFAULT_STAGES)
        return Config(rate, want_rate, windfall_rate, expense, stages_list, currency)


CONFIG_COLUMNS = {"rate", "want_rate", "windfall_rate", "monthly_expense", "stages_json", "currency"}


def update_config(tg_id: int, **fields):
    get_config(tg_id)
    if "stages" in fields:
        fields["stages_json"] = json.dumps(fields.pop("stages"))
    assert set(fields) <= CONFIG_COLUMNS
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with closing(db()) as conn, conn:
        conn.execute(f"UPDATE config SET {cols} WHERE tg_id=?", (*fields.values(), tg_id))


def total_saved(tg_id: int) -> float:
    with closing(db()) as conn:
        return conn.execute("SELECT COALESCE(SUM(saved),0) FROM entries WHERE tg_id=?", (tg_id,)).fetchone()[0]


def want_fund_balance(tg_id: int) -> float:
    with closing(db()) as conn:
        earned = conn.execute("SELECT COALESCE(SUM(want),0) FROM entries WHERE tg_id=?", (tg_id,)).fetchone()[0]
        spent = conn.execute(
            "SELECT COALESCE(SUM(price),0) FROM wants WHERE tg_id=? AND purchased=1", (tg_id,)
        ).fetchone()[0]
        return earned - spent


def add_entry(tg_id: int, amount: float, kind: str, saved: float, want: float, spend: float):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO entries (tg_id, amount, kind, saved, want, spend) VALUES (?,?,?,?,?,?)",
            (tg_id, amount, kind, saved, want, spend),
        )


def list_entries(tg_id: int):
    with closing(db()) as conn:
        return conn.execute(
            "SELECT id, amount, kind, saved, want, spend, created_at FROM entries WHERE tg_id=? ORDER BY id DESC",
            (tg_id,),
        ).fetchall()


def add_want(tg_id: int, name: str, price: float):
    with closing(db()) as conn, conn:
        pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM wants WHERE tg_id=?", (tg_id,)).fetchone()[0]
        conn.execute("INSERT INTO wants (tg_id, name, price, position) VALUES (?,?,?,?)", (tg_id, name, price, pos))


def list_wants(tg_id: int):
    with closing(db()) as conn:
        return conn.execute(
            "SELECT id, name, price, purchased FROM wants WHERE tg_id=? ORDER BY position", (tg_id,)
        ).fetchall()


def active_want(tg_id: int):
    with closing(db()) as conn:
        return conn.execute(
            "SELECT id, name, price FROM wants WHERE tg_id=? AND purchased=0 ORDER BY position LIMIT 1", (tg_id,)
        ).fetchone()


def mark_purchased(tg_id: int, want_id: int):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE wants SET purchased=1 WHERE id=? AND tg_id=?", (want_id, tg_id))


# купленные хотелки — уже история: их нельзя менять или удалять,
# иначе задним числом изменится остаток фонда хотелок
def edit_want(tg_id: int, want_id: int, name: str, price: float) -> bool:
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "UPDATE wants SET name=?, price=? WHERE id=? AND tg_id=? AND purchased=0",
            (name, price, want_id, tg_id),
        )
        return cur.rowcount > 0


def drop_want(tg_id: int, want_id: int) -> bool:
    with closing(db()) as conn, conn:
        cur = conn.execute("DELETE FROM wants WHERE id=? AND tg_id=? AND purchased=0", (want_id, tg_id))
        return cur.rowcount > 0


def stages_for(cfg: Config):
    stages = list(cfg.stages)
    if cfg.monthly_expense > 0:
        final_target = round(cfg.monthly_expense * 6)
        # добавляем как отдельный этап, только если он больше последнего —
        # иначе порядок этапов сломается (они должны идти по возрастанию)
        if not stages or final_target > stages[-1]:
            stages.append(final_target)
    return stages


def fmt(n: float, currency: str) -> str:
    return f"{n:,.0f}".replace(",", " ") + f" {currency}"


def parse_amount(value):
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace(" ", "").replace("\u00a0", "").replace("'", "")
    # несколько разделителей («1.000.000», «1,000,000») — это разряды, а не дробь
    if text.count(".") + text.count(",") > 1:
        text = text.replace(".", "").replace(",", "")
    try:
        v = float(text.replace(",", "."))
    except ValueError:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


# ---------- состояние для Mini App ----------

def _iso(ts: str) -> str:
    # SQLite CURRENT_TIMESTAMP пишет UTC в формате "YYYY-MM-DD HH:MM:SS"
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ts


def build_state(tg_id: int) -> dict:
    cfg = get_config(tg_id)
    history = []
    for eid, amount, kind, saved, want, spend, created_at in list_entries(tg_id):
        if kind.startswith("withdraw"):
            reason = kind.split(":", 1)[1] if ":" in kind else ""
            history.append({
                "id": eid, "date": _iso(created_at), "kind": "withdraw", "reason": reason,
                "amount": -amount, "toCushion": saved, "toWant": 0, "toSpend": 0,
            })
        else:
            history.append({
                "id": eid, "date": _iso(created_at), "kind": kind, "reason": "",
                "amount": amount, "toCushion": saved, "toWant": want, "toSpend": spend,
            })
    return {
        "config": {
            "rate": cfg.rate, "wantRate": cfg.want_rate, "bonusRate": cfg.windfall_rate,
            "expense": cfg.monthly_expense, "stages": cfg.stages, "currency": cfg.currency,
        },
        "saved": total_saved(tg_id),
        "wantFund": max(0.0, want_fund_balance(tg_id)),
        "wants": [
            {"id": wid, "name": name, "price": price, "purchased": bool(purchased)}
            for wid, name, price, purchased in list_wants(tg_id)
        ],
        "history": history,
    }


class ApiError(Exception):
    pass


def do_income(tg_id: int, body: dict) -> dict:
    amount = parse_amount(body.get("amount"))
    if amount is None or amount <= 0:
        raise ApiError("Введи сумму больше нуля, например 150000")
    is_bonus = body.get("kind") == "bonus"
    cfg = get_config(tg_id)
    cushion_rate = cfg.windfall_rate if is_bonus else cfg.rate
    saved = round(amount * cushion_rate / 100, 2)
    want = round(amount * cfg.want_rate / 100, 2)
    spend = round(amount - saved - want, 2)
    add_entry(tg_id, amount, "bonus" if is_bonus else "regular", saved, want, spend)
    return {
        "split": {"cRate": cushion_rate, "wantRate": cfg.want_rate,
                  "toCushion": saved, "toWant": want, "toSpend": spend},
    }


def do_withdraw(tg_id: int, body: dict) -> dict:
    amount = parse_amount(body.get("amount"))
    if amount is None or amount <= 0:
        raise ApiError("Введи сумму больше нуля")
    reason = str(body.get("reason") or "").strip()[:200]
    current = total_saved(tg_id)
    if amount > current + 1e-9:
        cfg = get_config(tg_id)
        raise ApiError(f"В подушке только {fmt(current, cfg.currency)} — столько снять не получится")
    add_entry(tg_id, amount, f"withdraw:{reason}" if reason else "withdraw", -amount, 0, 0)
    return {}


def _want_fields(body: dict):
    name = str(body.get("name") or "").strip()[:120]
    price = parse_amount(body.get("price"))
    if not name:
        raise ApiError("Напиши название цели")
    if price is None or price <= 0:
        raise ApiError("Цена должна быть больше нуля")
    return name, price


def do_want_add(tg_id: int, body: dict) -> dict:
    name, price = _want_fields(body)
    add_want(tg_id, name, price)
    return {}


def do_want_edit(tg_id: int, want_id: int, body: dict) -> dict:
    name, price = _want_fields(body)
    if not edit_want(tg_id, want_id, name, price):
        raise ApiError("Эту цель уже нельзя изменить")
    return {}


def do_want_delete(tg_id: int, want_id: int) -> dict:
    if not drop_want(tg_id, want_id):
        raise ApiError("Эту цель уже нельзя удалить")
    return {}


def do_want_buy(tg_id: int) -> dict:
    aw = active_want(tg_id)
    if not aw:
        raise ApiError("Очередь пуста — сначала добавь цель")
    wid, name, price = aw
    fund = want_fund_balance(tg_id)
    if fund < price:
        cfg = get_config(tg_id)
        raise ApiError(
            f"Пока не хватает {fmt(price - fund, cfg.currency)}. "
            f"В фонде {fmt(max(fund, 0), cfg.currency)} из {fmt(price, cfg.currency)}."
        )
    mark_purchased(tg_id, wid)
    return {"bought": name}


def do_settings(tg_id: int, body: dict) -> dict:
    cfg = get_config(tg_id)
    upd = {}

    def pct(key):
        v = parse_amount(body[key])
        if v is None or not (0 <= v <= 100):
            raise ApiError("Процент должен быть от 0 до 100")
        return round(v)

    if "rate" in body:
        upd["rate"] = pct("rate")
    if "wantRate" in body:
        upd["want_rate"] = pct("wantRate")
    if "bonusRate" in body:
        upd["windfall_rate"] = pct("bonusRate")
    if round(upd.get("rate", cfg.rate)) + round(upd.get("want_rate", cfg.want_rate)) > 100:
        raise ApiError("Подушка + хотелки не может быть больше 100%")

    if "expense" in body:
        raw = body["expense"]
        v = 0.0 if raw in ("", None) else parse_amount(raw)
        if v is None or v < 0:
            raise ApiError("Месячные расходы — это число, например 300000")
        upd["monthly_expense"] = v

    if "stages" in body:
        raw = body["stages"]
        if not isinstance(raw, list):
            raise ApiError("Этапы должны быть списком сумм")
        vals = [parse_amount(x) for x in raw]
        if any(v is None or v <= 0 for v in vals):
            raise ApiError("Каждый этап — сумма больше нуля")
        vals = sorted({round(v) for v in vals}) or list(DEFAULT_STAGES)
        upd["stages"] = vals[:20]

    if "currency" in body:
        cur = str(body["currency"] or "").strip()[:8]
        if not cur:
            raise ApiError("Укажи валюту, например AMD или $")
        upd["currency"] = cur

    update_config(tg_id, **upd)
    return {}


# ---------- проверка подписи Telegram ----------

def user_from_init_data(init_data: str):
    """Проверяет подпись initData по алгоритму Telegram и возвращает id пользователя."""
    if not init_data or not BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        if datetime.now(timezone.utc).timestamp() - auth_date > INIT_DATA_MAX_AGE:
            return None
        return int(json.loads(pairs["user"])["id"])
    except (KeyError, ValueError, TypeError):
        return None


# ---------- веб-сервер ----------

def _error(status: int, message: str):
    return web.json_response({"error": message}, status=status)


def api(handler):
    async def wrapper(request: web.Request):
        tg_id = user_from_init_data(request.headers.get("X-Telegram-Init-Data", ""))
        if tg_id is None:
            return _error(401, "Открой приложение через бота в Telegram")
        body = {}
        if request.method in ("POST", "PATCH"):
            raw = await request.text()
            if raw.strip():
                try:
                    body = json.loads(raw)
                except ValueError:
                    return _error(400, "Некорректный запрос")
                if not isinstance(body, dict):
                    return _error(400, "Некорректный запрос")
        try:
            extra = handler(request, tg_id, body) or {}
        except ApiError as e:
            return _error(400, str(e))
        return web.json_response({**extra, "state": build_state(tg_id)})
    return wrapper


def _want_id(request) -> int:
    try:
        return int(request.match_info["want_id"])
    except (KeyError, ValueError):
        raise ApiError("Неизвестная цель")


async def index(request: web.Request):
    path = find_index()
    if path is None:
        return web.Response(status=500, text="Нет файла index.html — загрузи папку webapp в репозиторий")
    return web.FileResponse(path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


async def health(request: web.Request):
    return web.Response(text="ok")


def build_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/state", api(lambda r, uid, b: {}))
    app.router.add_post("/api/income", api(lambda r, uid, b: do_income(uid, b)))
    app.router.add_post("/api/withdraw", api(lambda r, uid, b: do_withdraw(uid, b)))
    app.router.add_post("/api/wants", api(lambda r, uid, b: do_want_add(uid, b)))
    app.router.add_post("/api/wants/buy", api(lambda r, uid, b: do_want_buy(uid)))
    app.router.add_patch("/api/wants/{want_id}", api(lambda r, uid, b: do_want_edit(uid, _want_id(r), b)))
    app.router.add_delete("/api/wants/{want_id}", api(lambda r, uid, b: do_want_delete(uid, _want_id(r))))
    app.router.add_post("/api/settings", api(lambda r, uid, b: do_settings(uid, b)))
    return app


# ---------- бот ----------

def open_app_kb():
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💰 Открыть «Деньги»", web_app=WebAppInfo(url=WEBAPP_URL)),
    ]])


@router.message(CommandStart())
async def start(message: Message):
    cfg = get_config(message.from_user.id)
    text = (
        "Это копилка по принципу «плати сначала себе».\n\n"
        "С каждого поступления:\n"
        f"🔒 {cfg.rate:.0f}% — подушка безопасности (не трогать)\n"
        f"🎯 {cfg.want_rate:.0f}% — фонд хотелок (можно тратить без вины)\n"
        "💳 остальное — обычная жизнь\n\n"
    )
    kb = open_app_kb()
    if kb:
        text += "Всё управление — в приложении, кнопка ниже или «Деньги» слева от поля ввода."
    else:
        text += "Приложение пока не подключено: на сервере не задан WEBAPP_URL."
    await message.answer(text, reply_markup=kb)


@router.message()
async def any_message(message: Message):
    kb = open_app_kb()
    await message.answer(
        "Записывать доходы, хотелки и снятия теперь можно в приложении." if kb
        else "Приложение пока не подключено: на сервере не задан WEBAPP_URL.",
        reply_markup=kb,
    )


@router.callback_query()
async def old_buttons(callback: CallbackQuery):
    # кнопки от прошлой версии бота, оставшиеся в истории чата
    await callback.answer("Теперь всё в приложении «Деньги»")
    kb = open_app_kb()
    if kb and callback.message:
        await callback.message.answer("Открой приложение кнопкой ниже.", reply_markup=kb)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан — добавь его в переменные окружения")
    init_db()
    found = find_index()
    if found:
        log.info("Mini App файл: %s", found)
    else:
        # подробности для диагностики: что реально лежит рядом с ботом
        log.error("Нет файла index.html. Искал: %s", ", ".join(str(p) for p in INDEX_CANDIDATES))
        log.error("Содержимое %s: %s", BASE_DIR, sorted(x.name for x in BASE_DIR.iterdir()))

    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Веб-сервер запущен на порту %s", PORT)

    bot = Bot(BOT_TOKEN)
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Деньги", web_app=WebAppInfo(url=WEBAPP_URL))
            )
            log.info("Mini App: %s", WEBAPP_URL)
        except Exception:
            log.exception("Не удалось поставить кнопку «Деньги» в меню — проверь WEBAPP_URL")
    else:
        log.warning("WEBAPP_URL не задан — кнопка приложения в боте не появится")

    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
