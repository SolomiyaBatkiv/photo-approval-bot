"""Telegram-бот для апруву фото.

Схема:
  1. Виконавець тисне «Згенерувати фото» -> відкривається Higgsfield.
  2. Генерує фото на сайті й надсилає його в бот (фото або файлом),
     бот питає стать: Male / Female.
  3. Бот пересилає фото апруверу з кнопками ✅ / ❌.
  4. ✅ -> виконавцю приходить «Прийнято»; фото йде на Google Drive
          (підпапка male / female), рядок (Manager, Notes, Added Date, Link, Gender)
          — у Google Таблицю.
     ❌ -> апрувер пише коментар -> він приходить виконавцю.
"""

import asyncio
import base64
import html
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyParameters
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
_approver = os.getenv("APPROVER_ID", "").strip()
APPROVER_ID = int(_approver) if _approver.isdigit() else 0  # 0 = ще не налаштовано
# Порожньо = ботом може користуватися будь-хто
ALLOWED_USERS = {
    int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x
}
# Менеджери: ID:Ім'я через кому. Ім'я піде в колонку Manager.
# Якщо задано — ботом можуть користуватися тільки ці люди (+ ALLOWED_USERS).
MANAGERS: dict[int, str] = {}
for _item in os.getenv("MANAGERS", "").split(","):
    if ":" in _item:
        _id, _name = _item.split(":", 1)
        if _id.strip().isdigit():
            MANAGERS[int(_id.strip())] = _name.strip()
GENERATOR_URL = os.getenv(
    "GENERATOR_URL", "https://higgsfield.ai/ai/image?model=nano-banana-pro"
)
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Google Таблиця + Drive (через Apps Script, див. apps_script.gs)
SHEETS_WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL", "")
SHEETS_SECRET = os.getenv("SHEETS_SECRET", "")

GENDERS = {"male": "♂ Male", "female": "♀ Female"}

router = Router()


# ---------- База ----------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                user_name       TEXT,
                user_msg_id     INTEGER,
                file_id         TEXT NOT NULL,
                file_kind       TEXT NOT NULL,      -- photo | document
                caption         TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
                comment         TEXT,
                approver_msg_id INTEGER,
                created_at      TEXT NOT NULL,
                decided_at      TEXT
            )
            """
        )
        # Нові колонки (додаються й до вже існуючої бази)
        new_cols = {
            "mime_type": "TEXT",
            "file_name": "TEXT",
            "thumb_file_id": "TEXT",
            "gender": "TEXT",
            "drive_link": "TEXT",
            "archived": "INTEGER NOT NULL DEFAULT 0",
            "approver_chat_id": "INTEGER",
        }
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(submissions)")}
        for col, typ in new_cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {col} {typ}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS managers (
                user_id    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                username   TEXT,
                status     TEXT NOT NULL,   -- pending | active
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        # Апрувер, призначений командою /approver, має пріоритет над .env
        row = conn.execute("SELECT value FROM settings WHERE key='approver_id'").fetchone()
    global APPROVER_ID
    if row and row["value"].isdigit():
        APPROVER_ID = int(row["value"])


def set_approver(user_id: int) -> None:
    global APPROVER_ID
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('approver_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(user_id),),
        )
    APPROVER_ID = user_id


def from_approver(event) -> bool:
    """Фільтр: подія від поточного апрувера (читає значення під час виклику)."""
    return bool(APPROVER_ID) and event.from_user.id == APPROVER_ID


def get_manager(user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM managers WHERE user_id = ?", (user_id,)).fetchone()


def get_submission(sub_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- Клавіатури ----------

def start_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎨 Згенерувати фото", url=GENERATOR_URL)
    return kb.as_markup()


def review_kb(sub_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Approve", callback_data=f"ok:{sub_id}")
    kb.button(text="❌ Reject", callback_data=f"no:{sub_id}")
    kb.adjust(2)
    return kb.as_markup()


def gender_kb(sub_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text=GENDERS["male"], callback_data=f"g:male:{sub_id}")
    kb.button(text=GENDERS["female"], callback_data=f"g:female:{sub_id}")
    kb.adjust(2)
    return kb.as_markup()


def gender_label(g: str | None) -> str:
    return GENDERS.get(g or "", "—")


class Review(StatesGroup):
    waiting_comment = State()


class Register(StatesGroup):
    waiting_name = State()


def is_allowed(user_id: int) -> bool:
    if user_id == APPROVER_ID or user_id in MANAGERS or user_id in ALLOWED_USERS:
        return True
    m = get_manager(user_id)
    return bool(m and m["status"] == "active")


def has_name(user_id: int) -> bool:
    return user_id in MANAGERS or get_manager(user_id) is not None


def manager_name(user_id: int, fallback: str | None) -> str:
    if user_id in MANAGERS:
        return MANAGERS[user_id]
    m = get_manager(user_id)
    if m and m["name"]:
        return m["name"]
    return fallback or str(user_id)


def start_text(user_id: int) -> str:
    text = (
        "Як це працює:\n\n"
        "1. Натисніть «🎨 Згенерувати фото» і створіть фото на сайті.\n"
        "2. Завантажте його й надішліть сюди <b>файлом</b> (щоб не втратити якість) або як фото. "
        "У підписі можна додати нотатку — вона потрапить у таблицю.\n"
        "3. Оберіть стать: Male або Female.\n"
        "4. Фото піде на апрув, відповідь прийде сюди ж."
    )
    if user_id == APPROVER_ID:
        text += (
            "\n\n<i>Ви апрувер: заявки приходитимуть вам з кнопками ✅ / ❌.\n"
            "/managers — список менеджерів\n"
            "/approver — передати роль апрувера іншому менеджеру\n"
            "/resync — повторно записати в таблицю фото, які не записались.</i>"
        )
    return text


def user_label(sub: sqlite3.Row) -> str:
    name = html.escape(manager_name(sub["user_id"], sub["user_name"]))
    return f'<a href="tg://user?id={sub["user_id"]}">{name}</a>'


# ---------- Google Drive + Таблиця ----------

async def save_to_sheet(sub: sqlite3.Row, data: bytes, gender: str) -> str:
    payload = {
        "secret": SHEETS_SECRET,
        "fileName": f"{sub['id']}_{sub['file_name'] or 'photo.jpg'}",
        "mimeType": sub["mime_type"] or "image/jpeg",
        "data": base64.b64encode(data).decode(),
        "manager": manager_name(sub["user_id"], sub["user_name"]),
        "notes": sub["caption"] or "",
        "gender": gender,
    }
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(SHEETS_WEBHOOK_URL, json=payload) as r:
            status, text = r.status, await r.text()
    try:
        res = json.loads(text)
    except ValueError:
        raise RuntimeError(f"Apps Script відповів не JSON (HTTP {status}): {text[:200]}")
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "невідома помилка"))
    return res["url"]


async def archive(bot: Bot, sub_id: int) -> bool:
    """Після апруву: Drive (папка male/female) -> рядок у таблиці. True, якщо записано."""
    if not SHEETS_WEBHOOK_URL:
        return False
    sub = get_submission(sub_id)
    try:
        data = (await bot.download(sub["file_id"])).getvalue()
        gender = sub["gender"] or ""
        link = await save_to_sheet(sub, data, gender)
        with db() as conn:
            conn.execute(
                "UPDATE submissions SET archived=1, drive_link=? WHERE id=?", (link, sub_id)
            )
        await bot.send_message(
            APPROVER_ID,
            f"📊 #{sub_id} записано в таблицю · {gender_label(gender)} · "
            f'<a href="{html.escape(link)}">Drive</a>',
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        logging.exception("Архівація #%s не вдалася", sub_id)
        await bot.send_message(
            APPROVER_ID,
            f"⚠️ #{sub_id} не записано в таблицю: {html.escape(str(e))[:300]}\n"
            "Спробувати ще раз: /resync",
        )
        return False


# ---------- Команди ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    uid = message.from_user.id
    await state.clear()

    if not APPROVER_ID:
        await message.answer(
            "⚙️ Бот ще не налаштований.\n\n"
            f"Ваш ID: <code>{uid}</code>\n"
            "Якщо апрувер — ви, впишіть у .env рядок\n"
            f"<code>APPROVER_ID={uid}</code>\nі перезапустіть бота."
        )
        return

    m = get_manager(uid)
    if m and m["status"] == "pending":
        await message.answer("⏳ Вашу заявку на доступ уже надіслано. Чекайте підтвердження.")
        return

    # Новий користувач (або апрувер без імені) — реєстрація
    if not has_name(uid) and (uid == APPROVER_ID or not is_allowed(uid)):
        await state.set_state(Register.waiting_name)
        await message.answer(
            "Привіт! 👋 Напишіть, будь ласка, ваше ім'я — "
            "так вас буде підписано в таблиці (наприклад: Solomiya Batkiv)."
        )
        return

    if not is_allowed(uid):
        await message.answer("У вас немає доступу до цього бота.")
        return

    await message.answer(
        f"Привіт, {html.escape(manager_name(uid, message.from_user.first_name))}! "
        + start_text(uid),
        reply_markup=start_kb(),
    )


# ---------- Реєстрація ----------

def reg_kb(user_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Дати доступ", callback_data=f"reg_ok:{user_id}")
    kb.button(text="❌ Відхилити", callback_data=f"reg_no:{user_id}")
    kb.adjust(2)
    return kb.as_markup()


@router.message(StateFilter(Register.waiting_name), F.text)
async def reg_name(message: Message, state: FSMContext, bot: Bot):
    name = message.text.strip()[:60]
    if not name or name.startswith("/"):
        await message.answer("Напишіть, будь ласка, ім'я звичайним текстом.")
        return
    await state.clear()

    u = message.from_user
    status = "active" if u.id == APPROVER_ID else "pending"
    with db() as conn:
        conn.execute(
            """INSERT INTO managers (user_id, name, username, status, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,
                 username=excluded.username, status=excluded.status""",
            (u.id, name, u.username, status, now()),
        )

    if status == "active":
        await message.answer(
            f"Готово, {html.escape(name)}! " + start_text(u.id), reply_markup=start_kb()
        )
        return

    await message.answer("Дякую! Заявку на доступ надіслано адміністратору. Повідомлю, щойно її підтвердять.")
    uname = f" (@{html.escape(u.username)})" if u.username else ""
    await bot.send_message(
        APPROVER_ID,
        f"👤 Нова заявка на доступ\n\n<b>{html.escape(name)}</b>{uname}\nID: <code>{u.id}</code>",
        reply_markup=reg_kb(u.id),
    )


@router.callback_query(F.data.startswith("reg_ok:") | F.data.startswith("reg_no:"))
async def on_registration(call: CallbackQuery, bot: Bot):
    if call.from_user.id != APPROVER_ID:
        await call.answer("Тільки апрувер може це робити.", show_alert=True)
        return
    action, uid = call.data.split(":")
    uid = int(uid)
    m = get_manager(uid)
    if not m or m["status"] != "pending":
        await call.answer("Заявка вже оброблена.", show_alert=True)
        return

    if action == "reg_ok":
        with db() as conn:
            conn.execute("UPDATE managers SET status='active' WHERE user_id=?", (uid,))
        verdict = "✅ Доступ надано"
        await bot.send_message(
            uid,
            f"✅ Доступ відкрито, {html.escape(m['name'])}!\n\n" + start_text(uid),
            reply_markup=start_kb(),
        )
    else:
        with db() as conn:
            conn.execute("DELETE FROM managers WHERE user_id=?", (uid,))
        verdict = "❌ Відхилено"
        await bot.send_message(uid, "❌ На жаль, доступ до бота не надано.")

    await call.message.edit_text(
        f"{call.message.html_text}\n\n<b>{verdict}</b>", reply_markup=None
    )
    await call.answer(verdict)


@router.message(Command("managers"), from_approver)
async def cmd_managers(message: Message):
    lines = [f"• {html.escape(n)} — <code>{i}</code> (.env)" for i, n in MANAGERS.items()]
    with db() as conn:
        for r in conn.execute("SELECT * FROM managers ORDER BY created_at"):
            if r["user_id"] in MANAGERS:
                continue
            mark = "" if r["status"] == "active" else " ⏳ очікує"
            lines.append(f"• {html.escape(r['name'])} — <code>{r['user_id']}</code>{mark}")
    await message.answer("👥 Менеджери:\n\n" + ("\n".join(lines) if lines else "поки нікого"))


# ---------- Передача ролі апрувера ----------

def active_managers() -> list[tuple[int, str]]:
    people = dict(MANAGERS)
    with db() as conn:
        for r in conn.execute("SELECT user_id, name FROM managers WHERE status='active'"):
            people.setdefault(r["user_id"], r["name"])
    return sorted(people.items(), key=lambda p: p[1].lower())


@router.message(Command("approver"), from_approver)
async def cmd_approver(message: Message):
    candidates = [(i, n) for i, n in active_managers() if i != APPROVER_ID]
    if not candidates:
        await message.answer(
            "Немає кому передати роль: спочатку людина має зареєструватися в боті (/start) "
            "і отримати доступ."
        )
        return
    kb = InlineKeyboardBuilder()
    for uid, name in candidates:
        kb.button(text=name, callback_data=f"appr:{uid}")
    kb.button(text="Скасувати", callback_data="appr:cancel")
    kb.adjust(1)
    await message.answer(
        "Кому передати роль апрувера? Нові заявки й ті, що зараз очікують, підуть цій людині.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("appr:"))
async def on_transfer_approver(call: CallbackQuery, bot: Bot):
    if not from_approver(call):
        await call.answer("Тільки поточний апрувер може це робити.", show_alert=True)
        return
    target = call.data.split(":")[1]
    if target == "cancel":
        await call.message.edit_text("Скасовано.", reply_markup=None)
        await call.answer()
        return

    new_id = int(target)
    names = dict(active_managers())
    if new_id not in names or new_id == APPROVER_ID:
        await call.answer("Цю людину не можна призначити.", show_alert=True)
        return

    old_id = APPROVER_ID
    with db() as conn:
        pending = [r["id"] for r in conn.execute(
            "SELECT id FROM submissions WHERE status='pending' ORDER BY id"
        )]

    set_approver(new_id)
    try:
        await bot.send_message(
            new_id,
            f"👑 Вас призначено апрувером.\n"
            f"Заявок в очікуванні: {len(pending)}.\n\n" + start_text(new_id),
            reply_markup=start_kb(),
        )
    except Exception as e:
        # Людина могла заблокувати бота — роль лишається за старим апрувером
        logging.warning("Новий апрувер недоступний: %s", e)
        set_approver(old_id)
        await call.message.edit_text(
            "⚠️ Не вдалося написати цій людині (можливо, вона заблокувала бота). "
            "Роль залишилась за вами.",
            reply_markup=None,
        )
        await call.answer()
        return

    await call.message.edit_text(
        f"✅ Роль апрувера передано: <b>{html.escape(names[new_id])}</b>.", reply_markup=None
    )
    await call.answer()

    # Заявки, що очікують, переносимо новому апруверу
    for sub_id in pending:
        sub = get_submission(sub_id)
        await edit_approver_msg(bot, sub, f"↪️ Передано апруверу {html.escape(names[new_id])}")
        try:
            await send_to_approver(bot, sub)
        except Exception as e:
            logging.warning("Не вдалося переслати заявку #%s: %s", sub_id, e)


@router.message(Command("id"))
async def cmd_id(message: Message):
    uid = message.from_user.id
    text = f"Ваш Telegram ID: <code>{uid}</code>"
    if uid in MANAGERS:
        text += f"\nВи в списку менеджерів як: <b>{html.escape(MANAGERS[uid])}</b>"
    await message.answer(text)


@router.message(Command("cancel"), StateFilter(Review.waiting_comment))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Скасовано. Заявка лишилась в очікуванні.")


@router.message(Command("resync"), from_approver)
async def cmd_resync(message: Message, bot: Bot):
    if not SHEETS_WEBHOOK_URL:
        await message.answer("Таблиця не налаштована (SHEETS_WEBHOOK_URL).")
        return
    with db() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM submissions WHERE status='approved' AND archived=0 ORDER BY id"
        )]
    if not ids:
        await message.answer("Усі апрувнуті фото вже в таблиці.")
        return
    await message.answer(f"Записую {len(ids)} фото…")
    ok = 0
    for sub_id in ids:
        ok += await archive(bot, sub_id)
    await message.answer(f"Готово: {ok} з {len(ids)}.")


# ---------- Коментар апрувера (має йти перед прийомом фото) ----------

@router.message(StateFilter(Review.waiting_comment), from_approver, F.text)
async def approver_comment(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    sub_id = data.get("sub_id")
    await state.clear()

    sub = get_submission(sub_id) if sub_id else None
    if not sub or sub["status"] != "pending":
        await message.answer("Ця заявка вже оброблена.")
        return

    comment = message.text.strip()
    with db() as conn:
        conn.execute(
            "UPDATE submissions SET status='rejected', comment=?, decided_at=? WHERE id=?",
            (comment, now(), sub_id),
        )

    await bot.send_message(
        sub["user_id"],
        f"❌ Фото #{sub_id} повернуто на доробку.\n\n"
        f"<b>Коментар:</b> {html.escape(comment)}\n\n"
        "Виправте й надішліть нове фото сюди.",
        reply_parameters=ReplyParameters(
            message_id=sub["user_msg_id"], allow_sending_without_reply=True
        ),
        reply_markup=start_kb(),
    )
    await mark_decided(bot, sub, f"❌ Rejected: {html.escape(comment)}")
    await message.answer(f"Коментар до #{sub_id} надіслано виконавцю.")


# ---------- Прийом фото ----------

@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def receive_photo(message: Message, bot: Bot):
    if not is_allowed(message.from_user.id):
        await message.answer("Спочатку зареєструйтесь: натисніть /start")
        return

    if message.photo:
        file_id, kind, mime, fname = message.photo[-1].file_id, "photo", "image/jpeg", None
        thumb = message.photo[len(message.photo) // 2].file_id  # середній розмір
    else:
        d = message.document
        file_id, kind, mime, fname = d.file_id, "document", d.mime_type, d.file_name
        thumb = d.thumbnail.file_id if d.thumbnail else None

    u = message.from_user
    name = f"@{u.username}" if u.username else u.full_name

    # Чернетка: на апрув піде лише після вибору статі
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO submissions
               (user_id, user_name, user_msg_id, file_id, file_kind, caption, created_at,
                mime_type, file_name, thumb_file_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')""",
            (u.id, name, message.message_id, file_id, kind, message.caption, now(),
             mime, fname, thumb),
        )
        sub_id = cur.lastrowid

    await message.reply(f"Фото #{sub_id}: оберіть стать 👇", reply_markup=gender_kb(sub_id))


@router.callback_query(F.data.startswith("g:"))
async def on_gender(call: CallbackQuery, bot: Bot):
    _, gender, sub_id = call.data.split(":")
    sub_id = int(sub_id)
    sub = get_submission(sub_id)
    if not sub or sub["user_id"] != call.from_user.id:
        await call.answer("Це не ваше фото.", show_alert=True)
        return
    if sub["status"] != "draft" or gender not in GENDERS:
        await call.answer("Стать уже обрано.", show_alert=True)
        return

    with db() as conn:
        conn.execute(
            "UPDATE submissions SET gender=?, status='pending' WHERE id=?", (gender, sub_id)
        )
    await send_to_approver(bot, get_submission(sub_id))

    await call.message.edit_text(
        f"📨 Фото #{sub_id} ({gender_label(gender)}) надіслано на апрув. Чекайте відповіді.",
        reply_markup=None,
    )
    await call.answer()


@router.message(F.photo | F.document)
async def wrong_file(message: Message):
    await message.answer("Надішліть, будь ласка, зображення (JPG/PNG/WEBP).")


# ---------- Кнопки апрувера ----------

@router.callback_query(F.data.startswith("ok:"))
async def on_approve(call: CallbackQuery, bot: Bot):
    if call.from_user.id != APPROVER_ID:
        await call.answer("Тільки апрувер може це робити.", show_alert=True)
        return
    sub_id = int(call.data.split(":")[1])
    sub = get_submission(sub_id)
    if not sub or sub["status"] != "pending":
        await call.answer("Заявка вже оброблена.", show_alert=True)
        return

    with db() as conn:
        conn.execute(
            "UPDATE submissions SET status='approved', decided_at=? WHERE id=?", (now(), sub_id)
        )
    await call.answer("Прийнято")

    await bot.send_message(
        sub["user_id"],
        f"✅ Фото #{sub_id} прийнято. Все добре!",
        reply_parameters=ReplyParameters(
            message_id=sub["user_msg_id"], allow_sending_without_reply=True
        ),
    )
    await mark_decided(bot, sub, "✅ Approved")
    await archive(bot, sub_id)


@router.callback_query(F.data.startswith("no:"))
async def on_reject(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != APPROVER_ID:
        await call.answer("Тільки апрувер може це робити.", show_alert=True)
        return
    sub_id = int(call.data.split(":")[1])
    sub = get_submission(sub_id)
    if not sub or sub["status"] != "pending":
        await call.answer("Заявка вже оброблена.", show_alert=True)
        return

    await state.set_state(Review.waiting_comment)
    await state.update_data(sub_id=sub_id)
    await call.message.answer(
        f"✍️ Напишіть, що не так з фото #{sub_id}. Коментар піде виконавцю.\n"
        "/cancel — скасувати."
    )
    await call.answer()


async def mark_decided(bot: Bot, sub: sqlite3.Row, verdict: str) -> None:
    """Прибирає кнопки з повідомлення апрувера й дописує рішення."""
    await edit_approver_msg(bot, sub, verdict)


async def send_to_approver(bot: Bot, sub: sqlite3.Row) -> None:
    """Надсилає заявку поточному апруверу з кнопками Approve / Reject."""
    caption = (
        f"🆕 Заявка #{sub['id']} від {user_label(sub)}\n"
        f"Стать: <b>{gender_label(sub['gender'])}</b>"
    )
    if sub["caption"]:
        caption += f"\n\n{html.escape(sub['caption'])}"
    send = bot.send_photo if sub["file_kind"] == "photo" else bot.send_document
    sent = await send(
        APPROVER_ID, sub["file_id"], caption=caption[:1024], reply_markup=review_kb(sub["id"])
    )
    with db() as conn:
        conn.execute(
            "UPDATE submissions SET approver_msg_id=?, approver_chat_id=? WHERE id=?",
            (sent.message_id, APPROVER_ID, sub["id"]),
        )


async def edit_approver_msg(bot: Bot, sub: sqlite3.Row, verdict: str) -> None:
    if not sub["approver_msg_id"]:
        return
    caption = (
        f"Заявка #{sub['id']} від {user_label(sub)}\n"
        f"Стать: <b>{gender_label(sub['gender'])}</b>"
    )
    if sub["caption"]:
        caption += f"\n\n{html.escape(sub['caption'])}"
    caption += f"\n\n<b>{verdict}</b>"
    try:
        await bot.edit_message_caption(
            chat_id=sub["approver_chat_id"] or APPROVER_ID,
            message_id=sub["approver_msg_id"],
            caption=caption[:1024],
            reply_markup=None,
        )
    except Exception as e:  # повідомлення могли видалити
        logging.warning("Не вдалося оновити повідомлення апрувера: %s", e)


# ---------- Решта тексту ----------

@router.message(F.text)
async def fallback(message: Message):
    await message.answer(
        "Надішліть згенероване фото сюди (краще файлом), і воно піде на апрув.",
        reply_markup=start_kb(),
    )


async def main():
    logging.basicConfig(level=logging.INFO)
    if not APPROVER_ID:
        logging.warning("APPROVER_ID не задано: напишіть боту /start, щоб дізнатися свій ID")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())