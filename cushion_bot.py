import asyncio
import json
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from PIL import Image, ImageDraw, ImageFont

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
DB_PATH = "cushion.db"
DEFAULT_STAGES = [100_000, 500_000, 1_000_000]
DEFAULT_CURRENCY = "AMD"

logging.basicConfig(level=logging.INFO)
router = Router()


class Flow(StatesGroup):
    income_amount = State()
    withdraw_amount = State()
    withdraw_reason = State()
    wishlist_name = State()
    wishlist_price = State()
    set_rate = State()
    set_wantrate = State()
    set_bonusrate = State()
    set_expense = State()
    set_stages = State()
    set_currency = State()


# ---------- db ----------

def db():
    return sqlite3.connect(DB_PATH)


def init_db():
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
        cur = conn.execute(
            "SELECT rate, want_rate, windfall_rate, monthly_expense, stages_json, currency FROM config WHERE tg_id=?",
            (tg_id,),
        )
        row = cur.fetchone()
        if row is None:
            conn.execute("INSERT INTO config (tg_id) VALUES (?)", (tg_id,))
            return Config(20.0, 10.0, 50.0, 0.0, list(DEFAULT_STAGES), DEFAULT_CURRENCY)
        rate, want_rate, windfall_rate, expense, stages_json, currency = row
        try:
            stages_list = json.loads(stages_json)
        except (TypeError, ValueError):
            stages_list = list(DEFAULT_STAGES)
        return Config(rate, want_rate, windfall_rate, expense, stages_list, currency)


def update_config(tg_id: int, **fields):
    get_config(tg_id)
    if "stages" in fields:
        fields["stages_json"] = json.dumps(fields.pop("stages"))
    cols = ", ".join(f"{k}=?" for k in fields)
    with closing(db()) as conn, conn:
        conn.execute(f"UPDATE config SET {cols} WHERE tg_id=?", (*fields.values(), tg_id))


def total_saved(tg_id: int) -> float:
    with closing(db()) as conn:
        cur = conn.execute("SELECT COALESCE(SUM(saved),0) FROM entries WHERE tg_id=?", (tg_id,))
        return cur.fetchone()[0]


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


def withdraw_from_cushion(tg_id: int, amount: float, reason: str):
    add_entry(tg_id, amount, f"withdraw:{reason}" if reason else "withdraw", -amount, 0, 0)


def add_want(tg_id: int, name: str, price: float):
    with closing(db()) as conn, conn:
        pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM wants WHERE tg_id=?", (tg_id,)).fetchone()[0]
        conn.execute("INSERT INTO wants (tg_id, name, price, position) VALUES (?,?,?,?)", (tg_id, name, price, pos))


def list_wants(tg_id: int):
    with closing(db()) as conn:
        cur = conn.execute(
            "SELECT id, name, price, purchased FROM wants WHERE tg_id=? ORDER BY purchased, position", (tg_id,)
        )
        return cur.fetchall()


def active_want(tg_id: int):
    with closing(db()) as conn:
        cur = conn.execute(
            "SELECT id, name, price FROM wants WHERE tg_id=? AND purchased=0 ORDER BY position LIMIT 1", (tg_id,)
        )
        return cur.fetchone()


def mark_purchased(tg_id: int, want_id: int):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE wants SET purchased=1 WHERE id=? AND tg_id=?", (want_id, tg_id))


def drop_want(tg_id: int, want_id: int):
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM wants WHERE id=? AND tg_id=?", (want_id, tg_id))


def stages_for(tg_id: int):
    cfg = get_config(tg_id)
    stages = list(cfg.stages)
    if cfg.monthly_expense > 0:
        final_target = round(cfg.monthly_expense * 6)
        # добавляем как отдельный этап, только если он больше последнего —
        # иначе порядок этапов сломается (они должны идти по возрастанию)
        if not stages or final_target > stages[-1]:
            stages.append(final_target)
    return stages


def current_stage(tg_id: int):
    saved = total_saved(tg_id)
    stages = stages_for(tg_id)
    for i, target in enumerate(stages):
        if saved < target:
            return i + 1, target, saved, stages
    return len(stages), stages[-1], saved, stages


def fmt(n: float, currency: str) -> str:
    return f"{n:,.0f}".replace(",", " ") + f" {currency}"


def rebalance_hint(stage_no: int, n_stages: int, saved_total: float, target: float) -> str:
    if stage_no == n_stages and saved_total >= target:
        return (
            "\n\n🛡️ Подушка полностью сформирована! Хороший момент пересмотреть проценты — "
            "например снизить долю подушки и увеличить хотелки (в ⚙️ Настройках)."
        )
    return ""


# ---------- image ----------

def _font(size, bold=False):
    path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold \
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def make_bar_image(title: str, saved: float, target: float, accent, currency: str):
    pct = max(0.0, min(1.0, saved / target)) if target else 0
    W, H = 900, 340
    img = Image.new("RGB", (W, H), (12, 20, 32))
    draw = ImageDraw.Draw(img)
    title_font = _font(34, bold=True)
    sub_font = _font(26)
    pct_font = _font(56, bold=True)
    draw.text((40, 34), title, font=title_font, fill=(238, 243, 248))
    bar_x, bar_y, bar_w, bar_h = 40, 170, 820, 54
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=27, fill=(21, 34, 51))
    fill_w = int(bar_w * pct)
    if fill_w > 0:
        c1, c2 = accent
        for i in range(fill_w):
            t = i / max(fill_w, 1)
            r = int(c1[0] + t * (c2[0] - c1[0]))
            g = int(c1[1] + t * (c2[1] - c1[1]))
            b = int(c1[2] + t * (c2[2] - c1[2]))
            draw.line([(bar_x + i, bar_y), (bar_x + i, bar_y + bar_h)], fill=(r, g, b))
    draw.text((bar_x, bar_y + 76), f"{saved:,.0f} / {target:,.0f} {currency}".replace(",", " "),
              font=sub_font, fill=(200, 210, 220))
    pct_text = f"{pct*100:.0f}%"
    tw = draw.textlength(pct_text, font=pct_font)
    draw.text((W - 40 - tw, 90), pct_text, font=pct_font, fill=accent[0])
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


CUSHION_ACCENT = ((56, 224, 176), (79, 140, 255))
WANT_ACCENT = ((255, 184, 77), (255, 122, 89))


# ---------- keyboards ----------

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Доход", callback_data="menu:income")],
        [InlineKeyboardButton(text="📊 Статус", callback_data="menu:status"),
         InlineKeyboardButton(text="🎯 Хотелки", callback_data="menu:wishlist")],
        [InlineKeyboardButton(text="🔓 Снять деньги", callback_data="menu:withdraw"),
         InlineKeyboardButton(text="📜 Правило", callback_data="menu:rule")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")],
    ])


def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✕ Отмена", callback_data="menu:main")]])


def income_kind_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обычный доход", callback_data="income:regular"),
         InlineKeyboardButton(text="Неожиданно 🎁", callback_data="income:bonus")],
        [InlineKeyboardButton(text="✕ Отмена", callback_data="menu:main")],
    ])


def wishlist_kb(has_active: bool):
    rows = [[InlineKeyboardButton(text="➕ Добавить цель", callback_data="wishlist:add")]]
    if has_active:
        rows.append([
            InlineKeyboardButton(text="✅ Купить текущую", callback_data="wishlist:buy"),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="wishlist:skip"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="% в подушку", callback_data="settings:rate"),
         InlineKeyboardButton(text="% в хотелки", callback_data="settings:wantrate")],
        [InlineKeyboardButton(text="% для бонуса", callback_data="settings:bonusrate"),
         InlineKeyboardButton(text="Расходы/мес", callback_data="settings:expense")],
        [InlineKeyboardButton(text="Этапы подушки", callback_data="settings:stages"),
         InlineKeyboardButton(text="Валюта", callback_data="settings:currency")],
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="menu:main")],
    ])


def no_reason_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Без причины", callback_data="withdraw:noreason")],
        [InlineKeyboardButton(text="✕ Отмена", callback_data="menu:main")],
    ])


# ---------- helpers ----------

async def send_status(target_message: Message, tg_id: int):
    cfg = get_config(tg_id)
    stage_no, target, saved_total, stages = current_stage(tg_id)
    img = make_bar_image(f"Подушка — этап {stage_no} из {len(stages)}", saved_total, target, CUSHION_ACCENT, cfg.currency)
    caption = f"🔒 Подушка: {fmt(saved_total, cfg.currency)}, этап {stage_no} из {len(stages)}, цель — {fmt(target, cfg.currency)}"
    if cfg.monthly_expense > 0:
        caption += f"\nПокрывает {saved_total/cfg.monthly_expense:.1f} мес. расходов"
    caption += rebalance_hint(stage_no, len(stages), saved_total, target)
    await target_message.answer_photo(
        BufferedInputFile(img, filename="stage.png"), caption=caption, reply_markup=main_menu_kb()
    )
    aw = active_want(tg_id)
    if aw:
        _, name, price = aw
        wf = want_fund_balance(tg_id)
        img2 = make_bar_image(f"Хотелка: {name}", wf, price, WANT_ACCENT, cfg.currency)
        await target_message.answer_photo(
            BufferedInputFile(img2, filename="want.png"),
            caption=f"🎯 Фонд хотелок: {fmt(wf, cfg.currency)} из {fmt(price, cfg.currency)}",
        )


async def send_wishlist(target_message: Message, tg_id: int):
    cfg = get_config(tg_id)
    items = list_wants(tg_id)
    wf = want_fund_balance(tg_id)
    if not items:
        await target_message.answer(
            f"🎯 В фонде хотелок: {fmt(wf, cfg.currency)}\n\nОчередь пуста — добавь первую цель.",
            reply_markup=wishlist_kb(False),
        )
        return
    lines = [f"🎯 В фонде хотелок: {fmt(wf, cfg.currency)}\n"]
    for wid, name, price, purchased in items:
        mark = "✅" if purchased else "⏳"
        lines.append(f"{mark} {name} — {fmt(price, cfg.currency)}")
    await target_message.answer("\n".join(lines), reply_markup=wishlist_kb(active_want(tg_id) is not None))


def parse_amount(text: str):
    try:
        v = float(text.replace(",", ".").replace(" ", ""))
        return v if v > 0 else None
    except ValueError:
        return None


# ---------- entry points ----------

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    cfg = get_config(message.from_user.id)
    await message.answer(
        "Это бот по принципу «плати сначала себе», с двумя копилками.\n\n"
        f"С каждого поступления:\n"
        f"🔒 {cfg.rate:.0f}% — подушка безопасности (не трогать)\n"
        f"🎯 {cfg.want_rate:.0f}% — фонд хотелок (можно тратить без вины)\n"
        f"💳 остальное — обычная жизнь\n\n"
        "Выбирай действие кнопками ниже.",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "menu:main")
async def cb_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await send_status(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "menu:rule")
async def cb_rule(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "🔒 Правило неприкосновенности.\n\n"
        "Подушку можно трогать только на:\n"
        "— потерю работы / отсутствие дохода\n"
        "— серьёзную непредвиденную трату\n"
        "— заранее определённую большую цель\n\n"
        "Из подушки в хотелки — никогда. А из хотелок в подушку — можно "
        "(расхотел цель → «⏭ Пропустить», деньги остаются в фонде хотелок).\n\n"
        "Фонд хотелок можно и нужно тратить, когда цель накоплена — это не провал.",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


# ---------- income flow ----------

@router.callback_query(F.data == "menu:income")
async def cb_income(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.income_amount)
    await callback.message.answer("Сколько пришло? Напиши число.", reply_markup=cancel_kb())
    await callback.answer()


@router.message(Flow.income_amount)
async def income_amount_entered(message: Message, state: FSMContext):
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("Не понял сумму, попробуй ещё раз (например: 1000).", reply_markup=cancel_kb())
        return
    await state.update_data(amount=amount)
    await message.answer("Это обычный доход или неожиданные деньги?", reply_markup=income_kind_kb())


@router.callback_query(F.data.startswith("income:"))
async def cb_income_kind(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount = data.get("amount")
    if amount is None:
        await callback.answer("Сессия истекла, начни заново.", show_alert=True)
        return
    is_bonus = callback.data == "income:bonus"
    tg_id = callback.from_user.id
    cfg = get_config(tg_id)
    cushion_rate = cfg.windfall_rate if is_bonus else cfg.rate
    saved = round(amount * cushion_rate / 100, 2)
    want = round(amount * cfg.want_rate / 100, 2)
    spend = amount - saved - want
    add_entry(tg_id, amount, "bonus" if is_bonus else "regular", saved, want, spend)

    stage_no, target, saved_total, stages = current_stage(tg_id)
    img = make_bar_image(f"Подушка — этап {stage_no} из {len(stages)}", saved_total, target, CUSHION_ACCENT, cfg.currency)
    caption = (
        f"Пришло {fmt(amount, cfg.currency)}{' (неожиданно)' if is_bonus else ''}\n"
        f"🔒 В подушку ({cushion_rate:.0f}%): {fmt(saved, cfg.currency)}\n"
        f"🎯 В хотелки ({cfg.want_rate:.0f}%): {fmt(want, cfg.currency)}\n"
        f"💳 Можно тратить: {fmt(spend, cfg.currency)}\n\n"
        f"Подушка всего: {fmt(saved_total, cfg.currency)}"
    )
    aw = active_want(tg_id)
    if aw:
        _, name, price = aw
        wf = want_fund_balance(tg_id)
        caption += f"\nФонд хотелок: {fmt(wf, cfg.currency)} / {fmt(price, cfg.currency)} на «{name}»"
    caption += rebalance_hint(stage_no, len(stages), saved_total, target)

    await callback.message.answer_photo(
        BufferedInputFile(img, filename="stage.png"), caption=caption, reply_markup=main_menu_kb()
    )
    await state.clear()
    await callback.answer()


# ---------- withdraw flow ----------

@router.callback_query(F.data == "menu:withdraw")
async def cb_withdraw(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.withdraw_amount)
    await callback.message.answer(
        "Сколько снять? Помни: только на реальный форс-мажор, не на «захотелось».",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(Flow.withdraw_amount)
async def withdraw_amount_entered(message: Message, state: FSMContext):
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("Не понял сумму, попробуй ещё раз.", reply_markup=cancel_kb())
        return
    cfg = get_config(message.from_user.id)
    current = total_saved(message.from_user.id)
    if amount > current:
        await message.answer(f"В подушке только {fmt(current, cfg.currency)} — столько не снять.", reply_markup=main_menu_kb())
        await state.clear()
        return
    await state.update_data(amount=amount)
    await state.set_state(Flow.withdraw_reason)
    await message.answer("Причина? Можно написать текстом или нажать «Без причины».", reply_markup=no_reason_kb())


async def _finish_withdraw(tg_id: int, amount: float, reason: str, answer_target: Message):
    cfg = get_config(tg_id)
    withdraw_from_cushion(tg_id, amount, reason)
    saved_total = total_saved(tg_id)
    caption = f"🔓 Снято из подушки: {fmt(amount, cfg.currency)}"
    if reason:
        caption += f" — «{reason}»"
    caption += f"\nОсталось в подушке: {fmt(saved_total, cfg.currency)}\n\n"
    caption += "Если это правда форс-мажор — это не провал. Восстанови подушку через 💰 Доход, когда сможешь."
    await answer_target.answer(caption, reply_markup=main_menu_kb())


@router.message(Flow.withdraw_reason)
async def withdraw_reason_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get("amount")
    if amount is None:
        await state.clear()
        await message.answer("Сессия истекла, начни заново.", reply_markup=main_menu_kb())
        return
    await _finish_withdraw(message.from_user.id, amount, message.text.strip(), message)
    await state.clear()


@router.callback_query(F.data == "withdraw:noreason")
async def cb_withdraw_noreason(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount = data.get("amount")
    if amount is None:
        await callback.answer("Сессия истекла, начни заново.", show_alert=True)
        return
    await _finish_withdraw(callback.from_user.id, amount, "", callback.message)
    await state.clear()
    await callback.answer()


# ---------- wishlist flow ----------

@router.callback_query(F.data == "menu:wishlist")
async def cb_wishlist(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await send_wishlist(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "wishlist:add")
async def cb_wishlist_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.wishlist_name)
    await callback.message.answer("На что копим? Напиши название.", reply_markup=cancel_kb())
    await callback.answer()


@router.message(Flow.wishlist_name)
async def wishlist_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Напиши название текстом.", reply_markup=cancel_kb())
        return
    await state.update_data(name=name)
    await state.set_state(Flow.wishlist_price)
    await message.answer("Сколько это стоит?", reply_markup=cancel_kb())


@router.message(Flow.wishlist_price)
async def wishlist_price_entered(message: Message, state: FSMContext):
    price = parse_amount(message.text)
    if price is None:
        await message.answer("Не понял цену, попробуй ещё раз.", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    add_want(message.from_user.id, data["name"], price)
    await state.clear()
    cfg = get_config(message.from_user.id)
    await message.answer(f"Добавил «{data['name']}» за {fmt(price, cfg.currency)} в очередь.")
    await send_wishlist(message, message.from_user.id)


@router.callback_query(F.data == "wishlist:buy")
async def cb_wishlist_buy(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    cfg = get_config(tg_id)
    aw = active_want(tg_id)
    if not aw:
        await callback.answer("Очередь пуста.", show_alert=True)
        return
    wid, name, price = aw
    wf = want_fund_balance(tg_id)
    if wf < price:
        await callback.answer(f"Не хватает {fmt(price - wf, cfg.currency)}.", show_alert=True)
        return
    mark_purchased(tg_id, wid)
    await callback.message.answer(f"🎉 Куплено: «{name}» за {fmt(price, cfg.currency)}.")
    await send_wishlist(callback.message, tg_id)
    await callback.answer()


@router.callback_query(F.data == "wishlist:skip")
async def cb_wishlist_skip(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    aw = active_want(tg_id)
    if not aw:
        await callback.answer("Очередь пуста.", show_alert=True)
        return
    wid, name, price = aw
    drop_want(tg_id, wid)
    await callback.message.answer(f"«{name}» убрана из очереди — деньги остаются в фонде хотелок.")
    await send_wishlist(callback.message, tg_id)
    await callback.answer()


# ---------- settings ----------

SETTINGS_FIELDS = {
    "rate": (Flow.set_rate, "Новый % в подушку (1–100):"),
    "wantrate": (Flow.set_wantrate, "Новый % в хотелки (1–100):"),
    "bonusrate": (Flow.set_bonusrate, "Новый % для неожиданных денег (1–100):"),
    "expense": (Flow.set_expense, "Месячные расходы:"),
    "currency": (Flow.set_currency, "Новая валюта, например $, €, ֏ или AMD:"),
}


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    cfg = get_config(callback.from_user.id)
    stages_str = ", ".join(fmt(s, cfg.currency) for s in cfg.stages)
    await callback.message.answer(
        f"⚙️ Текущие настройки:\n"
        f"🔒 подушка: {cfg.rate:.0f}%\n"
        f"🎯 хотелки: {cfg.want_rate:.0f}%\n"
        f"🎁 неожиданные деньги: {cfg.windfall_rate:.0f}%\n"
        f"Месячные расходы: {fmt(cfg.monthly_expense, cfg.currency) if cfg.monthly_expense else 'не заданы'}\n"
        f"Этапы подушки: {stages_str}\n"
        f"Валюта: {cfg.currency}\n\n"
        "Что изменить?",
        reply_markup=settings_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:"))
async def cb_settings_field(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(":", 1)[1]
    if field == "stages":
        await state.set_state(Flow.set_stages)
        await callback.message.answer(
            "Суммы этапов через пробел, по возрастанию, например:\n100000 500000 1000000",
            reply_markup=cancel_kb(),
        )
        await callback.answer()
        return
    new_state, prompt = SETTINGS_FIELDS[field]
    await state.set_state(new_state)
    await state.update_data(field=field)
    await callback.message.answer(prompt, reply_markup=cancel_kb())
    await callback.answer()


@router.message(Flow.set_rate)
@router.message(Flow.set_wantrate)
@router.message(Flow.set_bonusrate)
@router.message(Flow.set_expense)
async def settings_number_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("field")
    if field not in SETTINGS_FIELDS:
        await state.clear()
        return
    value = parse_amount(message.text)
    if value is None:
        await message.answer("Не понял число, попробуй ещё раз.", reply_markup=cancel_kb())
        return
    key = {"rate": "rate", "wantrate": "want_rate", "bonusrate": "windfall_rate", "expense": "monthly_expense"}[field]
    if field in ("rate", "wantrate", "bonusrate") and not (0 < value <= 100):
        await message.answer("Процент должен быть от 1 до 100.", reply_markup=cancel_kb())
        return
    # подушка + хотелки не должны в сумме превышать 100% — иначе на жизнь останется отрицательная сумма
    cfg = get_config(message.from_user.id)
    if field == "rate" and value + cfg.want_rate > 100:
        max_allowed = 100 - cfg.want_rate
        await message.answer(
            f"Подушка + хотелки не может быть больше 100%. Сейчас хотелки — {cfg.want_rate:.0f}%, "
            f"значит для подушки максимум {max_allowed:.0f}%.",
            reply_markup=cancel_kb(),
        )
        return
    if field == "wantrate" and cfg.rate + value > 100:
        max_allowed = 100 - cfg.rate
        await message.answer(
            f"Подушка + хотелки не может быть больше 100%. Сейчас подушка — {cfg.rate:.0f}%, "
            f"значит для хотелок максимум {max_allowed:.0f}%.",
            reply_markup=cancel_kb(),
        )
        return
    update_config(message.from_user.id, **{key: value})
    await state.clear()
    await message.answer("Сохранил.", reply_markup=settings_kb())


@router.message(Flow.set_currency)
async def settings_currency_entered(message: Message, state: FSMContext):
    value = message.text.strip()[:8]
    if not value:
        await message.answer("Напиши валюту текстом, например $.", reply_markup=cancel_kb())
        return
    update_config(message.from_user.id, currency=value)
    await state.clear()
    await message.answer(f"Валюта теперь {value}.", reply_markup=settings_kb())


@router.message(Flow.set_stages)
async def settings_stages_entered(message: Message, state: FSMContext):
    try:
        values = [float(p.replace(",", ".").replace(" ", "")) for p in message.text.split()]
        assert values == sorted(values) and all(v > 0 for v in values) and len(values) >= 1
    except (ValueError, AssertionError):
        await message.answer("Не понял. Числа по возрастанию через пробел, например: 100000 500000 1000000", reply_markup=cancel_kb())
        return
    update_config(message.from_user.id, stages=[round(v) for v in values])
    await state.clear()
    await message.answer("Этапы подушки обновлены.", reply_markup=settings_kb())


async def main():
    init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
