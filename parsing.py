import difflib
import re
from datetime import datetime, date

import gspread
from google.oauth2.service_account import Credentials

import db
from config import CREDENTIALS_FILE, SCOPES, FM_EXCLUDED_CITIES, logger


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
    cities = db.load_cities()
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
    cities = db.load_cities()
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
    index = db.build_city_index(cities)
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
    index = db.build_city_index(cities)

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
    messages = db.db_get_messages(report_date)
    if not messages:
        return []
    overrides = db.db_get_overrides(report_date)

    cities = db.load_cities()

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