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
import base64
import zlib
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


# Mini App встроен прямо в этот файл, поэтому папка webapp не обязательна.
# Если рядом есть webapp/index.html — используется он (удобно, если правишь дизайн).
APP_VERSION = "2026-10-03-embedded"
_EMBEDDED_INDEX = zlib.decompress(base64.b64decode(
    "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"
    "9w=="
)).decode("utf-8")


async def index(request: web.Request):
    path = find_index()
    html = path.read_text(encoding="utf-8") if path else _EMBEDDED_INDEX
    return web.Response(text=html, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


async def health(request: web.Request):
    # открой /health в браузере, чтобы увидеть, какая версия бота отвечает
    return web.Response(text=f"ok {APP_VERSION}")


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
    log.info("Версия бота: %s", APP_VERSION)
    log.info("Mini App: %s", f"файл {found}" if found else "встроенная копия")

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
