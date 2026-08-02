import json
import os
import re
import sqlite3
from datetime import datetime

from config import DB_FILE, CITIES_FILE, REPORTS_FILE, logger


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
            delivery_key TEXT,
            delivery_id INTEGER,
            worker_id INTEGER NOT NULL,
            created_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_assignments_key ON delivery_assignments(delivery_key)")
    da_cols = [r["name"] for r in conn.execute("PRAGMA table_info(delivery_assignments)")]
    if "delivery_id" not in da_cols:
        conn.execute("ALTER TABLE delivery_assignments ADD COLUMN delivery_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_assignments_delivery_id ON delivery_assignments(delivery_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            delivery_date TEXT NOT NULL,
            city TEXT NOT NULL,
            detail TEXT,
            brand TEXT,
            boxes TEXT,
            workers_needed INTEGER,
            hours REAL,
            time TEXT,
            driver_phone TEXT,
            import_key TEXT UNIQUE,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_date ON deliveries(delivery_date)")
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER PRIMARY KEY,
            report_date TEXT
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
    """ORDER BY message_id (а не за нашим внутрішнім id) — message_id призначає сам
    Telegram строго за реальним порядком відправки в чаті, тому навіть якщо бот
    отримав повідомлення не в тому порядку (буває при пересиланні), звіт все одно
    збереться правильно."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT text, timestamp, message_id FROM payment_messages WHERE report_date = ? "
        "ORDER BY COALESCE(message_id, id)",
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


def db_set_user_report_date(user_id: int, report_date: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO user_state (user_id, report_date) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET report_date = excluded.report_date",
        (user_id, report_date)
    )
    conn.commit()
    conn.close()


def db_load_user_states() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT user_id, report_date FROM user_state").fetchall()
    conn.close()
    return {r["user_id"]: r["report_date"] for r in rows}


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
def db_assign_worker(delivery_id: int, worker_id: int):
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM delivery_assignments WHERE delivery_id = ? AND worker_id = ?",
        (delivery_id, worker_id)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO delivery_assignments (delivery_id, worker_id, created_at) VALUES (?, ?, ?)",
            (delivery_id, worker_id, datetime.now().isoformat())
        )
        conn.commit()
    conn.close()


def db_unassign_worker(delivery_id: int, worker_id: int):
    conn = get_conn()
    conn.execute(
        "DELETE FROM delivery_assignments WHERE delivery_id = ? AND worker_id = ?",
        (delivery_id, worker_id)
    )
    conn.commit()
    conn.close()


def db_get_assigned_workers(delivery_id: int) -> list:
    conn = get_conn()
    rows = conn.execute("""
        SELECT w.id, w.name, w.username, w.telegram_id FROM delivery_assignments da
        JOIN workers w ON w.id = da.worker_id
        WHERE da.delivery_id = ?
        ORDER BY da.id
    """, (delivery_id,)).fetchall()
    conn.close()
    return [
        {"id": r["id"], "name": r["name"], "username": r["username"] or "", "telegram_id": r["telegram_id"]}
        for r in rows
    ]


# ==================== ПОСТАВКИ (свої записи, більше не читаються "наживо" з Sheets) ====================
def db_upsert_delivery(record: dict):
    """Вставляє нову поставку, якщо такого import_key ще немає (за ним і визначається
    'та сама поставка' між синхронізаціями). Якщо вже є — нічого не робить,
    не зачіпає ручні правки (години, кількість коробок і т.д.)."""
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM deliveries WHERE import_key = ?", (record["import_key"],)
    ).fetchone()
    if existing:
        conn.close()
        return existing["id"], False

    now = datetime.now().isoformat()
    cur = conn.execute(
        """INSERT INTO deliveries
           (source, delivery_date, city, detail, brand, boxes, workers_needed, time,
            driver_phone, import_key, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record["source"], record["delivery_date"], record["city"], record.get("detail", ""),
            record.get("brand", ""), record.get("boxes", ""), record.get("workers_needed"),
            record.get("time", ""), record.get("driver_phone", ""), record["import_key"], now, now,
        )
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id, True


def db_get_deliveries(delivery_date: str = None, source: str = None, include_deleted: bool = False) -> list:
    conn = get_conn()
    query = "SELECT * FROM deliveries WHERE 1=1"
    params = []
    if delivery_date:
        query += " AND delivery_date = ?"
        params.append(delivery_date)
    if source:
        query += " AND source = ?"
        params.append(source)
    if not include_deleted:
        query += " AND is_deleted = 0"
    query += " ORDER BY time, id"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_get_delivery(delivery_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def db_update_delivery(delivery_id: int, field: str, value):
    column = {
        "city": "city", "detail": "detail", "brand": "brand", "boxes": "boxes",
        "workers_needed": "workers_needed", "hours": "hours", "time": "time",
        "driver_phone": "driver_phone", "delivery_date": "delivery_date",
    }.get(field)
    if not column:
        raise ValueError(f"Невідоме поле поставки: {field}")
    conn = get_conn()
    conn.execute(
        f"UPDATE deliveries SET {column} = ?, updated_at = ? WHERE id = ?",
        (value, datetime.now().isoformat(), delivery_id)
    )
    conn.commit()
    conn.close()


def db_delete_delivery(delivery_id: int):
    """М'яке видалення — запис лишається в базі (з призначеннями), просто ховається зі списків."""
    conn = get_conn()
    conn.execute(
        "UPDATE deliveries SET is_deleted = 1, updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), delivery_id)
    )
    conn.commit()
    conn.close()


def db_restore_delivery(delivery_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE deliveries SET is_deleted = 0, updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), delivery_id)
    )
    conn.commit()
    conn.close()


def db_add_manual_delivery(source: str, delivery_date: str, city: str, **kwargs) -> int:
    """Створює поставку вручну (не через синхронізацію) — свій унікальний import_key,
    щоб не конфліктувати з тими, що прийдуть при наступній синхронізації."""
    import uuid
    record = {
        "source": source, "delivery_date": delivery_date, "city": city,
        "import_key": f"manual:{uuid.uuid4().hex}",
        **kwargs,
    }
    delivery_id, _ = db_upsert_delivery(record)
    return delivery_id


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