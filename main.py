import asyncio
import logging
import re
import difflib
import hashlib
import hmac
import time
import json
import os
import sqlite3
import base64
import html as html_module
from datetime import datetime, date, timedelta

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.error import RetryAfter
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from aiohttp import web

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]  # токен берём тільки з env
FM_SPREADSHEET_ID = "1x-vsC2M1cLtitP2DF04EqkSB4emVwvyh4N3jaauLqZ4"
EKOL_SPREADSHEET_ID = os.environ.get("EKOL_SPREADSHEET_ID", "")  # поки не налаштовано

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
WEB_PORT = int(os.environ.get("PORT", 8080))
CREDENTIALS_FILE = "credentials.json"

# RAILWAY_VOLUME_MOUNT_PATH встановлюється Railway автоматично, якщо до сервісу
# прикріплено Volume. Локально (в PyCharm) цієї змінної немає — тоді файли
# зберігаються поруч зі скриптом, як і раніше.
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
CITIES_FILE = os.path.join(DATA_DIR, "cities.json")       # тільки для одноразової міграції
REPORTS_FILE = os.path.join(DATA_DIR, "reports.json")     # тільки для одноразової міграції
DB_FILE = os.path.join(DATA_DIR, "postavo4ki.db")
ALLOWED_USERS = [7305470549, 506094120]
REPORT_GROUP_ID = -5344273524
MY_CARD_NUMBER = "4441111134286644"

# Міста, які треба виключити саме з парсингу FM (наприклад через однойменне
# але зовсім інше місто в тій же колонці "Місто" — реальний Миколаїв ведеться
# тільки через Ekol, а в FM під цією ж назвою трапляється Миколаїв Львівської області)
FM_EXCLUDED_CITIES = {"Миколаїв"}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

current_report_date = {}
edit_state = {}  # user_id -> {"date": ..., "location": ..., "field": "hours"|"workers"|"paid"}
worker_flow_state = {}  # user_id -> {"mode": "add_name"|"add_phone"|"add_username"|"add_card"|"edit_field", ...}
anketa_state = {}  # user_id -> {"step": "name"|"age"|"phone", "data": {...}} — для незнайомих людей (не ALLOWED_USERS)

WORKER_FIELD_LABELS = {
    "name": "ім'я",
    "phone": "телефон",
    "username": "username в Telegram (без @)",
    "card": "номер картки",
    "city": "місто",
}

FIELD_LABELS = {
    "hours": "години",
    "workers": "кількість людей",
    "paid": "сума виплат (те, що я реально заплатив)",
}
FIELD_TO_OVERRIDE_KEY = {
    "hours": "hours",
    "workers": "total_workers",
    "paid": "paid_to_workers",
}

# Тексты кнопок Reply Keyboard — их нельзя перехватывать как оплату/карту в группе
BUTTON_TEXTS = {
    "📦 Поставки", "🏙 Мої міста", "📊 Звіт", "🗂 Мої звіти", "👷 Робітники", "📇 Кандидати",
}


# ==================== SQLITE ====================
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cities (
            name TEXT PRIMARY KEY,
            rate INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS city_aliases (
            alias TEXT PRIMARY KEY,
            city_name TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payment_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            text TEXT NOT NULL,
            timestamp TEXT,
            message_id INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_messages_date ON payment_messages(report_date)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            username TEXT,
            card_number TEXT,
            created_at TEXT
        )
    """)
    worker_cols = [r["name"] for r in conn.execute("PRAGMA table_info(workers)")]
    if "telegram_id" not in worker_cols:
        conn.execute("ALTER TABLE workers ADD COLUMN telegram_id INTEGER")
    if "city" not in worker_cols:
        conn.execute("ALTER TABLE workers ADD COLUMN city TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS worker_phones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_key TEXT NOT NULL,
            worker_id INTEGER NOT NULL,
            created_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_assignments_key ON delivery_assignments(delivery_key)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_contacts (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen TEXT,
            last_seen TEXT,
            full_name_ua TEXT,
            age TEXT,
            phone TEXT,
            city_raw TEXT,
            anketa_completed_at TEXT,
            converted_to_worker_id INTEGER
        )
    """)
    contact_cols = [r["name"] for r in conn.execute("PRAGMA table_info(bot_contacts)")]
    if "city_raw" not in contact_cols:
        conn.execute("ALTER TABLE bot_contacts ADD COLUMN city_raw TEXT")
    if "dismissed" not in contact_cols:
        conn.execute("ALTER TABLE bot_contacts ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_overrides (
            report_date TEXT NOT NULL,
            location TEXT NOT NULL,
            hours REAL,
            total_workers INTEGER,
            paid_to_workers REAL,
            PRIMARY KEY (report_date, location)
        )
    """)
    conn.commit()
    conn.close()


def migrate_json_to_db():
    """Одноразово переносить дані зі старих cities.json / reports.json у SQLite,
    якщо відповідні таблиці ще порожні. Самі JSON-файли не видаляються — лежать як бекап."""
    conn = get_conn()

    cities_count = conn.execute("SELECT COUNT(*) AS c FROM cities").fetchone()["c"]
    if cities_count == 0 and os.path.exists(CITIES_FILE):
        try:
            with open(CITIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, info in data.items():
                if isinstance(info, (int, float)):
                    rate, aliases = info, []
                else:
                    rate, aliases = info.get("rate", 0), info.get("aliases", [])
                conn.execute("INSERT OR IGNORE INTO cities (name, rate) VALUES (?, ?)", (name, rate))
                for alias in aliases:
                    conn.execute(
                        "INSERT OR IGNORE INTO city_aliases (alias, city_name) VALUES (?, ?)",
                        (alias, name)
                    )
            conn.commit()
            logger.info(f"Мігровано {len(data)} міст з cities.json у SQLite")
        except Exception as e:
            logger.error(f"Помилка міграції cities.json: {e}", exc_info=True)

    messages_count = conn.execute("SELECT COUNT(*) AS c FROM payment_messages").fetchone()["c"]
    if messages_count == 0 and os.path.exists(REPORTS_FILE):
        try:
            with open(REPORTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            total = 0
            for report_date, session in data.items():
                for msg in session.get("messages", []):
                    conn.execute(
                        "INSERT INTO payment_messages (report_date, text, timestamp, message_id) VALUES (?, ?, ?, ?)",
                        (report_date, msg.get("text", ""), msg.get("timestamp"), msg.get("message_id"))
                    )
                    total += 1
                for location, ov in session.get("overrides", {}).items():
                    conn.execute(
                        """INSERT INTO report_overrides (report_date, location, hours, total_workers, paid_to_workers)
                           VALUES (?, ?, ?, ?, ?)""",
                        (report_date, location, ov.get("hours"), ov.get("total_workers"), ov.get("paid_to_workers"))
                    )
            conn.commit()
            logger.info(f"Мігровано {total} повідомлень з reports.json у SQLite")
        except Exception as e:
            logger.error(f"Помилка міграції reports.json: {e}", exc_info=True)

    conn.close()


# ==================== МІСТА ====================
def load_cities() -> dict:
    """Формат: {"Назва": {"rate": 200, "aliases": ["Скорочення", ...]}}"""
    conn = get_conn()
    cities = {}
    for row in conn.execute("SELECT name, rate FROM cities"):
        cities[row["name"]] = {"rate": row["rate"], "aliases": []}
    for row in conn.execute("SELECT alias, city_name FROM city_aliases"):
        if row["city_name"] in cities:
            cities[row["city_name"]]["aliases"].append(row["alias"])
    conn.close()
    return cities


def save_cities(cities: dict):
    """Повністю синхронізує таблиці cities/city_aliases зі станом переданого словника."""
    conn = get_conn()
    existing = {row["name"] for row in conn.execute("SELECT name FROM cities")}
    incoming = set(cities.keys())

    for name in existing - incoming:
        conn.execute("DELETE FROM city_aliases WHERE city_name = ?", (name,))
        conn.execute("DELETE FROM cities WHERE name = ?", (name,))

    for name, info in cities.items():
        rate = info.get("rate", 0) if isinstance(info, dict) else info
        aliases = info.get("aliases", []) if isinstance(info, dict) else []
        conn.execute(
            "INSERT INTO cities (name, rate) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET rate=excluded.rate",
            (name, rate)
        )
        conn.execute("DELETE FROM city_aliases WHERE city_name = ?", (name,))
        for alias in aliases:
            conn.execute(
                "INSERT OR REPLACE INTO city_aliases (alias, city_name) VALUES (?, ?)",
                (alias, name)
            )

    conn.commit()
    conn.close()


def build_city_index(cities: dict) -> dict:
    """Ключ у нижньому регістрі (назва або синонім) -> канонічна назва міста."""
    index = {}
    for city_name, info in cities.items():
        aliases = info.get("aliases", []) if isinstance(info, dict) else []
        index[city_name.lower()] = city_name
        for alias in aliases:
            index[alias.lower()] = city_name
    return index


# ==================== ЗВІТИ (сирі повідомлення про оплату + ручні правки) ====================
def db_add_message(report_date: str, text: str, timestamp: str, message_id: int):
    conn = get_conn()
    conn.execute(
        "INSERT INTO payment_messages (report_date, text, timestamp, message_id) VALUES (?, ?, ?, ?)",
        (report_date, text, timestamp, message_id)
    )
    conn.commit()
    conn.close()


def db_get_messages(report_date: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT text, timestamp, message_id FROM payment_messages WHERE report_date = ? ORDER BY id",
        (report_date,)
    ).fetchall()
    conn.close()
    return [{"text": r["text"], "timestamp": r["timestamp"], "message_id": r["message_id"]} for r in rows]


def db_update_message_by_id(message_id: int, new_text: str) -> bool:
    """Оновлює текст вже збереженого повідомлення за його Telegram message_id
    (для випадку, коли користувач відредагував повідомлення замість того, щоб надіслати нове).
    Повертає True, якщо запис знайдено й оновлено."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE payment_messages SET text = ? WHERE message_id = ?",
        (new_text, message_id)
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def db_list_report_dates() -> list:
    conn = get_conn()
    rows = conn.execute("""
        SELECT report_date FROM payment_messages
        UNION
        SELECT report_date FROM report_overrides
    """).fetchall()
    conn.close()
    dates = {r["report_date"] for r in rows}
    return sorted(dates, key=lambda d: datetime.strptime(d, "%d.%m.%Y"), reverse=True)


def db_delete_report(report_date: str):
    conn = get_conn()
    conn.execute("DELETE FROM payment_messages WHERE report_date = ?", (report_date,))
    conn.execute("DELETE FROM report_overrides WHERE report_date = ?", (report_date,))
    conn.commit()
    conn.close()


def db_get_overrides(report_date: str) -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT location, hours, total_workers, paid_to_workers FROM report_overrides WHERE report_date = ?",
        (report_date,)
    ).fetchall()
    conn.close()
    overrides = {}
    for r in rows:
        ov = {}
        if r["hours"] is not None:
            ov["hours"] = r["hours"]
        if r["total_workers"] is not None:
            ov["total_workers"] = r["total_workers"]
        if r["paid_to_workers"] is not None:
            ov["paid_to_workers"] = r["paid_to_workers"]
        overrides[r["location"]] = ov
    return overrides


def db_set_override(report_date: str, location: str, field: str, value):
    if field not in ("hours", "total_workers", "paid_to_workers"):
        raise ValueError(f"Невідоме поле override: {field}")
    conn = get_conn()
    conn.execute(
        "INSERT INTO report_overrides (report_date, location) VALUES (?, ?) "
        "ON CONFLICT(report_date, location) DO NOTHING",
        (report_date, location)
    )
    conn.execute(
        f"UPDATE report_overrides SET {field} = ? WHERE report_date = ? AND location = ?",
        (value, report_date, location)
    )
    conn.commit()
    conn.close()


def db_delete_override(report_date: str, location: str):
    conn = get_conn()
    conn.execute(
        "DELETE FROM report_overrides WHERE report_date = ? AND location = ?",
        (report_date, location)
    )
    conn.commit()
    conn.close()


# ==================== РОБІТНИКИ ====================
def db_add_worker(name: str, phone: str = "", username: str = "", card: str = "", telegram_id: int = None, city: str = "") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO workers (name, phone, username, card_number, telegram_id, city, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, phone, username, card, telegram_id, city, datetime.now().isoformat())
    )
    conn.commit()
    worker_id = cur.lastrowid
    conn.close()
    return worker_id


UA_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"


def ua_sort_key(name: str):
    name = name.lower()
    return [UA_ALPHABET.index(c) if c in UA_ALPHABET else 1000 + ord(c) for c in name]


def db_get_workers() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT id, name, phone, username, card_number, telegram_id, city FROM workers").fetchall()
    conn.close()
    workers = [
        {
            "id": r["id"], "name": r["name"], "phone": r["phone"] or "",
            "username": r["username"] or "", "card": r["card_number"] or "",
            "telegram_id": r["telegram_id"], "city": r["city"] or "",
        }
        for r in rows
    ]
    workers.sort(key=lambda w: ua_sort_key(w["name"]))
    return workers


def db_get_worker(worker_id: int):
    conn = get_conn()
    r = conn.execute(
        "SELECT id, name, phone, username, card_number, telegram_id, city FROM workers WHERE id = ?",
        (worker_id,)
    ).fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r["id"], "name": r["name"], "phone": r["phone"] or "",
        "username": r["username"] or "", "card": r["card_number"] or "",
        "telegram_id": r["telegram_id"], "city": r["city"] or "",
    }


def db_update_worker(worker_id: int, field: str, value: str):
    column = {"name": "name", "phone": "phone", "username": "username", "card": "card_number", "city": "city"}.get(field)
    if not column:
        raise ValueError(f"Невідоме поле працівника: {field}")
    conn = get_conn()
    conn.execute(f"UPDATE workers SET {column} = ? WHERE id = ?", (value, worker_id))
    conn.commit()
    conn.close()


def db_delete_worker(worker_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM worker_phones WHERE worker_id = ?", (worker_id,))
    conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
    conn.commit()
    conn.close()


def db_add_worker_phone(worker_id: int, phone: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO worker_phones (worker_id, phone, created_at) VALUES (?, ?, ?)",
        (worker_id, phone, datetime.now().isoformat())
    )
    conn.commit()
    phone_id = cur.lastrowid
    conn.close()
    return phone_id


def db_get_worker_phones(worker_id: int) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, phone FROM worker_phones WHERE worker_id = ? ORDER BY id", (worker_id,)
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "phone": r["phone"]} for r in rows]


def db_delete_worker_phone(phone_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM worker_phones WHERE id = ?", (phone_id,))
    conn.commit()
    conn.close()


def db_update_worker_phone(phone_id: int, value: str):
    conn = get_conn()
    conn.execute("UPDATE worker_phones SET phone = ? WHERE id = ?", (value, phone_id))
    conn.commit()
    conn.close()


def db_promote_worker_phone(worker_id: int, phone_id: int):
    """Робить додатковий номер основним, а старий основний переносить у додаткові."""
    phones = db_get_worker_phones(worker_id)
    target = next((p for p in phones if p["id"] == phone_id), None)
    if not target:
        return
    old_primary = db_get_worker(worker_id).get("phone", "")
    db_update_worker(worker_id, "phone", target["phone"])
    db_delete_worker_phone(phone_id)
    if old_primary:
        db_add_worker_phone(worker_id, old_primary)


# ==================== ПРИЗНАЧЕННЯ РОБІТНИКІВ НА ПОСТАВКИ ====================
def make_delivery_key(source: str, date_str: str, text: str, needed: int = None) -> str:
    """Стабільний ключ поставки: не залежить від сесії/id повідомлення,
    тільки від змісту картки — щоб призначення переживали рестарт бота.
    Кількість потрібних людей закодована прямо в ключі (щоб клавіатуру
    можна було перебудувати з самого ключа, без додаткового стану)."""
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    needed_part = str(needed) if needed else "x"
    return f"{source}:{date_str}:{h}:{needed_part}"


def get_needed_from_key(delivery_key: str):
    parts = delivery_key.split(":")
    if len(parts) >= 4 and parts[3].isdigit():
        return int(parts[3])
    return None


def db_assign_worker(delivery_key: str, worker_id: int):
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM delivery_assignments WHERE delivery_key = ? AND worker_id = ?",
        (delivery_key, worker_id)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO delivery_assignments (delivery_key, worker_id, created_at) VALUES (?, ?, ?)",
            (delivery_key, worker_id, datetime.now().isoformat())
        )
        conn.commit()
    conn.close()


def db_unassign_worker(delivery_key: str, worker_id: int):
    conn = get_conn()
    conn.execute(
        "DELETE FROM delivery_assignments WHERE delivery_key = ? AND worker_id = ?",
        (delivery_key, worker_id)
    )
    conn.commit()
    conn.close()


def db_get_assigned_workers(delivery_key: str) -> list:
    conn = get_conn()

    # міграція зі старого формату ключа (без закодованої кількості потрібних людей):
    # якщо є записи під "коротким" ключем цієї ж поставки — переносимо їх на новий ключ
    parts = delivery_key.split(":")
    if len(parts) >= 4:
        legacy_key = ":".join(parts[:3])
        if legacy_key != delivery_key:
            conn.execute(
                "UPDATE delivery_assignments SET delivery_key = ? WHERE delivery_key = ?",
                (delivery_key, legacy_key)
            )
            conn.commit()

    rows = conn.execute("""
        SELECT w.id, w.name, w.username, w.telegram_id FROM delivery_assignments da
        JOIN workers w ON w.id = da.worker_id
        WHERE da.delivery_key = ?
        ORDER BY da.id
    """, (delivery_key,)).fetchall()
    conn.close()
    return [
        {"id": r["id"], "name": r["name"], "username": r["username"] or "", "telegram_id": r["telegram_id"]}
        for r in rows
    ]


def get_delivery_assign_keyboard(delivery_key: str) -> InlineKeyboardMarkup:
    assigned = db_get_assigned_workers(delivery_key)
    needed = get_needed_from_key(delivery_key)
    keyboard = []

    for w in assigned:
        row = []
        if w.get("telegram_id") or w.get("username"):
            chat_url = f"tg://user?id={w['telegram_id']}" if w.get("telegram_id") else f"https://t.me/{w['username']}"
            row.append(InlineKeyboardButton(w["name"], url=chat_url))
        else:
            row.append(InlineKeyboardButton(w["name"], callback_data="noop"))
        row.append(InlineKeyboardButton("❌", callback_data=f"unassign_{delivery_key}_{w['id']}"))
        keyboard.append(row)

    count = len(assigned)
    if needed and count >= needed:
        keyboard.append([
            InlineKeyboardButton(f"✅ Набрано ({count}/{needed})", callback_data="noop"),
            InlineKeyboardButton("➕ Призначити", callback_data=f"assign_{delivery_key}"),
        ])
    else:
        label = f"➕ Призначити ({count}/{needed})" if needed else "➕ Призначити"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"assign_{delivery_key}")])

    return InlineKeyboardMarkup(keyboard)


def normalize_phone(phone: str) -> str:
    """Прибирає все, крім цифр, і код країни/ведучий нуль — щоб порівнювати
    +380664492617 / 0664492617 / 380664492617 як один і той самий номер."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("380") and len(digits) > 9:
        digits = digits[3:]
    elif digits.startswith("0") and len(digits) == 10:
        digits = digits[1:]
    return digits


def db_find_matching_worker(username: str = "", phone: str = "", telegram_id: int = None, exclude_id: int = None):
    """Шукає серед вже існуючих робітників того, хто, ймовірно, — та сама людина
    (за telegram_id, username або нормалізованим номером телефону — основним чи додатковим)."""
    username_norm = username.strip().lstrip("@").lower() if username else ""
    phone_norm = normalize_phone(phone) if phone else ""

    for w in db_get_workers():
        if exclude_id and w["id"] == exclude_id:
            continue
        if telegram_id and w.get("telegram_id") == telegram_id:
            return w
        if username_norm and w.get("username", "").lower() == username_norm:
            return w
        if phone_norm:
            if normalize_phone(w.get("phone", "")) == phone_norm:
                return w
            if any(normalize_phone(p["phone"]) == phone_norm for p in db_get_worker_phones(w["id"])):
                return w
    return None


def db_merge_workers(keep_id: int, remove_id: int):
    """Об'єднує remove_id в keep_id: переносить порожні поля, зберігає інший номер
    телефону як додатковий (не втрачає його), видаляє дубль."""
    keep = db_get_worker(keep_id)
    remove = db_get_worker(remove_id)
    if not keep or not remove:
        return keep

    for field in ("username", "card", "city"):
        if not keep.get(field) and remove.get(field):
            db_update_worker(keep_id, field, remove[field])

    if not keep.get("phone") and remove.get("phone"):
        db_update_worker(keep_id, "phone", remove["phone"])
    elif keep.get("phone") and remove.get("phone"):
        if normalize_phone(keep["phone"]) != normalize_phone(remove["phone"]):
            already = [normalize_phone(p["phone"]) for p in db_get_worker_phones(keep_id)]
            if normalize_phone(remove["phone"]) not in already:
                db_add_worker_phone(keep_id, remove["phone"])

    if not keep.get("telegram_id") and remove.get("telegram_id"):
        conn = get_conn()
        conn.execute("UPDATE workers SET telegram_id = ? WHERE id = ?", (remove["telegram_id"], keep_id))
        conn.commit()
        conn.close()

    conn = get_conn()
    conn.execute("UPDATE worker_phones SET worker_id = ? WHERE worker_id = ?", (keep_id, remove_id))
    conn.execute(
        "UPDATE bot_contacts SET converted_to_worker_id = ? WHERE converted_to_worker_id = ?",
        (keep_id, remove_id)
    )
    conn.commit()
    conn.close()

    db_delete_worker(remove_id)
    return db_get_worker(keep_id)


# ==================== КОНТАКТИ БОТА (для "знайомства" з незнайомими людьми) ====================
def db_touch_contact(user):
    """Логуємо/оновлюємо будь-якого, хто написав боту — незалежно від того, чи є в ALLOWED_USERS."""
    conn = get_conn()
    now = datetime.now().isoformat()
    existing = conn.execute("SELECT telegram_id FROM bot_contacts WHERE telegram_id = ?", (user.id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE bot_contacts SET username=?, first_name=?, last_name=?, last_seen=? WHERE telegram_id=?",
            (user.username or "", user.first_name or "", user.last_name or "", now, user.id)
        )
    else:
        conn.execute(
            "INSERT INTO bot_contacts (telegram_id, username, first_name, last_name, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, user.username or "", user.first_name or "", user.last_name or "", now, now)
        )
    conn.commit()
    conn.close()


def db_save_anketa(telegram_id: int, data: dict):
    conn = get_conn()
    conn.execute(
        "UPDATE bot_contacts SET full_name_ua=?, age=?, phone=?, city_raw=?, anketa_completed_at=? WHERE telegram_id=?",
        (
            data.get("full_name", ""), data.get("age", ""), data.get("phone", ""),
            data.get("city", ""), datetime.now().isoformat(), telegram_id
        )
    )
    conn.commit()
    conn.close()


def db_get_contact(telegram_id: int):
    conn = get_conn()
    r = conn.execute("SELECT * FROM bot_contacts WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return dict(r) if r else None


def db_list_pending_contacts() -> list:
    """Ті, хто заповнив анкету, але ще не доданий у реєстр робітників і не відхилений."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bot_contacts WHERE anketa_completed_at IS NOT NULL "
        "AND converted_to_worker_id IS NULL AND dismissed = 0 "
        "ORDER BY anketa_completed_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_mark_converted(telegram_id: int, worker_id: int):
    conn = get_conn()
    conn.execute("UPDATE bot_contacts SET converted_to_worker_id = ? WHERE telegram_id = ?", (worker_id, telegram_id))
    conn.commit()
    conn.close()


def db_dismiss_contact(telegram_id: int):
    conn = get_conn()
    conn.execute("UPDATE bot_contacts SET dismissed = 1 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()


init_db()
migrate_json_to_db()


# ==================== GOOGLE SHEETS ====================
def get_sheet_data(source: str = "fm"):
    import base64
    import json as json_module

    if source == "fm":
        spreadsheet_id = FM_SPREADSHEET_ID
    elif source == "ekol":
        spreadsheet_id = EKOL_SPREADSHEET_ID
    else:
        raise ValueError(f"Невідоме джерело: {source}")

    if not spreadsheet_id:
        raise ValueError(f"Таблиця для '{source}' ще не налаштована (немає SPREADSHEET_ID)")

    google_creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_B64")
    google_creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")

    if google_creds_b64:
        creds_json_str = base64.b64decode(google_creds_b64).decode("utf-8")
        creds_json_str = creds_json_str.replace('\\n', '\n')
        creds_dict = json_module.loads(creds_json_str)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    elif google_creds_json:
        creds_dict = json_module.loads(google_creds_json, strict=False)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)
    last_sheet = spreadsheet.worksheets()[-1]
    logger.info(f"Читаємо лист ({source}): {last_sheet.title}")

    all_values = last_sheet.get_all_values()
    sheet_id = last_sheet.id
    spreadsheet_meta = spreadsheet.fetch_sheet_metadata()

    merged_cells = []
    for sheet_meta in spreadsheet_meta.get('sheets', []):
        if sheet_meta['properties']['sheetId'] == sheet_id:
            for merge in sheet_meta.get('merges', []):
                merged_cells.append({
                    'start_row': merge['startRowIndex'],
                    'end_row': merge['endRowIndex'],
                    'start_col': merge['startColumnIndex'],
                    'end_col': merge['endColumnIndex'],
                })
            break

    return all_values, merged_cells


def is_merged_with_above(row_idx: int, col_idx: int, merged_cells: list) -> bool:
    for merge in merged_cells:
        if (merge['start_col'] <= col_idx < merge['end_col'] and
                merge['start_row'] < row_idx < merge['end_row']):
            return True
    return False


def extract_phone(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r'\+38\s*\(?\d{3}\)?\s*\d{3}[\s-]?\d{2}[\s-]?\d{2}',
        r'38\s*\(?\d{3}\)?\s*\d{3}[\s-]?\d{2}[\s-]?\d{2}',
        r'\+?\(0\d{2}\)\s*\d{3}[\s-]?\d{2}[\s-]?\d{2}',
        r'\b0\d{9}\b',
        r'\b\d{9}\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            phone = match.group().strip()
            digits = re.sub(r'\D', '', phone)
            if len(digits) == 9:
                digits = '380' + digits
            elif len(digits) == 10 and digits.startswith('0'):
                digits = '38' + digits
            return '+' + digits if not digits.startswith('+') else digits
    return ""


def parse_routes(all_values: list, merged_cells: list) -> list:
    routes = []
    current_route = []

    for row_idx, row in enumerate(all_values):
        if row[0] in ("Місто", "Город", "місто", "город"):
            continue
        if all(cell.strip() == "" for cell in row):
            if current_route:
                routes.append(current_route)
                current_route = []
            continue
        if not row[0].strip():
            continue
        current_route.append((row_idx, row))

    if current_route:
        routes.append(current_route)

    return routes


def build_delivery_messages(routes: list, merged_cells: list, filter_date: date = None) -> list:
    cities = load_cities()
    excluded = {c.lower() for c in FM_EXCLUDED_CITIES}
    my_cities = {c.lower() for c in cities.keys()} - excluded
    messages = []

    for route in routes:
        driver_phone = ""
        for row_idx, row in route:
            if len(row) > 9 and row[9].strip():
                phone = extract_phone(row[9])
                if phone and not driver_phone:
                    driver_phone = phone

        groups = []
        current_group = []

        for row_idx, row in route:
            city = row[0].strip() if len(row) > 0 else ""
            tc = row[1].strip() if len(row) > 1 else ""
            brand = row[2].strip() if len(row) > 2 else ""
            boxes = row[4].strip() if len(row) > 4 else ""
            workers_cell = row[5].strip() if len(row) > 5 else ""
            delivery_date = row[7].strip() if len(row) > 7 else ""
            delivery_time = row[8].strip() if len(row) > 8 else ""

            if not city or not delivery_date:
                continue

            parsed_date = None
            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                try:
                    parsed_date = datetime.strptime(delivery_date, fmt).date()
                    break
                except ValueError:
                    continue

            is_continuation = is_merged_with_above(row_idx, 5, merged_cells)

            point = {
                "city": city, "tc": tc, "brand": brand, "boxes": boxes,
                "workers": workers_cell, "date": parsed_date,
                "date_str": delivery_date, "time": delivery_time,
                "phone": driver_phone, "is_continuation": is_continuation,
            }

            if is_continuation:
                current_group.append(point)
            else:
                if current_group:
                    groups.append(current_group)
                current_group = [point]

        if current_group:
            groups.append(current_group)

        for group in groups:
            first = group[0]

            if filter_date:
                dates_in_group = [p["date"] for p in group if p["date"]]
                if not any(d == filter_date for d in dates_in_group):
                    continue

            if len(group) == 1:
                if first['city'].lower() not in my_cities:
                    continue
                msg = f"📦 *{first['brand']}*\n"
                msg += f"📍 {first['city']}, {first['tc']}\n"
                msg += f"📅 {first['date_str']}"
                if first['time']:
                    msg += f"  🕐 {first['time']}"
                msg += "\n"
                msg += f"📦 Коробок: {first['boxes']}\n"
                msg += f"👷 Вантажників: {first['workers']}\n"
                if first['phone']:
                    msg += f"📞 {first['phone']}"
            else:
                group = [p for p in group if p['city'].lower() in my_cities]
                if not group:
                    continue
                first = group[0]
                unique_tc = {p['tc'] for p in group}

                if len(unique_tc) == 1:
                    brands = ", ".join(p['brand'] for p in group)
                    total_boxes = sum(int(p['boxes']) for p in group if p['boxes'].isdigit())
                    msg = f"📦 *{brands}*\n"
                    msg += f"📍 {first['city']}, {first['tc']}\n"
                    msg += f"📅 {first['date_str']}"
                    if first['time']:
                        msg += f"  🕐 {first['time']}"
                    msg += "\n"
                    msg += f"📦 Коробок: {total_boxes}\n"
                    msg += f"👷 Вантажників: {first['workers']}\n"
                    if first['phone']:
                        msg += f"📞 {first['phone']}"
                elif len(group) == 1:
                    msg = f"📦 *{first['brand']}*\n"
                    msg += f"📍 {first['city']}, {first['tc']}\n"
                    msg += f"📅 {first['date_str']}"
                    if first['time']:
                        msg += f"  🕐 {first['time']}"
                    msg += "\n"
                    msg += f"📦 Коробок: {first['boxes']}\n"
                    msg += f"👷 Вантажників: {first['workers']}\n"
                    if first['phone']:
                        msg += f"📞 {first['phone']}"
                else:
                    msg = f"🗺 Маршрут\n"
                    msg += f"👷 Вантажників: {first['workers']}\n"
                    if first['phone']:
                        msg += f"📞 {first['phone']}\n"
                    msg += "─────────────────\n"
                    for p in group:
                        msg += f"📦 *{p['brand']}*\n"
                        msg += f"📍 {p['city']}, {p['tc']}\n"
                        msg += f"📅 {p['date_str']}"
                        if p['time']:
                            msg += f"  🕐 {p['time']}"
                        msg += f"  📦 {p['boxes']} кор.\n\n"

            messages.append({
                "text": msg,
                "date": first["date"],
                "date_str": first["date_str"],
                "workers_needed": parse_int_safe(first.get("workers")),
            })

    return messages


# ==================== EKOL ====================
def parse_int_safe(value) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def normalize_apostrophes(text: str) -> str:
    return text.replace('’', "'").replace('`', "'").replace('ʼ', "'")


def address_matches_my_cities(address: str, cities: dict) -> bool:
    """Перевіряє, чи згадується в адресі хоча б одне з моїх міст (або синонім) —
    точним співпадінням цілого слова, без урахування регістру."""
    addr_lower = normalize_apostrophes(address.lower())
    for city_name, info in cities.items():
        aliases = info.get('aliases', []) if isinstance(info, dict) else []
        for candidate in [city_name] + aliases:
            candidate_lower = normalize_apostrophes(candidate.lower())
            pattern = r'\b' + re.escape(candidate_lower) + r'\b'
            if re.search(pattern, addr_lower):
                return True
    return False


def parse_ekol_deliveries(all_values: list, filter_date: date = None) -> list:
    """Колонки: A компанія, B адреса, C дата, D час, E кількість, F вантажники, G телефон.
    Кожен рядок — окрема, незалежна поставка (без merged cells)."""
    cities = load_cities()
    messages = []

    for row in all_values:
        company = row[0].strip() if len(row) > 0 else ""
        address = row[1].strip() if len(row) > 1 else ""
        date_str = row[2].strip() if len(row) > 2 else ""
        time_str = row[3].strip() if len(row) > 3 else ""
        qty = row[4].strip() if len(row) > 4 else ""
        workers = row[5].strip() if len(row) > 5 else ""
        phone = row[6].strip() if len(row) > 6 else ""

        if not address or not date_str:
            continue

        parsed_date = None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                parsed_date = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue
        if not parsed_date:
            continue  # не рядок з даними (шапка таблиці, порожній рядок тощо)

        if filter_date and parsed_date != filter_date:
            continue

        if not address_matches_my_cities(address, cities):
            continue

        msg = f"📦 *{company or '—'}*\n"
        msg += f"📍 {address}\n"
        msg += f"📅 {date_str}"
        if time_str:
            msg += f"  🕐 {time_str}"
        msg += "\n"
        if qty:
            msg += f"📦 Кількість: {qty}\n"
        if workers:
            msg += f"👷 Вантажників: {workers}\n"
        if phone:
            msg += f"📞 {phone}"

        messages.append({
            "text": msg.strip(),
            "date": parsed_date,
            "date_str": date_str,
            "workers_needed": parse_int_safe(workers),
        })

    return messages


# ==================== ЗВІТ ТА СТАТИСТИКА ====================
def parse_payment_message(text: str):
    """Парсим первую строку сообщения об оплате"""
    first_line = text.strip().split('\n')[0].strip()
    pattern = r'^(.+?)\s+по\s+(\d+(?:[.,]\d+)?)\s+\((\d+(?:[.,]\d+)?)\)'
    match = re.match(pattern, first_line)
    if not match:
        return None
    location = match.group(1).strip()
    amount = float(match.group(2).replace(',', '.'))
    hours = float(match.group(3).replace(',', '.'))

    # Ищем "за двох/трьох..." в остальных строках того же сообщения,
    # а всё остальное (заметки для логістів) зберігаємо як є
    za_kilkokh = 0
    note_lines = []
    lines = text.strip().split('\n')
    for line in lines[1:]:
        stripped = line.strip()
        wc = parse_workers_count(stripped)
        if wc:
            za_kilkokh = wc
        elif stripped:
            note_lines.append(stripped)

    return {
        'location': location,
        'amount': amount,  # сколько реально заплачено вантажникам за цей блок
        'hours': hours,
        'za_kilkokh': za_kilkokh,
        'note': '\n'.join(note_lines),
    }


def parse_workers_count(text: str):
    mapping = {
        'за двох': 2, 'за трьох': 3, 'за чотирьох': 4,
        "за п'ятьох": 5, 'за шістьох': 6,
    }
    return mapping.get(text.strip().lower())


def match_city_detailed(location: str, cities: dict):
    """Повертає (місто, суфікс) — суфікс це текст після назви міста/синоніма
    (наприклад 'Велес' для 'Івано-Франківськ Велес')."""
    loc_lower = location.lower()
    index = build_city_index(cities)
    best_key = None
    for key in index.keys():
        if loc_lower.startswith(key):
            if best_key is None or len(key) > len(best_key):
                best_key = key
    if best_key is None:
        return None, location
    city = index[best_key]
    suffix = location[len(best_key):].strip()
    return city, (suffix if suffix else location)


def match_city(location: str, cities: dict):
    """Шукає місто за точним співпадінням назви або явного синоніма
    (тільки якщо location починається саме з цього слова)."""
    city, _ = match_city_detailed(location, cities)
    return city


def guess_city(raw: str, cities: dict) -> str:
    """Найкраще припущення, яке місто мав на увазі користувач у вільному тексті
    (наприклад "Ивано-Франковск", "ІФ", "ИФ", "і-ф"). Це лише підказка —
    не використовується ніде в фінансових розрахунках, тільки для довідки."""
    if not raw or not raw.strip():
        return ""

    text = normalize_apostrophes(raw.strip().lower())
    index = build_city_index(cities)

    # 1) точне співпадіння з назвою чи синонімом
    if text in index:
        return index[text]

    # 2) абревіатура: "ІФ" / "ИФ" / "і-ф" -> перші літери слів "Івано-Франківськ"
    translit = str.maketrans({"и": "і", "ы": "і"})
    letters_only = re.sub(r"[^а-яіїєґ]", "", text.translate(translit))
    if letters_only:
        for city_name in cities.keys():
            parts = re.split(r"[ \-]", city_name.lower().translate(translit))
            initials = "".join(p[0] for p in parts if p)
            if letters_only == initials:
                return city_name

    # 3) нечітке співпадіння (одруківки, транслітерація)
    close = difflib.get_close_matches(text, list(index.keys()), n=1, cutoff=0.6)
    if close:
        return index[close[0]]

    return ""


def format_hours(hours: float) -> str:
    if hours == int(hours):
        return str(int(hours))
    return str(hours).replace('.', ',')


def format_money(amount: float) -> str:
    """Точна сума грн: ціле число без дробової частини, або з копійками через кому
    (797,5), без жодного округлення чи втрати копійок."""
    if amount == int(amount):
        return str(int(amount))
    formatted = f"{amount:.2f}".rstrip('0').rstrip('.')
    return formatted.replace('.', ',')


def compute_location_data(report_date: str) -> list:
    """Повертає впорядкований список локацій з годинами/кількістю людей/сумою виплат/тарифом/доходом.
    Враховує ручні правки (overrides), якщо вони є."""
    messages = db_get_messages(report_date)
    if not messages:
        return []
    overrides = db_get_overrides(report_date)

    cities = load_cities()

    # Группируем сообщения по блокам
    blocks = []
    current_block = None

    for msg in messages:
        text = msg['text'].strip()
        payment = parse_payment_message(text)

        if payment:
            if current_block:
                blocks.append(current_block)
            current_block = {
                'location': payment['location'],
                'hours': payment['hours'],
                'za_kilkokh': payment['za_kilkokh'],
                'confirm_count': 0,
                'raw_amount': payment['amount'],
                'note': payment.get('note', ''),
            }
        elif current_block:
            wc = parse_workers_count(text)
            if wc:
                current_block['za_kilkokh'] += wc
            elif text:
                # будь-яке непорожнє повідомлення після суми — підтвердження оплати
                # одній людині (карта, ім'я, банк — не важливо, за оплату відповідає сам користувач)
                current_block['confirm_count'] += 1

    if current_block:
        blocks.append(current_block)

    # Группируем блоки по локации
    grouped = {}
    grouped_order = []
    for block in blocks:
        loc = block['location']
        if loc not in grouped:
            grouped[loc] = {
                'location': loc,
                'hours': block['hours'] if block['hours'] > 0 else 0,
                'total_workers': 0,
                'paid_to_workers': 0.0,
                'notes': [],
            }
            grouped_order.append(loc)
        elif grouped[loc]['hours'] == 0 and block['hours'] > 0:
            # перше повідомлення для цієї локації мало (0) годин — беремо години з наступного блоку
            grouped[loc]['hours'] = block['hours']

        # Скільки людей отримали гроші за цей блок:
        # якщо є мітка "За N" — сума вже загальна на всіх N.
        # якщо мітки немає — сума вказана ЗА ОДНУ людину, і підтверджень (будь-яких повідомлень) може бути декілька.
        if block['za_kilkokh'] > 0:
            block_workers = block['za_kilkokh']
            block_paid = block['raw_amount']
        else:
            block_workers = block['confirm_count'] if block['confirm_count'] > 0 else 1
            block_paid = block['raw_amount'] * block_workers

        # (0) годин — це особиста доплата (наприклад водію), яка йде тільки в мінус,
        # але не рахується як офіційний вантажник для звіту логістам
        if block['hours'] > 0:
            grouped[loc]['total_workers'] += block_workers

        grouped[loc]['paid_to_workers'] += block_paid

        if block.get('note'):
            grouped[loc]['notes'].append(block['note'])

    # Застосовуємо ручні правки (якщо є)
    for loc in grouped_order:
        if loc in overrides:
            ov = overrides[loc]
            if 'hours' in ov:
                grouped[loc]['hours'] = ov['hours']
            if 'total_workers' in ov:
                grouped[loc]['total_workers'] = ov['total_workers']
            if 'paid_to_workers' in ov:
                grouped[loc]['paid_to_workers'] = ov['paid_to_workers']

    result = []
    for loc in grouped_order:
        data = grouped[loc]
        city, sub_label = match_city_detailed(data['location'], cities)
        my_rate = cities.get(city, {}).get('rate', 0) if city else 0
        hours = data['hours']
        total_workers = data['total_workers']
        my_total = my_rate * hours * total_workers

        result.append({
            'location': loc,
            'city': city,
            'sub_label': sub_label,
            'hours': hours,
            'total_workers': total_workers,
            'paid_to_workers': data['paid_to_workers'],
            'rate': my_rate,
            'income': my_total,
            'note': '\n'.join(data['notes']),
            'edited': loc in overrides,
        })

    return result


def build_report_and_stats(report_date: str):
    """Возвращает (список строк отчёта, словарь статистики), с учётом ручных правок."""
    locations = compute_location_data(report_date)
    if not locations:
        return [], {}

    workers_ua = {
        1: '', 2: 'За двох', 3: 'За трьох',
        4: 'За чотирьох', 5: "За п'ятьох", 6: 'За шістьох'
    }

    reports = []
    city_stats = {}
    total_paid = 0.0
    total_income = 0.0
    total_manhours = 0.0

    for item in locations:
        loc = item['location']
        hours = item['hours']
        total_workers = item['total_workers']
        my_total = item['income']
        paid = item['paid_to_workers']
        city = item['city']

        hours_str = format_hours(hours)
        if total_workers > 1:
            label = workers_ua.get(total_workers, f'За {total_workers}')
            report_text = f"{loc} по {format_money(my_total)} ({hours_str})\n{label}"
        else:
            report_text = f"{loc} по {format_money(my_total)} ({hours_str})"
        if item.get('note'):
            report_text += f"\n{item['note']}"
        reports.append(report_text)

        total_paid += paid
        total_income += my_total
        total_manhours += hours * total_workers

        city_key = city if city else loc
        if city_key not in city_stats:
            city_stats[city_key] = {'paid': 0.0, 'income': 0.0, 'entries': []}
        city_stats[city_key]['paid'] += paid
        city_stats[city_key]['income'] += my_total
        city_stats[city_key]['entries'].append({
            'label': item['sub_label'],
            'profit': my_total - paid,
        })

    city_list = []
    for city_name, vals in city_stats.items():
        profit = vals['income'] - vals['paid']
        city_list.append({
            'city': city_name,
            'paid': vals['paid'],
            'income': vals['income'],
            'profit': profit,
            'entries': vals['entries'],
        })
    city_list.sort(key=lambda x: x['profit'], reverse=True)

    stats = {
        'total_deliveries': len(locations),
        'total_paid': total_paid,
        'total_income': total_income,
        'total_profit': total_income - total_paid,
        'total_manhours': total_manhours,
        'cities': city_list,
    }

    return reports, stats


def format_stats_message(report_date: str, stats: dict) -> str:
    if not stats:
        return "❌ Немає даних для статистики."

    lines = [f"📊 *Статистика за {report_date}*", ""]
    lines.append(f"*{stats['total_deliveries']} поставок*")
    lines.append("")
    lines.append(f"💸 Заплачено вантажникам: {format_money(stats['total_paid'])} грн")
    lines.append(f"💰 Отримаю за роботу: {format_money(stats['total_income'])} грн")

    profit = stats['total_profit']
    emoji = "📈" if profit >= 0 else "📉"
    lines.append(f"{emoji} Чистий прибуток: {format_money(profit)} грн")

    if stats['total_manhours'] > 0:
        lines.append(f"⏱ Людино-годин відпрацьовано: {stats['total_manhours']:.1f}")
        lines.append(f"📐 Маржа на людино-годину: {profit / stats['total_manhours']:.1f} грн")

    if stats['cities']:
        lines.append("")
        lines.append("🏙 *Міста за прибутковістю:*")
        for i, c in enumerate(stats['cities'], start=1):
            lines.append(f"{i}. {c['city']} — {format_money(c['profit'])} грн")
            if len(c['entries']) > 1:
                for entry in c['entries']:
                    sign = "+" if entry['profit'] >= 0 else ""
                    lines.append(f"    • {entry['label']} — {sign}{format_money(entry['profit'])} грн")

    return "\n".join(lines)


async def send_with_retry(bot, chat_id: int, text: str, parse_mode: str = None, reply_markup=None, max_retries: int = 5):
    """Отправка с обробкою Telegram flood control (RetryAfter) та інших тимчасових помилок."""
    for attempt in range(max_retries):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
            return
        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning(f"Flood control, чекаємо {wait}с (спроба {attempt + 1})")
            await asyncio.sleep(wait)
        except Exception as e:
            logger.warning(f"Помилка відправки, чекаємо 2с: {e}")
            await asyncio.sleep(2)
    # остання спроба без придушення помилки — щоб вона стала видною в логах
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)


async def send_report_and_stats(bot, chat_id: int, report_date: str, reports: list, stats: dict, mode: str = "both"):
    if mode in ("report", "both"):
        for r in reports:
            await send_with_retry(bot, chat_id, r)
            await asyncio.sleep(0.3)

        # службові повідомлення для логістів: за яку дату звіт і куди переказати оплату
        await send_with_retry(bot, chat_id, f"ЗА {report_date}")
        await asyncio.sleep(0.3)
        await send_with_retry(bot, chat_id, MY_CARD_NUMBER)
        await asyncio.sleep(0.3)

    if mode in ("stats", "both"):
        # особистий підсумок — тільки для мене
        await send_with_retry(bot, chat_id, format_stats_message(report_date, stats), parse_mode="Markdown")


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.id != REPORT_GROUP_ID:
        return
    if msg.from_user.id not in ALLOWED_USERS:
        return

    text = msg.text or ""
    if not text:
        return
    if text in BUTTON_TEXTS:
        return  # это нажатие кнопки меню, а не оплата — не перехватываем

    user_id = msg.from_user.id
    if user_id in edit_state:
        return  # це введення нового значення при редагуванні позиції, а не оплата
    if user_id in worker_flow_state:
        return  # це введення даних працівника, а не оплата
    if user_id not in current_report_date:
        return

    report_date = current_report_date[user_id]
    if report_date == "waiting_date":
        return

    for attempt in range(3):
        try:
            db_add_message(report_date, text, msg.date.isoformat(), msg.message_id)
            return
        except Exception as e:
            logger.warning(f"Спроба {attempt + 1}: не вдалося зберегти повідомлення оплати: {e}")
            await asyncio.sleep(0.5)

    logger.error(f"НЕ ЗБЕРЕЖЕНО повідомлення оплати за {report_date}: {text!r}")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"⚠️ Не вдалося зберегти в звіт за {report_date} це повідомлення:\n\n{text}\n\n"
                f"Перешли його ще раз у групу."
            )
        )
    except Exception:
        pass


async def group_edited_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram надсилає редагування повідомлення окремим типом update (edited_message),
    не таким самим, як нове повідомлення — тому це окремий обробник."""
    msg = update.edited_message
    if not msg or msg.chat.id != REPORT_GROUP_ID:
        return
    if msg.from_user.id not in ALLOWED_USERS:
        return

    text = msg.text or ""
    if not text or text in BUTTON_TEXTS:
        return

    updated = db_update_message_by_id(msg.message_id, text)
    if updated:
        logger.info(f"Оновлено відредаговане повідомлення {msg.message_id}: {text!r}")
        return

    # Повідомлення раніше не було збережено (наприклад, редагування прийшло
    # для чогось, що бот пропустив) — спробуємо додати його як нове,
    # якщо зараз активно триває збір звіту.
    user_id = msg.from_user.id
    if user_id in current_report_date and current_report_date[user_id] != "waiting_date":
        report_date = current_report_date[user_id]
        try:
            timestamp = msg.edit_date.isoformat() if msg.edit_date else datetime.now().isoformat()
            db_add_message(report_date, text, timestamp, msg.message_id)
            logger.info(f"Відредаговане повідомлення додано як нове: {msg.message_id}")
        except Exception as e:
            logger.error(f"Не вдалося зберегти відредаговане повідомлення: {e}")


# ==================== KEYBOARDS ====================
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📦 Поставки"],
            ["🏙 Мої міста", "📊 Звіт"],
            ["🗂 Мої звіти", "👷 Робітники"],
            ["📇 Кандидати"],
        ],
        resize_keyboard=True
    )


def get_reports_list_keyboard():
    dates = db_list_report_dates()
    keyboard = [[InlineKeyboardButton(d, callback_data=f"rdate_{d}")] for d in dates]
    return keyboard, dates


def get_workers_list_keyboard():
    workers = db_get_workers()
    keyboard = [[InlineKeyboardButton(w["name"] or f"#{w['id']}", callback_data=f"wview_{w['id']}")] for w in workers]
    keyboard.append([InlineKeyboardButton("➕ Додати працівника", callback_data="waddnew")])
    return keyboard, workers


def get_candidates_list_keyboard():
    candidates = db_list_pending_contacts()
    keyboard = [
        [InlineKeyboardButton(c["full_name_ua"] or f"ID {c['telegram_id']}", callback_data=f"cand_{c['telegram_id']}")]
        for c in candidates
    ]
    return keyboard, candidates


def format_candidate_card(c: dict) -> str:
    lines = [f"📇 *{c['full_name_ua'] or '(без імені)'}*"]
    if c.get("age"):
        lines.append(f"🎂 Вік: {c['age']}")
    if c.get("phone"):
        lines.append(f"📞 {c['phone']}")
    if c.get("username"):
        lines.append(f"💬 @{c['username']}")
    if c.get("city_raw"):
        lines.append(f"📍 Місто (як написав): {c['city_raw']}")
        guess = guess_city(c["city_raw"], load_cities())
        if guess and guess.lower() != c["city_raw"].strip().lower():
            lines.append(f"   ймовірно: {guess}")
    lines.append(f"🆔 {c['telegram_id']}")
    return "\n".join(lines)


def format_worker_card(w: dict) -> str:
    lines = [f"👷 *{w['name']}*"]
    if w["phone"]:
        lines.append(f"📞 {w['phone']}")
    for p in db_get_worker_phones(w["id"]):
        lines.append(f"📞 {p['phone']} _(додатковий)_")
    if w.get("city"):
        lines.append(f"📍 {w['city']}")
    if w["username"]:
        lines.append(f"💬 @{w['username']}")
    if w["card"]:
        lines.append(f"💳 {w['card']}")
    if w.get("telegram_id"):
        lines.append(f"🆔 {w['telegram_id']} (справжній Telegram ID, підтверджений)")
    return "\n".join(lines)


def get_worker_card_keyboard(worker_id: int) -> InlineKeyboardMarkup:
    w = db_get_worker(worker_id)
    keyboard = []

    if w and (w.get("telegram_id") or w.get("username")):
        chat_url = f"tg://user?id={w['telegram_id']}" if w.get("telegram_id") else f"https://t.me/{w['username']}"
        keyboard.append([InlineKeyboardButton("💬 Написати", url=chat_url)])

    keyboard += [
        [
            InlineKeyboardButton("✏️ Ім'я", callback_data=f"wf_{worker_id}_name"),
            InlineKeyboardButton("📞 Телефони", callback_data=f"wphones_{worker_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Місто", callback_data=f"wf_{worker_id}_city"),
            InlineKeyboardButton("✏️ Username", callback_data=f"wf_{worker_id}_username"),
        ],
        [InlineKeyboardButton("✏️ Картка", callback_data=f"wf_{worker_id}_card")],
        [InlineKeyboardButton("🔗 Об'єднати з іншим", callback_data=f"wmergestart_{worker_id}")],
        [InlineKeyboardButton("🗑 Видалити", callback_data=f"wdel_{worker_id}")],
        [InlineKeyboardButton("◀️ До списку", callback_data="wback")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_phones_management_keyboard(worker_id: int) -> InlineKeyboardMarkup:
    w = db_get_worker(worker_id)
    keyboard = []
    if w and w.get("phone"):
        keyboard.append([InlineKeyboardButton(f"📞 {w['phone']} (основний)", callback_data=f"wphoneview_{worker_id}_main")])
    else:
        keyboard.append([InlineKeyboardButton("➕ Додати основний номер", callback_data=f"wf_{worker_id}_phone")])
    for p in db_get_worker_phones(worker_id):
        keyboard.append([InlineKeyboardButton(f"📞 {p['phone']}", callback_data=f"wphoneview_{worker_id}_{p['id']}")])
    keyboard.append([InlineKeyboardButton("➕ Додати ще номер", callback_data=f"wphoneadd_{worker_id}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"wview_{worker_id}")])
    return InlineKeyboardMarkup(keyboard)


def get_phone_action_keyboard(worker_id: int, token: str) -> InlineKeyboardMarkup:
    if token == "main":
        keyboard = [
            [InlineKeyboardButton("✏️ Змінити", callback_data=f"wf_{worker_id}_phone")],
            [InlineKeyboardButton("🗑 Видалити", callback_data=f"wphoneclearmain_{worker_id}")],
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("✏️ Змінити", callback_data=f"wphoneeditextra_{token}_{worker_id}")],
            [InlineKeyboardButton("⭐ Зробити основним", callback_data=f"wphonepromote_{token}_{worker_id}")],
            [InlineKeyboardButton("🗑 Видалити", callback_data=f"wphonedel_{token}_{worker_id}")],
        ]
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"wphones_{worker_id}")])
    return InlineKeyboardMarkup(keyboard)


def get_merge_target_keyboard(worker_id: int) -> InlineKeyboardMarkup:
    others = [w for w in db_get_workers() if w["id"] != worker_id]
    keyboard = [
        [InlineKeyboardButton(w["name"] or f"#{w['id']}", callback_data=f"wmergepick_{worker_id}_{w['id']}")]
        for w in others
    ]
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data=f"wview_{worker_id}")])
    return InlineKeyboardMarkup(keyboard)


# ==================== ВІДПРАВКА ПОСТАВОК ====================
SOURCE_EMOJI = {"fm": "🔴", "ekol": "🔵"}
SOURCE_LABEL = {"fm": "FM", "ekol": "Ekol"}


async def send_deliveries_query(query, context: ContextTypes.DEFAULT_TYPE, source: str = "fm", filter_date: date = None):
    chat_id = query.message.chat_id
    bot = context.bot
    emoji = SOURCE_EMOJI.get(source, "")
    label = SOURCE_LABEL.get(source, source)
    await query.edit_message_text(f"⏳ {emoji} Завантажую дані з таблиці {label}...")
    try:
        all_values, merged_cells = get_sheet_data(source=source)

        if source == "ekol":
            messages = parse_ekol_deliveries(all_values, filter_date=filter_date)
        else:
            routes = parse_routes(all_values, merged_cells)
            messages = build_delivery_messages(routes, merged_cells, filter_date=filter_date)

        if not messages:
            date_info = filter_date.strftime("%d.%m.%Y") if filter_date else ""
            await send_with_retry(
                bot, chat_id,
                f"❌ {emoji} Поставок {label} {'на ' + date_info if date_info else ''} не знайдено."
            )
            return

        date_info = filter_date.strftime("%d.%m.%Y") if filter_date else "всі"
        await send_with_retry(
            bot, chat_id,
            f"✅ {emoji} {label}: знайдено поставок *{len(messages)}* (дата: {date_info})",
            parse_mode="Markdown"
        )

        for msg_data in messages:
            delivery_key = make_delivery_key(
                source, msg_data["date_str"], msg_data["text"], needed=msg_data.get("workers_needed")
            )
            await send_with_retry(
                bot, chat_id, msg_data["text"], parse_mode="Markdown",
                reply_markup=get_delivery_assign_keyboard(delivery_key)
            )
            await asyncio.sleep(0.35)

    except Exception as e:
        logger.error(f"Помилка: {e}", exc_info=True)
        await send_with_retry(bot, chat_id, f"❌ Помилка: {str(e)}")


# ==================== КОМАНДИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_touch_contact(user)

    if user.id not in ALLOWED_USERS:
        anketa_state[user.id] = {"step": "name", "data": {}}
        await update.message.reply_text(
            "👋 Вітаю! Щоб зв'язатися з вами щодо роботи, надішліть, будь ласка, "
            "невелику інформацію про себе.\n\nПрізвище та ім'я:"
        )
        return

    worker_flow_state.pop(user.id, None)
    edit_state.pop(user.id, None)
    await update.message.reply_text(
        "👋 Привіт! Я бот для поставок FM Logistics.\n\nОбери розділ:",
        reply_markup=get_main_keyboard()
    )


async def notify_admins_new_contact(bot, user, data: dict):
    text = (
        f"📇 *Нова заявка*\n\n"
        f"Прізвище та ім'я: {data.get('full_name', '—')}\n"
        f"Вік: {data.get('age', '—')}\n"
        f"Телефон: {data.get('phone', '—')}\n"
        f"Місто (як написав): {data.get('city', '—')}\n"
        f"Telegram: {'@' + user.username if user.username else '(немає username)'}\n"
        f"ID: {user.id}"
    )
    for admin_id in ALLOWED_USERS:
        try:
            await send_with_retry(bot, admin_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Не вдалося сповістити {admin_id} про нову заявку: {e}")


async def handle_anketa_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state = anketa_state[user.id]
    raw = update.message.text.strip()
    step = state["step"]

    if step == "name":
        if not raw:
            await update.message.reply_text("Введіть, будь ласка, прізвище та ім'я:")
            return
        state["data"]["full_name"] = raw
        state["step"] = "age"
        await update.message.reply_text("Вік:")
        return

    if step == "age":
        state["data"]["age"] = raw
        state["step"] = "phone"
        await update.message.reply_text("Номер телефону:")
        return

    if step == "phone":
        state["data"]["phone"] = raw
        state["step"] = "city"
        await update.message.reply_text("З якого ви міста?")
        return

    if step == "city":
        state["data"]["city"] = raw
        db_save_anketa(user.id, state["data"])
        del anketa_state[user.id]
        await update.message.reply_text("✅ Дякую! Ми зв'яжемося з вами за потреби.")
        await notify_admins_new_contact(context.bot, user, state["data"])
        return


async def mycities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    cities = load_cities()
    if not cities:
        await update.message.reply_text("❌ Список міст порожній.")
        return
    text = "🏙 *Мої міста:*\n\n"
    for city, info in sorted(cities.items()):
        rate = info.get('rate', 0)
        aliases = info.get('aliases', [])
        text += f"📍 {city} — {rate} грн/год"
        if aliases:
            text += f" (синоніми: {', '.join(aliases)})"
        text += "\n"
    text += (
        "\nЩоб додати місто: /addcity Назва Тариф"
        "\nЩоб видалити місто: /removecity Назва"
        "\nЩоб додати синонім: /addalias Назва Синонім"
        "\nЩоб видалити синонім: /removealias Синонім"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def addcity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /addcity Назва 135\nПриклад: /addcity Вінниця 135")
        return
    rate_str = context.args[-1]
    city_name = " ".join(context.args[:-1])
    try:
        rate = int(rate_str)
    except ValueError:
        await update.message.reply_text("❌ Тариф має бути числом.")
        return
    cities = load_cities()
    existing_aliases = cities.get(city_name, {}).get('aliases', [])
    cities[city_name] = {"rate": rate, "aliases": existing_aliases}
    save_cities(cities)
    await update.message.reply_text(f"✅ Додано: {city_name} — {rate} грн/год")


async def removecity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    if not context.args:
        await update.message.reply_text("Використання: /removecity Назва\nПриклад: /removecity Вінниця")
        return
    city_name = " ".join(context.args)
    cities = load_cities()
    if city_name in cities:
        del cities[city_name]
        save_cities(cities)
        await update.message.reply_text(f"✅ Видалено: {city_name}")
    else:
        await update.message.reply_text(f"❌ Місто '{city_name}' не знайдено.")


async def addalias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Використання: /addalias Назва Синонім\n"
            "Приклад: /addalias Могилів-Подільський Могилів"
        )
        return
    alias = context.args[-1]
    city_name = " ".join(context.args[:-1])
    cities = load_cities()
    if city_name not in cities:
        await update.message.reply_text(
            f"❌ Місто '{city_name}' не знайдено. Спочатку додайте його: /addcity {city_name} <тариф>"
        )
        return
    aliases = cities[city_name].get('aliases', [])
    if alias.lower() in [a.lower() for a in aliases]:
        await update.message.reply_text(f"⚠️ Синонім '{alias}' вже є у '{city_name}'.")
        return
    aliases.append(alias)
    cities[city_name]['aliases'] = aliases
    save_cities(cities)
    await update.message.reply_text(f"✅ Додано синонім: '{alias}' → {city_name}")


async def removealias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    if not context.args:
        await update.message.reply_text("Використання: /removealias Синонім\nПриклад: /removealias Могилів")
        return
    alias = " ".join(context.args)
    cities = load_cities()
    for city_name, info in cities.items():
        aliases = info.get('aliases', [])
        new_aliases = [a for a in aliases if a.lower() != alias.lower()]
        if len(new_aliases) != len(aliases):
            info['aliases'] = new_aliases
            save_cities(cities)
            await update.message.reply_text(f"✅ Синонім '{alias}' видалено з '{city_name}'.")
            return
    await update.message.reply_text(f"❌ Синонім '{alias}' не знайдено.")


# ==================== TEXT HANDLER ====================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_touch_contact(user)

    if user.id not in ALLOWED_USERS:
        if user.id in anketa_state:
            await handle_anketa_step(update, context)
        return

    user_id = user.id
    incoming_text = update.message.text.strip()

    # Натискання кнопки меню скасовує будь-який незавершений флоу редагування —
    # інакше текст кнопки "проковтується" як відповідь на попереднє питання бота
    if incoming_text in BUTTON_TEXTS:
        worker_flow_state.pop(user_id, None)
        edit_state.pop(user_id, None)

    if user_id in worker_flow_state:
        state = worker_flow_state[user_id]
        raw = update.message.text.strip()
        mode = state["mode"]

        if mode == "add_name":
            if not raw:
                await update.message.reply_text("❌ Ім'я не може бути порожнім. Введіть ім'я:")
                return
            state["data"]["name"] = raw
            state["mode"] = "add_phone"
            await update.message.reply_text("Телефон (або «-», щоб пропустити):")
            return

        if mode == "add_phone":
            state["data"]["phone"] = "" if raw == "-" else raw
            state["mode"] = "add_username"
            await update.message.reply_text("Username в Telegram без @ (або «-», щоб пропустити):")
            return

        if mode == "add_username":
            state["data"]["username"] = "" if raw == "-" else raw.lstrip("@")
            state["mode"] = "add_card"
            await update.message.reply_text("Номер картки (або «-», щоб пропустити):")
            return

        if mode == "add_card":
            state["data"]["card"] = "" if raw == "-" else raw
            worker_id = db_add_worker(**state["data"])
            del worker_flow_state[user_id]
            w = db_get_worker(worker_id)
            await update.message.reply_text(
                f"✅ Додано працівника:\n\n{format_worker_card(w)}",
                parse_mode="Markdown",
                reply_markup=get_worker_card_keyboard(worker_id)
            )
            return

        if mode == "edit_field":
            worker_id = state["worker_id"]
            field = state["field"]
            value = "" if raw == "-" else (raw.lstrip("@") if field == "username" else raw)
            db_update_worker(worker_id, field, value)
            del worker_flow_state[user_id]
            w = db_get_worker(worker_id)
            if w:
                await update.message.reply_text(
                    f"✅ Оновлено.\n\n{format_worker_card(w)}",
                    parse_mode="Markdown",
                    reply_markup=get_worker_card_keyboard(worker_id)
                )
            return

        if mode == "add_phone2":
            worker_id = state["worker_id"]
            if raw:
                db_add_worker_phone(worker_id, raw)
            del worker_flow_state[user_id]
            w = db_get_worker(worker_id)
            if w:
                await update.message.reply_text(
                    f"✅ Додано номер.\n\n{format_worker_card(w)}",
                    parse_mode="Markdown",
                    reply_markup=get_worker_card_keyboard(worker_id)
                )
            return

        if mode == "edit_phone_extra":
            phone_id = state["phone_id"]
            worker_id = state["worker_id"]
            if raw == "-":
                db_delete_worker_phone(phone_id)
            else:
                db_update_worker_phone(phone_id, raw)
            del worker_flow_state[user_id]
            w = db_get_worker(worker_id)
            if w:
                await update.message.reply_text(
                    f"✅ Оновлено.\n\n{format_worker_card(w)}",
                    parse_mode="Markdown",
                    reply_markup=get_phones_management_keyboard(worker_id)
                )
            return

    if user_id in edit_state:
        state = edit_state[user_id]
        text_input = update.message.text.strip().replace(',', '.')
        field = state['field']
        try:
            value = float(text_input)
            if field == 'workers':
                value = int(round(value))
            if value < 0:
                raise ValueError("negative")
        except ValueError:
            await update.message.reply_text("❌ Введіть коректне число (наприклад 2 або 2.5)")
            return

        report_date = state['date']
        location = state['location']
        override_key = FIELD_TO_OVERRIDE_KEY[field]

        db_set_override(report_date, location, override_key, value)

        del edit_state[user_id]

        keyboard = [
            [InlineKeyboardButton("✏️ Редагувати ще", callback_data=f"rloc_{report_date}_{state['idx']}")],
            [InlineKeyboardButton("◀️ До списку позицій", callback_data=f"redit_{report_date}")],
        ]
        await update.message.reply_text(
            f"✅ Оновлено: {location} → {FIELD_LABELS[field]} = {value}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if current_report_date.get(user_id) == "waiting_date":
        text_input = update.message.text.strip()
        try:
            datetime.strptime(text_input, "%d.%m.%Y")
            current_report_date[user_id] = text_input
            keyboard = [[InlineKeyboardButton("📋 Сформувати звіт", callback_data="build_report")]]
            await update.message.reply_text(
                f"✅ Дата звіту: {text_input}\n\nТепер скидайте оплати в групу.\nКоли закінчите — натисніть кнопку нижче.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except ValueError:
            await update.message.reply_text("❌ Невірний формат. Введіть дату як ДД.ММ.РРРР")
        return

    text = update.message.text

    if text == "📦 Поставки":
        keyboard = [
            [InlineKeyboardButton("🔴 FM", callback_data="dlvsrc_fm")],
            [InlineKeyboardButton("🔵 Ekol", callback_data="dlvsrc_ekol")],
        ]
        await update.message.reply_text(
            "Оберіть таблицю:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif text == "🏙 Мої міста":
        await mycities(update, context)
    elif text == "📊 Звіт":
        keyboard = [
            [InlineKeyboardButton("📅 Вчора", callback_data="report_yesterday")],
            [InlineKeyboardButton("📅 Сьогодні", callback_data="report_today")],
            [InlineKeyboardButton("✏️ Ввести дату", callback_data="report_custom")],
        ]
        await update.message.reply_text(
            "Оберіть дату звіту:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif text == "🗂 Мої звіти":
        keyboard, dates = get_reports_list_keyboard()
        if not dates:
            await update.message.reply_text("❌ Поки немає жодного звіту.")
        else:
            await update.message.reply_text(
                "Оберіть дату звіту:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    elif text == "👷 Робітники":
        keyboard, workers = get_workers_list_keyboard()
        msg = "Робітники:" if workers else "❌ Поки немає жодного працівника."
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif text == "📇 Кандидати":
        keyboard, candidates = get_candidates_list_keyboard()
        msg = "Нові заявки (ще не в реєстрі):" if candidates else "❌ Поки немає нових заявок."
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))


# ==================== CALLBACK HANDLER ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USERS:
        await query.answer("⛔ Доступ заборонено.")
        return
    await query.answer()
    data = query.data

    if data.startswith("dlvsrc_"):
        source = data.replace("dlvsrc_", "")
        label = SOURCE_LABEL.get(source, source)
        emoji = SOURCE_EMOJI.get(source, "")
        keyboard = [
            [InlineKeyboardButton("📋 Всі поставки", callback_data=f"dlv_all_{source}")],
            [InlineKeyboardButton("📅 На сьогодні", callback_data=f"dlv_today_{source}")],
            [InlineKeyboardButton("📅 На завтра", callback_data=f"dlv_tomorrow_{source}")],
            [InlineKeyboardButton("🔢 На конкретну дату", callback_data=f"dlv_pick_{source}")],
        ]
        await query.edit_message_text(
            f"{emoji} {label}. Обери що показати:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("dlv_all_"):
        source = data.replace("dlv_all_", "")
        await send_deliveries_query(query, context, source=source, filter_date=None)

    elif data.startswith("dlv_today_"):
        source = data.replace("dlv_today_", "")
        await send_deliveries_query(query, context, source=source, filter_date=date.today())

    elif data.startswith("dlv_tomorrow_"):
        source = data.replace("dlv_tomorrow_", "")
        await send_deliveries_query(query, context, source=source, filter_date=date.today() + timedelta(days=1))

    elif data.startswith("dlv_pick_"):
        source = data.replace("dlv_pick_", "")
        keyboard = []
        for i in range(7):
            d = date.today() + timedelta(days=i)
            label = d.strftime("%d.%m.%Y")
            keyboard.append([InlineKeyboardButton(label, callback_data=f"date_{label}_{source}")])
        await query.edit_message_text("Оберіть дату:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("date_"):
        rest = data.replace("date_", "")
        date_str, source = rest.rsplit("_", 1)
        try:
            filter_date = datetime.strptime(date_str, "%d.%m.%Y").date()
            await send_deliveries_query(query, context, source=source, filter_date=filter_date)
        except ValueError:
            await query.edit_message_text("Помилка дати")

    elif data == "report_yesterday":
        d = (date.today() - timedelta(days=1)).strftime("%d.%m.%Y")
        current_report_date[query.from_user.id] = d
        keyboard = [[InlineKeyboardButton("📋 Сформувати звіт", callback_data="build_report")]]
        await query.edit_message_text(
            f"✅ Дата звіту: {d}\n\nТепер скидайте оплати в групу.\nКоли закінчите — натисніть кнопку нижче.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "report_today":
        d = date.today().strftime("%d.%m.%Y")
        current_report_date[query.from_user.id] = d
        keyboard = [[InlineKeyboardButton("📋 Сформувати звіт", callback_data="build_report")]]
        await query.edit_message_text(
            f"✅ Дата звіту: {d}\n\nТепер скидайте оплати в групу.\nКоли закінчите — натисніть кнопку нижче.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "report_custom":
        current_report_date[query.from_user.id] = "waiting_date"
        await query.edit_message_text("Введіть дату у форматі ДД.ММ.РРРР:")

    elif data == "build_report":
        user_id = query.from_user.id
        if user_id not in current_report_date:
            await query.edit_message_text("❌ Спочатку оберіть дату звіту.")
            return
        report_date = current_report_date[user_id]
        reports, stats = build_report_and_stats(report_date)
        if not reports:
            await query.edit_message_text("❌ Немає даних для звіту.")
            return
        keyboard = [
            [InlineKeyboardButton("📋 Тільки звіт", callback_data=f"show_{report_date}_report")],
            [InlineKeyboardButton("📊 Тільки статистика", callback_data=f"show_{report_date}_stats")],
            [InlineKeyboardButton("📋+📊 Все разом", callback_data=f"show_{report_date}_both")],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"show_{report_date}_cancel")],
        ]
        await query.edit_message_text(
            f"Звіт за {report_date} готовий. Що показати?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "rback":
        keyboard, dates = get_reports_list_keyboard()
        if not dates:
            await query.edit_message_text("❌ Поки немає жодного звіту.")
        else:
            await query.edit_message_text("Оберіть дату звіту:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("rdelconfirm_"):
        d = data.replace("rdelconfirm_", "")
        db_delete_report(d)
        await query.edit_message_text(f"✅ Звіт за {d} видалено.")

    elif data.startswith("rdel_"):
        d = data.replace("rdel_", "")
        keyboard = [
            [InlineKeyboardButton("✅ Так, видалити", callback_data=f"rdelconfirm_{d}")],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"rdate_{d}")],
        ]
        await query.edit_message_text(
            f"Видалити звіт за {d}? Цю дію не можна скасувати.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("rview_"):
        d = data.replace("rview_", "")
        reports, stats = build_report_and_stats(d)
        if not reports:
            await query.edit_message_text(f"❌ Немає даних для звіту за {d}.")
            return
        keyboard = [
            [InlineKeyboardButton("📋 Тільки звіт", callback_data=f"show_{d}_report")],
            [InlineKeyboardButton("📊 Тільки статистика", callback_data=f"show_{d}_stats")],
            [InlineKeyboardButton("📋+📊 Все разом", callback_data=f"show_{d}_both")],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"show_{d}_cancel")],
        ]
        await query.edit_message_text(
            f"Звіт за {d} готовий. Що показати?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("rdate_"):
        d = data.replace("rdate_", "")
        keyboard = [
            [InlineKeyboardButton("👁 Переглянути", callback_data=f"rview_{d}")],
            [InlineKeyboardButton("✏️ Редагувати", callback_data=f"redit_{d}")],
            [InlineKeyboardButton("🗑 Видалити", callback_data=f"rdel_{d}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="rback")],
        ]
        await query.edit_message_text(f"Звіт за {d}:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("redit_"):
        d = data.replace("redit_", "")
        locations = compute_location_data(d)
        if not locations:
            await query.edit_message_text(f"❌ Немає даних для звіту за {d}.")
            return
        keyboard = []
        for idx, item in enumerate(locations):
            mark = "✏️ " if item['edited'] else ""
            label = f"{mark}{item['location']} — {format_money(item['income'])} грн ({item['total_workers']} люд.)"
            if len(label) > 64:
                label = label[:61] + "..."
            keyboard.append([InlineKeyboardButton(label, callback_data=f"rloc_{d}_{idx}")])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"rdate_{d}")])
        await query.edit_message_text(
            f"Позиції звіту за {d}\n(✏️ — вже відредаговано):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("rloc_"):
        rest = data.replace("rloc_", "")
        d, idx_str = rest.rsplit("_", 1)
        idx = int(idx_str)
        locations = compute_location_data(d)
        if idx >= len(locations):
            await query.edit_message_text(f"❌ Позицію не знайдено.")
            return
        item = locations[idx]
        text = (
            f"📍 {item['location']}\n\n"
            f"⏱ Години: {format_hours(item['hours'])}\n"
            f"👷 Кількість людей: {item['total_workers']}\n"
            f"💸 Сума виплат: {format_money(item['paid_to_workers'])} грн\n"
            f"💰 Дохід (за тарифом): {format_money(item['income'])} грн"
        )
        keyboard = [
            [InlineKeyboardButton("⏱ Змінити години", callback_data=f"rf_{d}_{idx}_hours")],
            [InlineKeyboardButton("👷 Змінити кількість людей", callback_data=f"rf_{d}_{idx}_workers")],
            [InlineKeyboardButton("💸 Змінити суму виплат", callback_data=f"rf_{d}_{idx}_paid")],
        ]
        if item['edited']:
            keyboard.append([InlineKeyboardButton("♻️ Скинути правки", callback_data=f"rreset_{d}_{idx}")])
        keyboard.append([InlineKeyboardButton("◀️ До списку позицій", callback_data=f"redit_{d}")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("rf_"):
        rest = data.replace("rf_", "")
        d, idx_str, field = rest.rsplit("_", 2)
        idx = int(idx_str)
        locations = compute_location_data(d)
        if idx >= len(locations):
            await query.edit_message_text(f"❌ Позицію не знайдено.")
            return
        location = locations[idx]['location']
        edit_state[query.from_user.id] = {"date": d, "location": location, "field": field, "idx": idx}
        await query.edit_message_text(
            f"📍 {location}\n\nВведіть нове значення для «{FIELD_LABELS[field]}»:"
        )

    elif data.startswith("rreset_"):
        rest = data.replace("rreset_", "")
        d, idx_str = rest.rsplit("_", 1)
        idx = int(idx_str)
        locations = compute_location_data(d)
        if idx >= len(locations):
            await query.edit_message_text(f"❌ Позицію не знайдено.")
            return
        location = locations[idx]['location']
        db_delete_override(d, location)
        await query.edit_message_text(f"✅ Правки для '{location}' скинуто до початкових значень.")

    elif data.startswith("show_"):
        rest = data.replace("show_", "")
        d, action = rest.rsplit("_", 1)

        if action == "cancel":
            await query.edit_message_text("Скасовано.")
            return

        reports, stats = build_report_and_stats(d)
        if not reports:
            await query.edit_message_text(f"❌ Немає даних для звіту за {d}.")
            return

        if action == "stats":
            await query.edit_message_text(f"📊 Статистика за {d}:")
        else:
            await query.edit_message_text(f"✅ Звіт за {d}:")

        await send_report_and_stats(context.bot, query.message.chat_id, d, reports, stats, mode=action)

    elif data == "waddnew":
        worker_flow_state[query.from_user.id] = {"mode": "add_name", "data": {}}
        await query.edit_message_text("Введіть ім'я нового працівника:")

    elif data == "wback":
        keyboard, workers = get_workers_list_keyboard()
        msg = "Робітники:" if workers else "❌ Поки немає жодного працівника."
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wdelconfirm_"):
        worker_id = int(data.replace("wdelconfirm_", ""))
        db_delete_worker(worker_id)
        keyboard, workers = get_workers_list_keyboard()
        msg = "✅ Видалено.\n\n" + ("Робітники:" if workers else "Поки немає жодного працівника.")
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wdel_"):
        worker_id = int(data.replace("wdel_", ""))
        keyboard = [
            [InlineKeyboardButton("✅ Так, видалити", callback_data=f"wdelconfirm_{worker_id}")],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"wview_{worker_id}")],
        ]
        await query.edit_message_text("Видалити цього працівника?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wf_"):
        rest = data.replace("wf_", "")
        worker_id_str, field = rest.rsplit("_", 1)
        worker_id = int(worker_id_str)
        worker_flow_state[query.from_user.id] = {"mode": "edit_field", "worker_id": worker_id, "field": field}
        await query.edit_message_text(f"Введіть нове значення для «{WORKER_FIELD_LABELS[field]}» (або «-», щоб очистити):")

    elif data.startswith("wview_"):
        worker_id = int(data.replace("wview_", ""))
        w = db_get_worker(worker_id)
        if not w:
            await query.edit_message_text("❌ Працівника не знайдено.")
            return
        await query.edit_message_text(
            format_worker_card(w),
            parse_mode="Markdown",
            reply_markup=get_worker_card_keyboard(worker_id)
        )

    elif data.startswith("cand_"):
        telegram_id = int(data.replace("cand_", ""))
        c = db_get_contact(telegram_id)
        if not c:
            await query.edit_message_text("❌ Кандидата не знайдено.")
            return
        keyboard = [
            [InlineKeyboardButton("➕ Додати в реєстр", callback_data=f"wclaim_{telegram_id}")],
            [InlineKeyboardButton("❌ Відхилити", callback_data=f"canddismiss_{telegram_id}")],
            [InlineKeyboardButton("◀️ До списку", callback_data="candback")],
        ]
        await query.edit_message_text(
            format_candidate_card(c),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "candback":
        keyboard, candidates = get_candidates_list_keyboard()
        msg = "Нові заявки (ще не в реєстрі):" if candidates else "❌ Поки немає нових заявок."
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("canddismiss_"):
        telegram_id = int(data.replace("canddismiss_", ""))
        db_dismiss_contact(telegram_id)
        keyboard, candidates = get_candidates_list_keyboard()
        msg = "✅ Заявку відхилено.\n\n" + ("Нові заявки (ще не в реєстрі):" if candidates else "Поки немає нових заявок.")
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wclaim_"):
        telegram_id = int(data.replace("wclaim_", ""))
        c = db_get_contact(telegram_id)
        if not c:
            await query.edit_message_text("❌ Кандидата не знайдено.")
            return

        existing = db_find_matching_worker(
            username=c.get("username") or "", phone=c.get("phone") or "", telegram_id=telegram_id
        )
        if existing:
            keyboard = [
                [InlineKeyboardButton("🔗 Так, це він — об'єднати", callback_data=f"wmergeconfirm_{telegram_id}_{existing['id']}")],
                [InlineKeyboardButton("➕ Ні, це інша людина", callback_data=f"wforcenew_{telegram_id}")],
            ]
            text = (
                f"⚠️ Схоже, такий працівник вже є в реєстрі:\n\n{format_worker_card(existing)}\n\n"
                f"Новий кандидат:\n{format_candidate_card(c)}"
            )
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        worker_id = db_add_worker(
            name=c.get("full_name_ua") or c.get("first_name") or f"ID {telegram_id}",
            phone=c.get("phone") or "",
            username=c.get("username") or "",
            telegram_id=telegram_id,
            city=c.get("city_raw") or "",
        )
        db_mark_converted(telegram_id, worker_id)
        w = db_get_worker(worker_id)
        await query.edit_message_text(
            f"✅ Додано в реєстр:\n\n{format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=get_worker_card_keyboard(worker_id)
        )

    elif data.startswith("wmergeconfirm_"):
        rest = data.replace("wmergeconfirm_", "")
        telegram_id_str, existing_id_str = rest.split("_")
        telegram_id, existing_id = int(telegram_id_str), int(existing_id_str)
        c = db_get_contact(telegram_id)
        if c:
            for field, val in (("phone", c.get("phone", "")), ("username", c.get("username", "")), ("city", c.get("city_raw", ""))):
                if val and not db_get_worker(existing_id).get(field):
                    db_update_worker(existing_id, field, val)
            w = db_get_worker(existing_id)
            if not w.get("telegram_id"):
                conn = get_conn()
                conn.execute("UPDATE workers SET telegram_id = ? WHERE id = ?", (telegram_id, existing_id))
                conn.commit()
                conn.close()
        db_mark_converted(telegram_id, existing_id)
        w = db_get_worker(existing_id)
        await query.edit_message_text(
            f"✅ Об'єднано:\n\n{format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=get_worker_card_keyboard(existing_id)
        )

    elif data.startswith("wforcenew_"):
        telegram_id = int(data.replace("wforcenew_", ""))
        c = db_get_contact(telegram_id)
        if not c:
            await query.edit_message_text("❌ Кандидата не знайдено.")
            return
        worker_id = db_add_worker(
            name=c.get("full_name_ua") or c.get("first_name") or f"ID {telegram_id}",
            phone=c.get("phone") or "",
            username=c.get("username") or "",
            telegram_id=telegram_id,
            city=c.get("city_raw") or "",
        )
        db_mark_converted(telegram_id, worker_id)
        w = db_get_worker(worker_id)
        await query.edit_message_text(
            f"✅ Додано окремим записом:\n\n{format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=get_worker_card_keyboard(worker_id)
        )

    elif data.startswith("wmergestart_"):
        worker_id = int(data.replace("wmergestart_", ""))
        await query.edit_message_text(
            "З ким об'єднати цього працівника?",
            reply_markup=get_merge_target_keyboard(worker_id)
        )

    elif data.startswith("wmergepick_"):
        rest = data.replace("wmergepick_", "")
        id1_str, id2_str = rest.split("_")
        id1, id2 = int(id1_str), int(id2_str)
        w1, w2 = db_get_worker(id1), db_get_worker(id2)
        if not w1 or not w2:
            await query.edit_message_text("❌ Одного з записів не знайдено.")
            return
        keyboard = [
            [InlineKeyboardButton("✅ Так, об'єднати", callback_data=f"wmergedo_{id1}_{id2}")],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"wview_{id1}")],
        ]
        text = f"Об'єднати ці два записи в один?\n\n{format_worker_card(w1)}\n\n➕\n\n{format_worker_card(w2)}"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wmergedo_"):
        rest = data.replace("wmergedo_", "")
        id1_str, id2_str = rest.split("_")
        id1, id2 = int(id1_str), int(id2_str)
        merged = db_merge_workers(id1, id2)
        await query.edit_message_text(
            f"✅ Об'єднано:\n\n{format_worker_card(merged)}",
            parse_mode="Markdown",
            reply_markup=get_worker_card_keyboard(id1)
        )

    elif data.startswith("wphoneadd_"):
        worker_id = int(data.replace("wphoneadd_", ""))
        worker_flow_state[query.from_user.id] = {"mode": "add_phone2", "worker_id": worker_id}
        await query.edit_message_text("Введіть додатковий номер телефону:")

    elif data.startswith("wphones_"):
        worker_id = int(data.replace("wphones_", ""))
        w = db_get_worker(worker_id)
        if not w:
            await query.edit_message_text("❌ Працівника не знайдено.")
            return
        await query.edit_message_text(
            f"Телефони — {w['name']}:",
            reply_markup=get_phones_management_keyboard(worker_id)
        )

    elif data.startswith("wphoneview_"):
        rest = data.replace("wphoneview_", "")
        worker_id_str, token = rest.split("_", 1)
        worker_id = int(worker_id_str)
        label = "Основний номер" if token == "main" else "Додатковий номер"
        await query.edit_message_text(
            f"{label}. Що зробити?",
            reply_markup=get_phone_action_keyboard(worker_id, token)
        )

    elif data.startswith("wphoneclearmain_"):
        worker_id = int(data.replace("wphoneclearmain_", ""))
        db_update_worker(worker_id, "phone", "")
        await query.edit_message_text(
            "✅ Основний номер очищено.",
            reply_markup=get_phones_management_keyboard(worker_id)
        )

    elif data.startswith("wphoneeditextra_"):
        rest = data.replace("wphoneeditextra_", "")
        phone_id_str, worker_id_str = rest.split("_")
        phone_id, worker_id = int(phone_id_str), int(worker_id_str)
        worker_flow_state[query.from_user.id] = {"mode": "edit_phone_extra", "phone_id": phone_id, "worker_id": worker_id}
        await query.edit_message_text("Введіть нове значення номера:")

    elif data.startswith("wphonepromote_"):
        rest = data.replace("wphonepromote_", "")
        phone_id_str, worker_id_str = rest.split("_")
        phone_id, worker_id = int(phone_id_str), int(worker_id_str)
        db_promote_worker_phone(worker_id, phone_id)
        w = db_get_worker(worker_id)
        await query.edit_message_text(
            f"✅ Готово.\n\n{format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=get_phones_management_keyboard(worker_id)
        )

    elif data.startswith("wphonedel_"):
        rest = data.replace("wphonedel_", "")
        phone_id_str, worker_id_str = rest.split("_")
        phone_id, worker_id = int(phone_id_str), int(worker_id_str)
        db_delete_worker_phone(phone_id)
        w = db_get_worker(worker_id)
        await query.edit_message_text(
            f"✅ Номер видалено.\n\n{format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=get_phones_management_keyboard(worker_id)
        )

    elif data.startswith("assignpick_"):
        rest = data.replace("assignpick_", "")
        delivery_key, worker_id_str = rest.rsplit("_", 1)
        db_assign_worker(delivery_key, int(worker_id_str))
        await query.edit_message_reply_markup(reply_markup=get_delivery_assign_keyboard(delivery_key))

    elif data.startswith("assignback_"):
        delivery_key = data.replace("assignback_", "")
        await query.edit_message_reply_markup(reply_markup=get_delivery_assign_keyboard(delivery_key))

    elif data.startswith("assign_"):
        delivery_key = data.replace("assign_", "")
        workers = db_get_workers()
        if not workers:
            await query.answer("Реєстр робітників порожній — спочатку додай когось у 👷 Робітники.", show_alert=True)
            return
        keyboard = [
            [InlineKeyboardButton(w["name"] or f"#{w['id']}", callback_data=f"assignpick_{delivery_key}_{w['id']}")]
            for w in workers
        ]
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"assignback_{delivery_key}")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("unassign_"):
        rest = data.replace("unassign_", "")
        delivery_key, worker_id_str = rest.rsplit("_", 1)
        db_unassign_worker(delivery_key, int(worker_id_str))
        await query.edit_message_reply_markup(reply_markup=get_delivery_assign_keyboard(delivery_key))

    elif data == "noop":
        pass  # інформаційна кнопка (наприклад "✅ Набрано" або ім'я без контакту) — нічого не робимо


# ==================== ВЕБ-ДАШБОРД МАРШРУТІВ ====================
def extract_phone_from_card(text: str):
    m = re.search(r'📞\s*(.+)$', text.strip())
    return m.group(1).strip() if m else None


def extract_time_minutes(text: str) -> int:
    m = re.search(r'🕐\s*(\d{1,2}):(\d{2})', text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 99999  # без часу — в кінець списку


def telegram_md_to_html(text: str) -> str:
    escaped = html_module.escape(text)
    escaped = re.sub(r'\*(.+?)\*', r'<b>\1</b>', escaped)
    return escaped.replace("\n", "<br>")


def build_driver_columns(target_date: date, date_str: str) -> dict:
    """Групує поставки FM+Ekol на дату по водію (за телефоном) і підвантажує
    реальні призначення робітників з бази — те, що бачить бот."""
    all_messages = []

    try:
        all_values, merged_cells = get_sheet_data(source="fm")
        routes = parse_routes(all_values, merged_cells)
        fm_msgs = build_delivery_messages(routes, merged_cells, filter_date=target_date)
        for m in fm_msgs:
            m["source"] = "fm"
        all_messages += fm_msgs
    except Exception as e:
        logger.error(f"Дашборд: помилка завантаження FM: {e}", exc_info=True)

    if EKOL_SPREADSHEET_ID:
        try:
            all_values_ekol, _ = get_sheet_data(source="ekol")
            ekol_msgs = parse_ekol_deliveries(all_values_ekol, filter_date=target_date)
            for m in ekol_msgs:
                m["source"] = "ekol"
            all_messages += ekol_msgs
        except Exception as e:
            logger.error(f"Дашборд: помилка завантаження Ekol: {e}", exc_info=True)

    columns = {}
    for m in all_messages:
        phone = extract_phone_from_card(m["text"]) or "Без номера водія"
        delivery_key = make_delivery_key(m["source"], m["date_str"], m["text"], needed=m.get("workers_needed"))
        assigned = db_get_assigned_workers(delivery_key)
        columns.setdefault(phone, []).append({
            "text": m["text"],
            "source": m["source"],
            "assigned": assigned,
            "needed": m.get("workers_needed"),
            "sort_key": extract_time_minutes(m["text"]),
        })

    for phone in columns:
        columns[phone].sort(key=lambda x: x["sort_key"])

    return columns


PX_PER_HOUR = 110
CARD_CENTER_OFFSET = 56  # приблизна половина висоти картки — щоб нитка проходила саме через центр
MIN_CARD_GAP = 110       # мінімальна відстань по вертикалі між сусідніми картками одного водія
HEADER_OFFSET = 44       # місце під заголовок колонки (номер водія) над першою карткою
DRIVER_COLORS = ["#2F5D46", "#3D5A73", "#8A5E74", "#8A6F2F", "#5E5A8A", "#2F7A6B", "#7A4A4A"]


def compute_time_range(columns: dict):
    minutes = [it["sort_key"] for items in columns.values() for it in items if it["sort_key"] < 99999]
    if not minutes:
        return 6 * 60, 20 * 60
    lo = max(0, (min(minutes) // 60) * 60 - 60)
    hi = min(24 * 60, ((max(minutes) // 60) + 1) * 60 + 60)
    if hi - lo < 120:
        hi = lo + 120
    return lo, hi


def assign_card_positions(columns: dict, lo: int) -> float:
    """Проставляє item['top'] (у пікселях, без урахування HEADER_OFFSET) для кожної картки.
    Повертає найбільший 'top' серед усіх колонок."""
    overall_max = 0.0
    for phone, items in columns.items():
        with_time = [it for it in items if it["sort_key"] < 99999]
        without_time = [it for it in items if it["sort_key"] >= 99999]

        prev_top = None
        for it in with_time:
            top = (it["sort_key"] - lo) / 60 * PX_PER_HOUR
            if prev_top is not None and top < prev_top + MIN_CARD_GAP:
                top = prev_top + MIN_CARD_GAP
            it["top"] = top
            prev_top = top

        next_top = (prev_top + MIN_CARD_GAP) if prev_top is not None else 0.0
        for it in without_time:
            it["top"] = next_top
            next_top += MIN_CARD_GAP

        col_items = with_time + without_time
        if col_items:
            overall_max = max(overall_max, max(it["top"] for it in col_items))

    return overall_max


def build_ruler_html(lo: int, hi: int, total_height: float) -> str:
    step = 60 if (hi - lo) <= 14 * 60 else 120
    ticks = ""
    m = lo
    while m <= hi:
        top = HEADER_OFFSET + (m - lo) / 60 * PX_PER_HOUR
        ticks += f'<div class="tick" style="top:{top}px;">{m // 60:02d}:{m % 60:02d}</div>'
        m += step

    periods = [
        ("РАНОК", lo, min(hi, 11 * 60)),
        ("ДЕНЬ", max(lo, 11 * 60), min(hi, 17 * 60)),
        ("ВЕЧІР", max(lo, 17 * 60), hi),
    ]
    period_html = ""
    for label, seg_lo, seg_hi in periods:
        if seg_hi > seg_lo:
            mid = (seg_lo + seg_hi) / 2
            top = HEADER_OFFSET + (mid - lo) / 60 * PX_PER_HOUR
            period_html += f'<div class="period" style="top:{top}px;">{label}</div>'

    return f'<div class="ruler" style="height:{total_height}px;">{period_html}{ticks}</div>'


DASHBOARD_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
html, body { height:100%; overflow:hidden; background:#E9E9E7; font-family:'Inter',-apple-system,sans-serif; color:#14201A; }
.app { display:flex; flex-direction:column; height:100vh; }
.header { padding:16px 20px; flex-shrink:0; }
.header-title { font-size:16px; font-weight:700; }
.nav { display:flex; align-items:center; gap:12px; margin-top:6px; }
.nav a { text-decoration:none; color:#2F5D46; font-weight:600; font-size:14px; padding:4px 10px; border-radius:8px; background:#F7F7F4; border:1px solid #ECECE8; }
.nav span { font-size:13px; color:#6B6B68; }
.board { flex:1; overflow:auto; -webkit-overflow-scrolling:touch; touch-action:pan-x pan-y; position:relative; background:#E9E9E7; }
.board-spacer { position:relative; }
.board-inner {
  position:absolute; top:0; left:0; display:flex; align-items:flex-start; gap:14px; padding:20px 20px 40px;
  transform-origin:0 0; will-change:transform;
  background-image: radial-gradient(circle, #D6D6D0 1.6px, transparent 1.6px);
  background-size: 22px 22px;
  background-position: 6px 6px;
}
.ruler { flex:0 0 56px; position:relative; }
.ruler .tick { position:absolute; left:0; right:8px; text-align:right; font-size:10.5px; color:#ADADA8; font-weight:500; transform:translateY(-50%); white-space:nowrap; }
.ruler .tick::after { content:''; position:absolute; right:-8px; top:50%; width:6px; height:1px; background:#C7C7C1; }
.ruler .period { position:absolute; left:0; font-size:9px; letter-spacing:0.1em; color:#B7B7B0; font-weight:700; writing-mode:vertical-rl; text-orientation:mixed; transform:translateY(-50%); }
.col { flex:0 0 250px; position:relative; }
.col-head { font-size:13px; font-weight:700; padding:8px 6px; color:#14201A; }
.card {
  position:absolute; left:6px; right:6px; z-index:2;
  background:#F7F7F4; border:1px solid #ECECE8; border-radius:12px; border-left-width:3px;
  padding:10px 12px; font-size:12.5px; line-height:1.5;
  box-shadow:0 1px 2px rgba(0,0,0,0.03), 0 4px 10px rgba(0,0,0,0.04);
}
.thread { position:absolute; left:calc(50% - 1px); width:2px; z-index:1; border-radius:2px; opacity:0.55; }
.badge { display:inline-block; font-size:10.5px; font-weight:600; padding:2px 8px; border-radius:20px; margin-top:6px; margin-right:4px; }
.badge.ok { background:#DCE8E0; color:#2F5D46; }
.badge.need { background:#F5E3D8; color:#8A5E2F; }
.worker-chip { display:inline-block; font-size:11px; background:#EFEFEA; color:#3D5A46; padding:2px 8px; border-radius:20px; margin:2px 4px 0 0; text-decoration:none; }
.empty { color:#B0B0AC; font-size:13px; padding:40px 20px; }
.src { font-size:10px; color:#B0B0AC; }
.zoom-controls { position:fixed; right:16px; bottom:16px; display:flex; flex-direction:column; gap:8px; z-index:20; }
.zoom-controls button { width:42px; height:42px; border-radius:50%; border:1px solid #ECECE8; background:white; font-size:19px; box-shadow:0 2px 10px rgba(0,0,0,0.12); cursor:pointer; color:#14201A; }
.zoom-controls button:active { background:#F0F0EC; }
"""

LOGIN_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#E9E9E7; font-family:'Inter',-apple-system,sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; }
.box { background:#F7F7F4; border:1px solid #ECECE8; border-radius:16px; padding:28px; width:280px; }
.box h1 { font-size:15px; margin-bottom:16px; color:#14201A; }
.box input { width:100%; padding:10px 12px; border:1px solid #ECECE8; border-radius:10px; font-size:14px; margin-bottom:12px; }
.box button { width:100%; padding:10px; border:none; border-radius:10px; background:#2F5D46; color:white; font-weight:600; font-size:14px; cursor:pointer; }
.box .err { color:#B23A3A; font-size:12.5px; margin-bottom:10px; }
"""


async def dashboard_handler(request):
    date_str = request.query.get("date") or date.today().strftime("%d.%m.%Y")
    try:
        target_date = datetime.strptime(date_str, "%d.%m.%Y").date()
    except ValueError:
        target_date = date.today()
        date_str = target_date.strftime("%d.%m.%Y")

    prev_date = (target_date - timedelta(days=1)).strftime("%d.%m.%Y")
    next_date = (target_date + timedelta(days=1)).strftime("%d.%m.%Y")

    columns = build_driver_columns(target_date, date_str)

    cols_html = ""
    if not columns:
        cols_html = '<div class="empty">Поставок на цю дату не знайдено.</div>'
    else:
        lo, hi = compute_time_range(columns)
        overall_max_top = assign_card_positions(columns, lo)
        total_height = HEADER_OFFSET + max(overall_max_top + 160, (hi - lo) / 60 * PX_PER_HOUR + 40)

        cols_html += build_ruler_html(lo, hi, total_height)

        for col_idx, (phone, items) in enumerate(columns.items()):
            color = DRIVER_COLORS[col_idx % len(DRIVER_COLORS)]
            cards_html = ""

            for idx, item in enumerate(items):
                top = HEADER_OFFSET + item["top"]

                if idx < len(items) - 1:
                    next_top = HEADER_OFFSET + items[idx + 1]["top"]
                    line_top = top + CARD_CENTER_OFFSET
                    line_h = (next_top + CARD_CENTER_OFFSET) - line_top
                    cards_html += (
                        f'<div class="thread" style="top:{line_top}px; height:{line_h}px; '
                        f'background:{color};"></div>'
                    )

                assigned = item["assigned"]
                needed = item["needed"]
                chips = ""
                for w in assigned:
                    if w.get("telegram_id"):
                        url = f"tg://user?id={w['telegram_id']}"
                        chips += f'<a class="worker-chip" href="{url}">{html_module.escape(w["name"])}</a>'
                    elif w.get("username"):
                        url = f"https://t.me/{w['username']}"
                        chips += f'<a class="worker-chip" href="{url}">{html_module.escape(w["name"])}</a>'
                    else:
                        chips += f'<span class="worker-chip">{html_module.escape(w["name"])}</span>'

                badge = ""
                if needed:
                    count = len(assigned)
                    cls = "ok" if count >= needed else "need"
                    badge = f'<span class="badge {cls}">{count}/{needed}</span>'

                src_emoji = SOURCE_EMOJI.get(item["source"], "")
                cards_html += (
                    f'<div class="card" style="top:{top}px; border-left-color:{color};">'
                    f'{telegram_md_to_html(item["text"])}'
                    f'<div>{badge}{chips}</div>'
                    f'<div class="src">{src_emoji} {SOURCE_LABEL.get(item["source"], "")}</div></div>'
                )

            cols_html += (
                f'<div class="col" style="height:{total_height}px;">'
                f'<div class="col-head">📞 {html_module.escape(phone)}</div>{cards_html}</div>'
            )

    page = f"""<!DOCTYPE html>
<html lang="uk"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Маршрути водіїв</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{DASHBOARD_CSS}</style>
</head><body>
<div class="app">
<div class="header">
  <div class="header-title">Маршрути водіїв</div>
  <div class="nav">
    <a href="/?date={prev_date}">← {prev_date}</a>
    <span>{date_str}</span>
    <a href="/?date={next_date}">{next_date} →</a>
  </div>
</div>
<div class="board" id="board">
  <div class="board-spacer" id="boardSpacer">
    <div class="board-inner" id="boardInner">{cols_html}</div>
  </div>
</div>
<div class="zoom-controls">
  <button id="zoomIn" aria-label="Наблизити">+</button>
  <button id="zoomOut" aria-label="Віддалити">−</button>
  <button id="zoomFit" aria-label="Показати все">⤢</button>
</div>
</div>
<script>
(function() {{
  var board = document.getElementById('board');
  var spacer = document.getElementById('boardSpacer');
  var inner = document.getElementById('boardInner');

  var zoom = 1, minZoom = 0.3, maxZoom = 2.5;
  var naturalW = 0, naturalH = 0;

  function measure() {{
    inner.style.transform = 'scale(1)';
    naturalW = inner.scrollWidth;
    naturalH = inner.scrollHeight;
  }}

  function applyZoom(z, focalX, focalY) {{
    z = Math.min(maxZoom, Math.max(minZoom, z));
    var ratio = z / zoom;
    var beforeX = (focalX !== undefined) ? board.scrollLeft + focalX : 0;
    var beforeY = (focalY !== undefined) ? board.scrollTop + focalY : 0;
    zoom = z;
    spacer.style.width = (naturalW * zoom) + 'px';
    spacer.style.height = (naturalH * zoom) + 'px';
    inner.style.transform = 'scale(' + zoom + ')';
    if (focalX !== undefined) {{
      board.scrollLeft = beforeX * ratio - focalX;
      board.scrollTop = beforeY * ratio - focalY;
    }}
  }}

  function fitAll() {{
    var fitZoom = Math.min(board.clientWidth / naturalW, board.clientHeight / naturalH);
    minZoom = Math.min(fitZoom, 1);
    applyZoom(fitZoom);
    board.scrollLeft = 0;
    board.scrollTop = 0;
  }}

  function dist(t1, t2) {{
    return Math.hypot(t1.clientX - t2.clientX, t1.clientY - t2.clientY);
  }}

  window.addEventListener('load', function() {{
    measure();
    var fitZoom = Math.min(board.clientWidth / naturalW, board.clientHeight / naturalH);
    minZoom = Math.min(fitZoom, 1);
    applyZoom(1);
  }});

  var pinchStartDist = null, pinchStartZoom = 1;

  board.addEventListener('touchstart', function(e) {{
    if (e.touches.length === 2) {{
      e.preventDefault();
      pinchStartDist = dist(e.touches[0], e.touches[1]);
      pinchStartZoom = zoom;
    }}
  }}, {{passive:false}});

  board.addEventListener('touchmove', function(e) {{
    if (e.touches.length === 2 && pinchStartDist) {{
      e.preventDefault();
      var d = dist(e.touches[0], e.touches[1]);
      var factor = d / pinchStartDist;
      var rect = board.getBoundingClientRect();
      var midX = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left;
      var midY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top;
      applyZoom(pinchStartZoom * factor, midX, midY);
    }}
  }}, {{passive:false}});

  board.addEventListener('touchend', function(e) {{
    if (e.touches.length < 2) pinchStartDist = null;
  }});

  board.addEventListener('wheel', function(e) {{
    if (e.ctrlKey) {{
      e.preventDefault();
      var rect = board.getBoundingClientRect();
      applyZoom(zoom * (e.deltaY < 0 ? 1.1 : 0.9), e.clientX - rect.left, e.clientY - rect.top);
    }}
  }}, {{passive:false}});

  document.getElementById('zoomIn').onclick = function() {{
    applyZoom(zoom * 1.3, board.clientWidth / 2, board.clientHeight / 2);
  }};
  document.getElementById('zoomOut').onclick = function() {{
    applyZoom(zoom / 1.3, board.clientWidth / 2, board.clientHeight / 2);
  }};
  document.getElementById('zoomFit').onclick = fitAll;
}})();
</script>
</body></html>"""

    return web.Response(text=page, content_type="text/html")


SESSION_SECRET = os.environ.get("SESSION_SECRET", BOT_TOKEN)
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 днів


def make_session_token() -> str:
    expiry = str(int(time.time()) + SESSION_MAX_AGE)
    sig = hmac.new(SESSION_SECRET.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def verify_session_token(token: str) -> bool:
    try:
        expiry, sig = token.split(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), expiry.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        return int(expiry) > int(time.time())
    except Exception:
        return False


def render_login_page(error: str = "") -> str:
    err_html = f'<div class="err">{html_module.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="uk"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Вхід</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{LOGIN_CSS}</style>
</head><body>
<form class="box" method="post" action="/login">
  <h1>Маршрути водіїв</h1>
  {err_html}
  <input type="password" name="password" placeholder="Пароль" autofocus>
  <button type="submit">Увійти</button>
</form>
</body></html>"""


async def login_handler(request):
    data = await request.post()
    pwd = data.get("password", "")
    if pwd == DASHBOARD_PASSWORD:
        resp = web.HTTPFound("/")
        resp.set_cookie(
            "dashboard_session", make_session_token(),
            max_age=SESSION_MAX_AGE, httponly=True, samesite="Lax"
        )
        return resp
    return web.Response(text=render_login_page("Невірний пароль"), content_type="text/html")


@web.middleware
async def auth_middleware(request, handler):
    if not DASHBOARD_PASSWORD:
        return web.Response(text="Дашборд не налаштовано: відсутня змінна DASHBOARD_PASSWORD.", status=503)

    if request.path == "/login":
        return await handler(request)

    token = request.cookies.get("dashboard_session", "")
    if verify_session_token(token):
        return await handler(request)

    return web.Response(text=render_login_page(), content_type="text/html")


def build_web_app() -> web.Application:
    webapp = web.Application(middlewares=[auth_middleware])
    webapp.router.add_get("/", dashboard_handler)
    webapp.router.add_post("/login", login_handler)
    return webapp


# ==================== MAIN ====================
async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mycities", mycities))
    app.add_handler(CommandHandler("addcity", addcity))
    app.add_handler(CommandHandler("removecity", removecity))
    app.add_handler(CommandHandler("addalias", addalias))
    app.add_handler(CommandHandler("removealias", removealias))

    # group=0: сначала пробуем перехватить сообщение в группе как "оплату"
    # (UpdateType.MESSAGE — явно тільки НОВІ повідомлення, інакше цей хендлер
    # перехоплював би й редагування раніше, ніж group_edited_message_handler)
    app.add_handler(MessageHandler(
        filters.Chat(REPORT_GROUP_ID) & filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE,
        group_message_handler
    ), group=0)

    # окреме прослуховування редагувань повідомлень у групі (Telegram шле це іншим типом update)
    app.add_handler(MessageHandler(
        filters.Chat(REPORT_GROUP_ID) & filters.TEXT & ~filters.COMMAND & filters.UpdateType.EDITED_MESSAGE,
        group_edited_message_handler
    ), group=0)

    # group=1: этот хендлер получит апдейт независимо от того, что сделал group_message_handler,
    # поэтому кнопки в группе тоже обрабатываются
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler), group=1)

    app.add_handler(CallbackQueryHandler(button_handler))

    webapp = build_web_app()
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    logger.info(f"Веб-дашборд запущено на порту {WEB_PORT}")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Бот запущено!")

    try:
        await asyncio.Event().wait()  # тримаємо процес живим, поки Railway не зупинить його
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())