#!/usr/bin/env python3
"""
Telegram-бот для составления графика поваров.
Основной повар: каждый день 11:00–00:00 (230 ₽/ч)
Второй повар:   пт–вс 16:00–00:00 (170 ₽/ч)
Зарплата: 3 и 18 число каждого месяца.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()

# ===================== КОНФИГ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8994452967:AAF4KicGJX1NJvTU7zyzZXh3KJvd6IjpjMU")
ADMIN_USERNAMES = {
    u.strip().lstrip("@").lower()
    for u in os.getenv("ADMIN_USERNAMES", "jar_yo,rudiment01,kitt069").split(",")
    if u.strip()
}
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))

RATE_MAIN = 230  # ₽/час
RATE_SECOND = 170  # ₽/час

# Обязательные слоты
MAIN_START = time(11, 0)
MAIN_END = time(0, 0)  # полночь
SECOND_START = time(16, 0)
SECOND_END = time(0, 0)
SECOND_WEEKDAYS = {4, 5, 6}  # пт=4, сб=5, вс=6 (понедельник=0)

DB_PATH = "schedule.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ===================== БАЗА ДАННЫХ =====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT,
                telegram_id INTEGER UNIQUE,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS availability (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER,
                raw_text TEXT NOT NULL,
                weekday INTEGER,          -- 0=пн … 6=вс, NULL если неизвестно
                date_specific TEXT,      -- YYYY-MM-DD если указана конкретная дата
                start_time TEXT,         -- HH:MM
                end_time TEXT,           -- HH:MM
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER,
                FOREIGN KEY (worker_id) REFERENCES workers(id)
            );

            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_date TEXT NOT NULL, -- YYYY-MM-DD
                role TEXT NOT NULL,      -- 'main' | 'second'
                worker_id INTEGER,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                hours REAL,
                is_manual INTEGER DEFAULT 0,
                note TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (worker_id) REFERENCES workers(id)
            );

            CREATE TABLE IF NOT EXISTS bonuses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                period_start TEXT,       -- YYYY-MM-DD
                period_end TEXT,
                note TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER,
                FOREIGN KEY (worker_id) REFERENCES workers(id)
            );

            CREATE TABLE IF NOT EXISTS availability_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_text TEXT NOT NULL,
                from_user_id INTEGER,
                from_username TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await db.commit()


def calc_hours(start: time, end: time) -> float:
    """Считает часы между start и end (end может быть 00:00 = следующий день)."""
    s = datetime.combine(date.today(), start)
    e = datetime.combine(date.today(), end)
    if end <= start:  # переход через полночь
        e += timedelta(days=1)
    return round((e - s).total_seconds() / 3600, 2)


def role_rate(role: str) -> int:
    return RATE_MAIN if role == "main" else RATE_SECOND


def role_name(role: str) -> str:
    return "Основной повар" if role == "main" else "Второй повар"


def weekday_name(wd: int) -> str:
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return names[wd]


def get_salary_period(for_date: date | None = None) -> tuple[date, date]:
    """
    Возвращает (начало, конец) текущего зарплатного периода.
    Периоды: 3–17 и 18–2 (следующего месяца).
    """
    today = for_date or datetime.now(TIMEZONE).date()
    if today.day >= 18:
        start = date(today.year, today.month, 18)
        # конец = 2 следующего месяца
        if today.month == 12:
            end = date(today.year + 1, 1, 2)
        else:
            end = date(today.year, today.month + 1, 2)
    elif today.day >= 3:
        start = date(today.year, today.month, 3)
        end = date(today.year, today.month, 17)
    else:
        # 1–2 число → период с 18 предыдущего
        if today.month == 1:
            start = date(today.year - 1, 12, 18)
        else:
            start = date(today.year, today.month - 1, 18)
        end = date(today.year, today.month, 2)
    return start, end


# ===================== ПРОВЕРКА АДМИНА =====================
def is_admin(user) -> bool:
    if not user:
        return False
    username = (user.username or "").lower()
    return username in ADMIN_USERNAMES


# ===================== FSM =====================
class AddWorker(StatesGroup):
    name = State()
    username = State()


class AddAvailability(StatesGroup):
    worker = State()
    text = State()


class ManualShift(StatesGroup):
    date = State()
    role = State()
    worker = State()
    start = State()
    end = State()


class AddBonus(StatesGroup):
    worker = State()
    amount = State()
    note = State()


class EditShift(StatesGroup):
    shift_id = State()
    field = State()
    value = State()


# ===================== КЛАВИАТУРЫ =====================
def main_kb(is_admin_user: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📅 График на неделю", callback_data="schedule_week")],
        [InlineKeyboardButton(text="💰 Моя / Зарплата за период", callback_data="salary_period")],
        [InlineKeyboardButton(text="📋 Список работников", callback_data="workers_list")],
    ]
    if is_admin_user:
        rows.extend(
            [
                [InlineKeyboardButton(text="➕ Добавить работника", callback_data="add_worker")],
                [InlineKeyboardButton(text="📝 Добавить доступность (текст)", callback_data="add_avail")],
                [InlineKeyboardButton(text="📨 Сообщения о доступности", callback_data="avail_msgs")],
                [InlineKeyboardButton(text="⚙️ Сформировать график", callback_data="generate_schedule")],
                [InlineKeyboardButton(text="✏️ Ручное редактирование смены", callback_data="manual_shift")],
                [InlineKeyboardButton(text="🎁 Добавить премию", callback_data="add_bonus")],
                [InlineKeyboardButton(text="📊 Зарплаты всех за период", callback_data="all_salaries")],
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]]
    )


def workers_kb(workers: list, prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=w["name"], callback_data=f"{prefix}:{w['id']}")]
        for w in workers
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ===================== РОУТЕР =====================
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    admin = is_admin(message.from_user)
    text = (
        "👋 Привет! Я бот для графика поваров.\n\n"
        f"Основной повар: каждый день 11:00–00:00 — {RATE_MAIN} ₽/ч\n"
        f"Второй повар: пт–вс 16:00–00:00 — {RATE_SECOND} ₽/ч\n\n"
        "Зарплата считается за периоды 3–17 и 18–2 числа."
    )
    if admin:
        text += "\n\n🔑 Ты админ — доступны все функции."
    await message.answer(text, reply_markup=main_kb(admin))


@router.callback_query(F.data == "cancel")
async def cancel_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Отменено.", reply_markup=main_kb(is_admin(callback.from_user))
    )
    await callback.answer()


@router.callback_query(F.data == "menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Главное меню:", reply_markup=main_kb(is_admin(callback.from_user))
    )
    await callback.answer()


# ---------- Работники ----------
@router.callback_query(F.data == "workers_list")
async def workers_list(callback: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, name, username, active FROM workers ORDER BY name"
        )
        rows = await cur.fetchall()
    if not rows:
        text = "Работников пока нет."
    else:
        lines = []
        for r in rows:
            status = "✅" if r["active"] else "❌"
            un = f" @{r['username']}" if r["username"] else ""
            lines.append(f"{status} {r['name']}{un} (id={r['id']})")
        text = "📋 Работники:\n\n" + "\n".join(lines)
    await callback.message.edit_text(
        text, reply_markup=main_kb(is_admin(callback.from_user))
    )
    await callback.answer()


@router.callback_query(F.data == "add_worker")
async def add_worker_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("Только для админов", show_alert=True)
        return
    await state.set_state(AddWorker.name)
    await callback.message.edit_text(
        "Введи имя работника:", reply_markup=cancel_kb()
    )
    await callback.answer()


@router.message(AddWorker.name)
async def add_worker_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddWorker.username)
    await message.answer(
        "Введи @username (или «-» если нет):", reply_markup=cancel_kb()
    )


@router.message(AddWorker.username)
async def add_worker_username(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["name"]
    username = message.text.strip().lstrip("@")
    if username == "-":
        username = None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO workers (name, username) VALUES (?, ?)", (name, username)
        )
        await db.commit()
    await state.clear()
    await message.answer(
        f"✅ Работник «{name}» добавлен.",
        reply_markup=main_kb(is_admin(message.from_user)),
    )


# ---------- Доступность (сообщения) ----------
@router.message(F.text & ~F.text.startswith("/"))
async def catch_availability_message(message: Message):
    """Любое текстовое сообщение (не команда) сохраняем как заметку о доступности."""
    if not message.text or len(message.text) < 3:
        return
    # Не мешаем FSM
    # (FSM обрабатывается приоритетнее, но на всякий случай)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO availability_messages (message_text, from_user_id, from_username)
            VALUES (?, ?, ?)
            """,
            (
                message.text,
                message.from_user.id if message.from_user else None,
                message.from_user.username if message.from_user else None,
            ),
        )
        await db.commit()
    # Если админ — подтверждаем, иначе просто молча сохраняем
    if is_admin(message.from_user):
        await message.reply("✅ Сообщение о доступности сохранено.")


@router.callback_query(F.data == "avail_msgs")
async def show_avail_msgs(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("Только для админов", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, message_text, from_username, created_at
            FROM availability_messages
            ORDER BY id DESC LIMIT 30
            """
        )
        rows = await cur.fetchall()
    if not rows:
        text = "Пока нет сообщений о доступности."
    else:
        lines = []
        for r in rows:
            un = f"@{r['from_username']}" if r["from_username"] else "—"
            lines.append(f"#{r['id']} ({r['created_at'][:16]}) {un}:\n{r['message_text'][:200]}")
        text = "📨 Последние сообщения о доступности:\n\n" + "\n\n".join(lines)
    await callback.message.edit_text(
        text[:4000], reply_markup=main_kb(True)
    )
    await callback.answer()


@router.callback_query(F.data == "add_avail")
async def add_avail_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("Только для админов", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, name FROM workers WHERE active=1 ORDER BY name"
        )
        workers = [dict(r) for r in await cur.fetchall()]
    if not workers:
        await callback.answer("Сначала добавь работников", show_alert=True)
        return
    await state.set_state(AddAvailability.worker)
    await callback.message.edit_text(
        "Выбери работника:", reply_markup=workers_kb(workers, "avail_w")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("avail_w:"))
async def add_avail_worker(callback: CallbackQuery, state: FSMContext):
    worker_id = int(callback.data.split(":")[1])
    await state.update_data(worker_id=worker_id)
    await state.set_state(AddAvailability.text)
    await callback.message.edit_text(
        "Напиши доступность (например:\n"
        "пн 12:00-18:00\n"
        "ср 14-22\n"
        "или просто свободный текст):",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AddAvailability.text)
async def add_avail_text(message: Message, state: FSMContext):
    data = await state.get_data()
    worker_id = data["worker_id"]
    text = message.text.strip()
    # Простой парсер: ищем "пн|вт|...  HH:MM-HH:MM"
    day_map = {
        "пн": 0, "пон": 0, "вт": 1, "ср": 2, "чт": 3,
        "пт": 4, "сб": 5, "вс": 6, "воскр": 6,
    }
    parsed = False
    async with aiosqlite.connect(DB_PATH) as db:
        for line in text.splitlines():
            line = line.lower().strip()
            m = re.search(
                r"(пн|пон|вт|ср|чт|пт|сб|вс|воскр)\w*\s+(\d{1,2})[:.]?(\d{0,2})\s*[-–]\s*(\d{1,2})[:.]?(\d{0,2})",
                line,
            )
            if m:
                wd = day_map.get(m.group(1)[:2] if m.group(1)[:2] in day_map else m.group(1)[:3], None)
                if wd is None:
                    for k, v in day_map.items():
                        if m.group(1).startswith(k):
                            wd = v
                            break
                if wd is not None:
                    h1 = int(m.group(2))
                    m1 = int(m.group(3) or 0)
                    h2 = int(m.group(4))
                    m2 = int(m.group(5) or 0)
                    start_t = f"{h1:02d}:{m1:02d}"
                    end_t = f"{h2:02d}:{m2:02d}"
                    await db.execute(
                        """
                        INSERT INTO availability
                        (worker_id, raw_text, weekday, start_time, end_time, created_by)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (worker_id, line, wd, start_t, end_t, message.from_user.id),
                    )
                    parsed = True
        if not parsed:
            await db.execute(
                """
                INSERT INTO availability
                (worker_id, raw_text, created_by)
                VALUES (?, ?, ?)
                """,
                (worker_id, text, message.from_user.id),
            )
        await db.commit()
    await state.clear()
    await message.answer(
        "✅ Доступность сохранена" + (" (распарсена)" if parsed else " (как текст)"),
        reply_markup=main_kb(is_admin(message.from_user)),
    )


# ---------- Генерация графика ----------
@router.callback_query(F.data == "generate_schedule")
async def generate_schedule(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("Только для админов", show_alert=True)
        return
    await callback.answer("Формирую…")
    today = datetime.now(TIMEZONE).date()
    # Генерируем на текущую + следующую неделю (14 дней)
    start_d = today
    end_d = today + timedelta(days=13)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Берём активных работников
        cur = await db.execute("SELECT id, name FROM workers WHERE active=1")
        workers = {r["id"]: r["name"] for r in await cur.fetchall()}
        if not workers:
            await callback.message.edit_text(
                "Нет активных работников.", reply_markup=main_kb(True)
            )
            return

        # Структурированная доступность
        cur = await db.execute(
            """
            SELECT worker_id, weekday, start_time, end_time
            FROM availability
            WHERE weekday IS NOT NULL AND start_time IS NOT NULL
            """
        )
        avail_list = await cur.fetchall()

        # Удаляем старые автоматические смены в периоде (ручные оставляем)
        await db.execute(
            """
            DELETE FROM shifts
            WHERE work_date >= ? AND work_date <= ? AND is_manual = 0
            """,
            (start_d.isoformat(), end_d.isoformat()),
        )

        # Собираем уже существующие ручные смены
        cur = await db.execute(
            """
            SELECT work_date, role FROM shifts
            WHERE work_date >= ? AND work_date <= ? AND is_manual = 1
            """,
            (start_d.isoformat(), end_d.isoformat()),
        )
        existing_manual = {(r["work_date"], r["role"]) for r in await cur.fetchall()}

        assigned = 0
        d = start_d
        while d <= end_d:
            wd = d.weekday()
            # Основной слот
            if (d.isoformat(), "main") not in existing_manual:
                candidate = None
                for a in avail_list:
                    if a["weekday"] == wd:
                        # Проверяем пересечение с 11:00–00:00
                        # Упрощённо: если человек указал этот день — берём
                        candidate = a["worker_id"]
                        break
                # Если нет структурированной — берём первого попавшегося (или оставляем пустым)
                if candidate is None and workers:
                    # Можно оставить пустым, чтобы админ назначил вручную
                    pass
                if candidate:
                    hours = calc_hours(MAIN_START, MAIN_END)
                    await db.execute(
                        """
                        INSERT INTO shifts
                        (work_date, role, worker_id, start_time, end_time, hours, is_manual)
                        VALUES (?, 'main', ?, ?, ?, ?, 0)
                        """,
                        (
                            d.isoformat(),
                            candidate,
                            MAIN_START.strftime("%H:%M"),
                            "00:00",
                            hours,
                        ),
                    )
                    assigned += 1

            # Второй повар (пт–вс)
            if wd in SECOND_WEEKDAYS and (d.isoformat(), "second") not in existing_manual:
                candidate = None
                for a in avail_list:
                    if a["weekday"] == wd and a["worker_id"] != candidate:
                        candidate = a["worker_id"]
                        break
                if candidate:
                    hours = calc_hours(SECOND_START, SECOND_END)
                    await db.execute(
                        """
                        INSERT INTO shifts
                        (work_date, role, worker_id, start_time, end_time, hours, is_manual)
                        VALUES (?, 'second', ?, ?, ?, ?, 0)
                        """,
                        (
                            d.isoformat(),
                            candidate,
                            SECOND_START.strftime("%H:%M"),
                            "00:00",
                            hours,
                        ),
                    )
                    assigned += 1
            d += timedelta(days=1)
        await db.commit()

    await callback.message.edit_text(
        f"✅ График сформирован на {start_d.strftime('%d.%m')}–{end_d.strftime('%d.%m')}.\n"
        f"Назначено автоматических смен: {assigned}.\n"
        "Ручные смены сохранены. Можно править вручную.",
        reply_markup=main_kb(True),
    )


# ---------- Просмотр графика ----------
@router.callback_query(F.data == "schedule_week")
async def schedule_week(callback: CallbackQuery):
    today = datetime.now(TIMEZONE).date()
    # показываем 7 дней от сегодня
    end = today + timedelta(days=6)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT s.work_date, s.role, s.start_time, s.end_time, s.hours,
                   s.worker_id, w.name, s.is_manual, s.id
            FROM shifts s
            LEFT JOIN workers w ON w.id = s.worker_id
            WHERE s.work_date >= ? AND s.work_date <= ?
            ORDER BY s.work_date, s.role
            """,
            (today.isoformat(), end.isoformat()),
        )
        rows = await cur.fetchall()

    if not rows:
        text = (
            f"📅 График на {today.strftime('%d.%m')}–{end.strftime('%d.%m')}:\n\n"
            "Пока пусто. Админ может нажать «Сформировать график» или назначить вручную."
        )
    else:
        by_date: dict[str, list] = {}
        for r in rows:
            by_date.setdefault(r["work_date"], []).append(r)
        lines = [f"📅 График {today.strftime('%d.%m')}–{end.strftime('%d.%m')}:\n"]
        for d_str in sorted(by_date.keys()):
            d = date.fromisoformat(d_str)
            lines.append(f"\n<b>{weekday_name(d.weekday())} {d.strftime('%d.%m')}</b>")
            for r in by_date[d_str]:
                name = r["name"] or "— не назначен —"
                manual = " ✏️" if r["is_manual"] else ""
                pay = int(r["hours"] * role_rate(r["role"])) if r["hours"] else 0
                lines.append(
                    f"  {role_name(r['role'])}: {name} "
                    f"{r['start_time']}–{r['end_time']} "
                    f"({r['hours']}ч ≈ {pay}₽){manual}"
                )
        text = "\n".join(lines)
    await callback.message.edit_text(
        text, reply_markup=main_kb(is_admin(callback.from_user)), parse_mode="HTML"
    )
    await callback.answer()


# ---------- Ручное назначение ----------
@router.callback_query(F.data == "manual_shift")
async def manual_shift_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("Только для админов", show_alert=True)
        return
    await state.set_state(ManualShift.date)
    await callback.message.edit_text(
        "Введи дату смены в формате ДД.ММ.ГГГГ (или ДД.ММ):",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(ManualShift.date)
async def manual_shift_date(message: Message, state: FSMContext):
    raw = message.text.strip()
    try:
        if len(raw.split(".")) == 2:
            d, m = map(int, raw.split("."))
            y = datetime.now(TIMEZONE).year
            work_date = date(y, m, d)
        else:
            d, m, y = map(int, raw.split("."))
            work_date = date(y, m, d)
    except Exception:
        await message.answer("Не понял дату. Пример: 20.10 или 20.10.2026")
        return
    await state.update_data(work_date=work_date.isoformat())
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Основной повар", callback_data="ms_role:main"),
                InlineKeyboardButton(text="Второй повар", callback_data="ms_role:second"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ]
    )
    await state.set_state(ManualShift.role)
    await message.answer("Выбери роль:", reply_markup=kb)


@router.callback_query(F.data.startswith("ms_role:"), ManualShift.role)
async def manual_shift_role(callback: CallbackQuery, state: FSMContext):
    role = callback.data.split(":")[1]
    await state.update_data(role=role)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, name FROM workers WHERE active=1 ORDER BY name"
        )
        workers = [dict(r) for r in await cur.fetchall()]
    if not workers:
        await callback.message.edit_text("Нет работников.", reply_markup=main_kb(True))
        await state.clear()
        return
    await state.set_state(ManualShift.worker)
    await callback.message.edit_text(
        "Выбери работника:", reply_markup=workers_kb(workers, "ms_w")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ms_w:"), ManualShift.worker)
async def manual_shift_worker(callback: CallbackQuery, state: FSMContext):
    worker_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    role = data["role"]
    # Стандартное время
    if role == "main":
        st, et = "11:00", "00:00"
        hours = calc_hours(MAIN_START, MAIN_END)
    else:
        st, et = "16:00", "00:00"
        hours = calc_hours(SECOND_START, SECOND_END)

    async with aiosqlite.connect(DB_PATH) as db:
        # Удаляем предыдущую смену этой роли в этот день (если была)
        await db.execute(
            "DELETE FROM shifts WHERE work_date=? AND role=?",
            (data["work_date"], role),
        )
        await db.execute(
            """
            INSERT INTO shifts
            (work_date, role, worker_id, start_time, end_time, hours, is_manual)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (data["work_date"], role, worker_id, st, et, hours),
        )
        await db.commit()
        cur = await db.execute("SELECT name FROM workers WHERE id=?", (worker_id,))
        row = await cur.fetchone()
        name = row[0] if row else "?"

    await state.clear()
    await callback.message.edit_text(
        f"✅ Назначено: {data['work_date']} — {role_name(role)} — {name}\n"
        f"{st}–{et} ({hours} ч)",
        reply_markup=main_kb(True),
    )
    await callback.answer()


# ---------- Зарплата ----------
@router.callback_query(F.data == "salary_period")
async def salary_period(callback: CallbackQuery):
    start, end = get_salary_period()
    user = callback.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Ищем работника по username или telegram_id
        cur = await db.execute(
            "SELECT id, name FROM workers WHERE username=? OR telegram_id=?",
            ((user.username or "").lower(), user.id),
        )
        worker = await cur.fetchone()
        if not worker and is_admin(user):
            # Админу показываем подсказку
            await callback.message.edit_text(
                "Ты админ. Чтобы посмотреть свою зарплату — добавь себя как работника "
                "(с твоим @username).\n"
                "Или нажми «Зарплаты всех за период».",
                reply_markup=main_kb(True),
            )
            await callback.answer()
            return
        if not worker:
            await callback.message.edit_text(
                "Ты не найден в списке работников. Попроси админа добавить тебя.",
                reply_markup=main_kb(False),
            )
            await callback.answer()
            return
        worker_id = worker["id"]
        name = worker["name"]

        cur = await db.execute(
            """
            SELECT work_date, role, hours, start_time, end_time
            FROM shifts
            WHERE worker_id=? AND work_date >= ? AND work_date <= ?
            ORDER BY work_date
            """,
            (worker_id, start.isoformat(), end.isoformat()),
        )
        shifts = await cur.fetchall()

        cur = await db.execute(
            """
            SELECT amount, note, created_at FROM bonuses
            WHERE worker_id=? AND (
                (period_start IS NULL AND period_end IS NULL)
                OR (period_start <= ? AND period_end >= ?)
                OR (period_start IS NULL AND period_end >= ?)
                OR (period_end IS NULL AND period_start <= ?)
            )
            """,
            (worker_id, end.isoformat(), start.isoformat(), start.isoformat(), end.isoformat()),
        )
        bonuses = await cur.fetchall()

    total_hours = 0.0
    total_pay = 0
    lines = [
        f"💰 Зарплата: <b>{name}</b>",
        f"Период: {start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')}\n",
    ]
    if shifts:
        lines.append("<b>Смены:</b>")
        for s in shifts:
            pay = int(s["hours"] * role_rate(s["role"]))
            total_hours += s["hours"]
            total_pay += pay
            lines.append(
                f"  {s['work_date']} {role_name(s['role'])}: "
                f"{s['start_time']}–{s['end_time']} ({s['hours']}ч) = {pay}₽"
            )
    else:
        lines.append("Смен в этом периоде нет.")

    bonus_sum = 0
    if bonuses:
        lines.append("\n<b>Премии:</b>")
        for b in bonuses:
            bonus_sum += b["amount"]
            note = f" ({b['note']})" if b["note"] else ""
            lines.append(f"  +{int(b['amount'])}₽{note}")

    total_pay += int(bonus_sum)
    lines.append(f"\n⏱ Всего часов: {total_hours}")
    lines.append(f"💵 Итого к выплате: <b>{total_pay} ₽</b>")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=main_kb(is_admin(user)),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "all_salaries")
async def all_salaries(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("Только для админов", show_alert=True)
        return
    start, end = get_salary_period()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, name FROM workers WHERE active=1")
        workers = await cur.fetchall()
        lines = [
            f"📊 Зарплаты за период {start.strftime('%d.%m')}–{end.strftime('%d.%m')}:\n"
        ]
        for w in workers:
            cur = await db.execute(
                """
                SELECT role, SUM(hours) as h
                FROM shifts
                WHERE worker_id=? AND work_date >= ? AND work_date <= ?
                GROUP BY role
                """,
                (w["id"], start.isoformat(), end.isoformat()),
            )
            roles = await cur.fetchall()
            pay = 0
            hours_info = []
            for r in roles:
                p = int((r["h"] or 0) * role_rate(r["role"]))
                pay += p
                hours_info.append(f"{role_name(r['role'])}: {r['h']}ч")
            cur = await db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM bonuses WHERE worker_id=?",
                (w["id"],),
            )
            bonus = (await cur.fetchone())[0] or 0
            pay += int(bonus)
            info = ", ".join(hours_info) if hours_info else "нет смен"
            bonus_txt = f" + премия {int(bonus)}₽" if bonus else ""
            lines.append(f"• {w['name']}: {pay}₽ ({info}{bonus_txt})")
    await callback.message.edit_text(
        "\n".join(lines)[:4000], reply_markup=main_kb(True)
    )
    await callback.answer()


# ---------- Премии ----------
@router.callback_query(F.data == "add_bonus")
async def add_bonus_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("Только для админов", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, name FROM workers WHERE active=1 ORDER BY name"
        )
        workers = [dict(r) for r in await cur.fetchall()]
    if not workers:
        await callback.answer("Нет работников", show_alert=True)
        return
    await state.set_state(AddBonus.worker)
    await callback.message.edit_text(
        "Кому добавить премию?", reply_markup=workers_kb(workers, "bonus_w")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bonus_w:"), AddBonus.worker)
async def add_bonus_worker(callback: CallbackQuery, state: FSMContext):
    worker_id = int(callback.data.split(":")[1])
    await state.update_data(worker_id=worker_id)
    await state.set_state(AddBonus.amount)
    await callback.message.edit_text(
        "Введи сумму премии (число):", reply_markup=cancel_kb()
    )
    await callback.answer()


@router.message(AddBonus.amount)
async def add_bonus_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", ".").replace(" ", ""))
    except ValueError:
        await message.answer("Введи число, например 1500")
        return
    await state.update_data(amount=amount)
    await state.set_state(AddBonus.note)
    await message.answer(
        "Комментарий к премии (или «-»):", reply_markup=cancel_kb()
    )


@router.message(AddBonus.note)
async def add_bonus_note(message: Message, state: FSMContext):
    data = await state.get_data()
    note = message.text.strip()
    if note == "-":
        note = None
    start, end = get_salary_period()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bonuses
            (worker_id, amount, period_start, period_end, note, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                data["worker_id"],
                data["amount"],
                start.isoformat(),
                end.isoformat(),
                note,
                message.from_user.id,
            ),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT name FROM workers WHERE id=?", (data["worker_id"],)
        )
        name = (await cur.fetchone())[0]
    await state.clear()
    await message.answer(
        f"✅ Премия {int(data['amount'])}₽ добавлена работнику {name}.",
        reply_markup=main_kb(True),
    )


# ---------- Команды ----------
@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(
        f"Твой Telegram ID: `{message.from_user.id}`\n"
        f"Username: @{message.from_user.username or '—'}",
        parse_mode="Markdown",
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Команды:\n"
        "/start — главное меню\n"
        "/id — узнать свой Telegram ID\n"
        "/help — эта справка\n\n"
        "Любое текстовое сообщение сохраняется как заметка о доступности."
    )


# ===================== ЗАПУСК =====================
async def main():
    if not BOT_TOKEN or "AAF" not in BOT_TOKEN:
        logger.error("Не указан BOT_TOKEN")
        return
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Бот запущен. Админы: %s", ADMIN_USERNAMES)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
