import asyncio
from datetime import datetime, date, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter
from telegram.ext import ContextTypes

import config
import db
import parsing
import state as appstate
import ui


async def send_with_retry(bot, chat_id: int, text: str, parse_mode: str = None, reply_markup=None, max_retries: int = 5):
    """Отправка с обробкою Telegram flood control (RetryAfter) та інших тимчасових помилок."""
    for attempt in range(max_retries):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
            return
        except RetryAfter as e:
            wait = e.retry_after + 1
            config.logger.warning(f"Flood control, чекаємо {wait}с (спроба {attempt + 1})")
            await asyncio.sleep(wait)
        except Exception as e:
            config.logger.warning(f"Помилка відправки, чекаємо 2с: {e}")
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
        await send_with_retry(bot, chat_id, config.MY_CARD_NUMBER)
        await asyncio.sleep(0.3)

    if mode in ("stats", "both"):
        # особистий підсумок — тільки для мене
        await send_with_retry(bot, chat_id, parsing.format_stats_message(report_date, stats), parse_mode="Markdown")


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.id != config.REPORT_GROUP_ID:
        return

    sender = msg.from_user
    sender_id = sender.id if sender else None
    sender_name = sender.full_name if sender else "(немає from_user — можливо, анонім/канал)"
    text_preview = (msg.text or "")[:60]
    config.logger.info(f"[group_msg] отримано від {sender_name} (id={sender_id}): {text_preview!r}")

    if sender is None or sender_id not in config.ALLOWED_USERS:
        config.logger.info(f"[group_msg] ІГНОРУЮ: sender_id={sender_id} не в config.ALLOWED_USERS={config.ALLOWED_USERS}")
        return

    text = msg.text or ""
    if not text:
        config.logger.info("[group_msg] ІГНОРУЮ: порожній текст (не текстове повідомлення)")
        return
    if text in config.BUTTON_TEXTS:
        config.logger.info(f"[group_msg] ІГНОРУЮ: це кнопка меню ({text!r})")
        return

    user_id = sender_id
    if user_id in appstate.edit_state:
        config.logger.info(f"[group_msg] ІГНОРУЮ: user {user_id} зараз у appstate.edit_state")
        return
    if user_id in appstate.worker_flow_state:
        config.logger.info(f"[group_msg] ІГНОРУЮ: user {user_id} зараз у appstate.worker_flow_state")
        return
    if user_id not in appstate.current_report_date:
        config.logger.info(f"[group_msg] ІГНОРУЮ: user {user_id} не обирав дату звіту (appstate.current_report_date порожній)")
        return

    report_date = appstate.current_report_date[user_id]
    if report_date == "waiting_date":
        config.logger.info(f"[group_msg] ІГНОРУЮ: user {user_id} ще вводить дату (waiting_date)")
        return

    for attempt in range(3):
        try:
            db.db_add_message(report_date, text, msg.date.isoformat(), msg.message_id)
            config.logger.info(f"[group_msg] ✅ ЗБЕРЕЖЕНО за {report_date}: {text_preview!r}")
            return
        except Exception as e:
            config.logger.warning(f"Спроба {attempt + 1}: не вдалося зберегти повідомлення оплати: {e}")
            await asyncio.sleep(0.5)

    config.logger.error(f"НЕ ЗБЕРЕЖЕНО повідомлення оплати за {report_date}: {text!r}")
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
    if not msg or msg.chat.id != config.REPORT_GROUP_ID:
        return
    if msg.from_user.id not in config.ALLOWED_USERS:
        return

    text = msg.text or ""
    if not text or text in config.BUTTON_TEXTS:
        return

    updated = db.db_update_message_by_id(msg.message_id, text)
    if updated:
        config.logger.info(f"Оновлено відредаговане повідомлення {msg.message_id}: {text!r}")
        return

    # Повідомлення раніше не було збережено (наприклад, редагування прийшло
    # для чогось, що бот пропустив) — спробуємо додати його як нове,
    # якщо зараз активно триває збір звіту.
    user_id = msg.from_user.id
    if user_id in appstate.current_report_date and appstate.current_report_date[user_id] != "waiting_date":
        report_date = appstate.current_report_date[user_id]
        try:
            timestamp = msg.edit_date.isoformat() if msg.edit_date else datetime.now().isoformat()
            db.db_add_message(report_date, text, timestamp, msg.message_id)
            config.logger.info(f"Відредаговане повідомлення додано як нове: {msg.message_id}")
        except Exception as e:
            config.logger.error(f"Не вдалося зберегти відредаговане повідомлення: {e}")


# ==================== ВІДПРАВКА ПОСТАВОК ====================


async def send_deliveries_query(query, context: ContextTypes.DEFAULT_TYPE, source: str = "fm", filter_date: date = None):
    chat_id = query.message.chat_id
    bot = context.bot
    emoji = config.SOURCE_EMOJI.get(source, "")
    label = config.SOURCE_LABEL.get(source, source)
    await query.edit_message_text(f"⏳ {emoji} Завантажую поставки {label} з бази...")
    try:
        date_str = filter_date.strftime("%d.%m.%Y") if filter_date else None
        deliveries = db.db_get_deliveries(delivery_date=date_str, source=source)

        if not deliveries:
            date_info = date_str or ""
            await send_with_retry(
                bot, chat_id,
                f"❌ {emoji} Поставок {label} {'на ' + date_info if date_info else ''} не знайдено в базі.\n\n"
                f"Спробуй спочатку «🔄 Синхронізувати з таблиць»."
            )
            return

        date_info = date_str or "всі"
        await send_with_retry(
            bot, chat_id,
            f"✅ {emoji} {label}: знайдено поставок *{len(deliveries)}* (дата: {date_info})",
            parse_mode="Markdown"
        )

        for d in deliveries:
            text = parsing.format_delivery_card(d)
            await send_with_retry(
                bot, chat_id, text, parse_mode="Markdown",
                reply_markup=ui.get_delivery_assign_keyboard(d["id"])
            )
            await asyncio.sleep(0.35)

    except Exception as e:
        config.logger.error(f"Помилка: {e}", exc_info=True)
        await send_with_retry(bot, chat_id, f"❌ Помилка: {str(e)}")


# ==================== КОМАНДИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.db_touch_contact(user)

    if user.id not in config.ALLOWED_USERS:
        appstate.anketa_state[user.id] = {"step": "name", "data": {}}
        await update.message.reply_text(
            "👋 Вітаю! Щоб зв'язатися з вами щодо роботи, надішліть, будь ласка, "
            "невелику інформацію про себе.\n\nПрізвище та ім'я:"
        )
        return

    appstate.worker_flow_state.pop(user.id, None)
    appstate.edit_state.pop(user.id, None)
    await update.message.reply_text(
        "👋 Привіт! Я бот для поставок FM Logistics.\n\nОбери розділ:",
        reply_markup=ui.get_main_keyboard()
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
    for admin_id in config.ALLOWED_USERS:
        try:
            await send_with_retry(bot, admin_id, text, parse_mode="Markdown")
        except Exception as e:
            config.logger.warning(f"Не вдалося сповістити {admin_id} про нову заявку: {e}")


async def handle_anketa_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state = appstate.anketa_state[user.id]
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
        db.db_save_anketa(user.id, state["data"])
        del appstate.anketa_state[user.id]
        await update.message.reply_text("✅ Дякую! Ми зв'яжемося з вами за потреби.")
        await notify_admins_new_contact(context.bot, user, state["data"])
        return


async def mycities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in config.ALLOWED_USERS:
        return
    cities = db.load_cities()
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
    if update.effective_user.id not in config.ALLOWED_USERS:
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
    cities = db.load_cities()
    existing_aliases = cities.get(city_name, {}).get('aliases', [])
    cities[city_name] = {"rate": rate, "aliases": existing_aliases}
    db.save_cities(cities)
    await update.message.reply_text(f"✅ Додано: {city_name} — {rate} грн/год")


async def removecity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in config.ALLOWED_USERS:
        return
    if not context.args:
        await update.message.reply_text("Використання: /removecity Назва\nПриклад: /removecity Вінниця")
        return
    city_name = " ".join(context.args)
    cities = db.load_cities()
    if city_name in cities:
        del cities[city_name]
        db.save_cities(cities)
        await update.message.reply_text(f"✅ Видалено: {city_name}")
    else:
        await update.message.reply_text(f"❌ Місто '{city_name}' не знайдено.")


async def addalias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in config.ALLOWED_USERS:
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Використання: /addalias Назва Синонім\n"
            "Приклад: /addalias Могилів-Подільський Могилів"
        )
        return
    alias = context.args[-1]
    city_name = " ".join(context.args[:-1])
    cities = db.load_cities()
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
    db.save_cities(cities)
    await update.message.reply_text(f"✅ Додано синонім: '{alias}' → {city_name}")


async def removealias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in config.ALLOWED_USERS:
        return
    if not context.args:
        await update.message.reply_text("Використання: /removealias Синонім\nПриклад: /removealias Могилів")
        return
    alias = " ".join(context.args)
    cities = db.load_cities()
    for city_name, info in cities.items():
        aliases = info.get('aliases', [])
        new_aliases = [a for a in aliases if a.lower() != alias.lower()]
        if len(new_aliases) != len(aliases):
            info['aliases'] = new_aliases
            db.save_cities(cities)
            await update.message.reply_text(f"✅ Синонім '{alias}' видалено з '{city_name}'.")
            return
    await update.message.reply_text(f"❌ Синонім '{alias}' не знайдено.")


# ==================== TEXT HANDLER ====================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.db_touch_contact(user)

    if user.id not in config.ALLOWED_USERS:
        if user.id in appstate.anketa_state:
            await handle_anketa_step(update, context)
        return

    user_id = user.id
    incoming_text = update.message.text.strip()

    # Натискання кнопки меню скасовує будь-який незавершений флоу редагування —
    # інакше текст кнопки "проковтується" як відповідь на попереднє питання бота
    if incoming_text in config.BUTTON_TEXTS:
        appstate.worker_flow_state.pop(user_id, None)
        appstate.edit_state.pop(user_id, None)

    if user_id in appstate.worker_flow_state:
        state = appstate.worker_flow_state[user_id]
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
            worker_id = db.db_add_worker(**state["data"])
            del appstate.worker_flow_state[user_id]
            w = db.db_get_worker(worker_id)
            await update.message.reply_text(
                f"✅ Додано працівника:\n\n{ui.format_worker_card(w)}",
                parse_mode="Markdown",
                reply_markup=ui.get_worker_card_keyboard(worker_id)
            )
            return

        if mode == "edit_field":
            worker_id = state["worker_id"]
            field = state["field"]
            value = "" if raw == "-" else (raw.lstrip("@") if field == "username" else raw)
            db.db_update_worker(worker_id, field, value)
            del appstate.worker_flow_state[user_id]
            w = db.db_get_worker(worker_id)
            if w:
                await update.message.reply_text(
                    f"✅ Оновлено.\n\n{ui.format_worker_card(w)}",
                    parse_mode="Markdown",
                    reply_markup=ui.get_worker_card_keyboard(worker_id)
                )
            return

        if mode == "add_phone2":
            worker_id = state["worker_id"]
            if raw:
                db.db_add_worker_phone(worker_id, raw)
            del appstate.worker_flow_state[user_id]
            w = db.db_get_worker(worker_id)
            if w:
                await update.message.reply_text(
                    f"✅ Додано номер.\n\n{ui.format_worker_card(w)}",
                    parse_mode="Markdown",
                    reply_markup=ui.get_worker_card_keyboard(worker_id)
                )
            return

        if mode == "edit_phone_extra":
            phone_id = state["phone_id"]
            worker_id = state["worker_id"]
            if raw == "-":
                db.db_delete_worker_phone(phone_id)
            else:
                db.db_update_worker_phone(phone_id, raw)
            del appstate.worker_flow_state[user_id]
            w = db.db_get_worker(worker_id)
            if w:
                await update.message.reply_text(
                    f"✅ Оновлено.\n\n{ui.format_worker_card(w)}",
                    parse_mode="Markdown",
                    reply_markup=ui.get_phones_management_keyboard(worker_id)
                )
            return

    if user_id in appstate.edit_state:
        state = appstate.edit_state[user_id]
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
        override_key = config.FIELD_TO_OVERRIDE_KEY[field]

        db.db_set_override(report_date, location, override_key, value)

        del appstate.edit_state[user_id]

        keyboard = [
            [InlineKeyboardButton("✏️ Редагувати ще", callback_data=f"rloc_{report_date}_{state['idx']}")],
            [InlineKeyboardButton("◀️ До списку позицій", callback_data=f"redit_{report_date}")],
        ]
        await update.message.reply_text(
            f"✅ Оновлено: {location} → {config.FIELD_LABELS[field]} = {value}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if appstate.current_report_date.get(user_id) == "waiting_date":
        text_input = update.message.text.strip()
        try:
            datetime.strptime(text_input, "%d.%m.%Y")
            appstate.set_report_date(user_id, text_input)
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
            [InlineKeyboardButton("🔄 Синхронізувати з таблиць", callback_data="dlvsync")],
            [InlineKeyboardButton("🔴 FM", callback_data="dlvsrc_fm")],
            [InlineKeyboardButton("🔵 Ekol", callback_data="dlvsrc_ekol")],
        ]
        await update.message.reply_text(
            "Оберіть таблицю (або спочатку синхронізуй, якщо давно не робив цього):",
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
        keyboard, dates = ui.get_reports_list_keyboard()
        if not dates:
            await update.message.reply_text("❌ Поки немає жодного звіту.")
        else:
            await update.message.reply_text(
                "Оберіть дату звіту:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    elif text == "👷 Робітники":
        keyboard, workers = ui.get_workers_list_keyboard()
        msg = "Робітники:" if workers else "❌ Поки немає жодного працівника."
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif text == "📇 Кандидати":
        keyboard, candidates = ui.get_candidates_list_keyboard()
        msg = "Нові заявки (ще не в реєстрі):" if candidates else "❌ Поки немає нових заявок."
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif text == "💰 Борги логістів":
        await update.message.reply_text(parsing.build_income_summary(), parse_mode="Markdown")


# ==================== CALLBACK HANDLER ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in config.ALLOWED_USERS:
        await query.answer("⛔ Доступ заборонено.")
        return
    await query.answer()
    data = query.data

    if data == "dlvsync":
        await query.edit_message_text("⏳ Синхронізую поставки з таблиць...")
        result = parsing.sync_deliveries_from_sheets()
        text = (
            f"✅ Синхронізацію завершено.\n\n"
            f"🔴 FM: {result['fm_new']} нових з {result['fm_total']} у таблиці\n"
            f"🔵 Ekol: {result['ekol_new']} нових з {result['ekol_total']} у таблиці"
        )
        if result["errors"]:
            text += "\n\n⚠️ Помилки:\n" + "\n".join(result["errors"])
        keyboard = [
            [InlineKeyboardButton("🔴 FM", callback_data="dlvsrc_fm")],
            [InlineKeyboardButton("🔵 Ekol", callback_data="dlvsrc_ekol")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("dlvsrc_"):
        source = data.replace("dlvsrc_", "")
        label = config.SOURCE_LABEL.get(source, source)
        emoji = config.SOURCE_EMOJI.get(source, "")
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
        await send_deliveries_query(query, context, source=source, filter_date=config.kyiv_today())

    elif data.startswith("dlv_tomorrow_"):
        source = data.replace("dlv_tomorrow_", "")
        await send_deliveries_query(query, context, source=source, filter_date=config.kyiv_today() + timedelta(days=1))

    elif data.startswith("dlv_pick_"):
        source = data.replace("dlv_pick_", "")
        keyboard = []
        for i in range(7):
            d = config.kyiv_today() + timedelta(days=i)
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
        d = (config.kyiv_today() - timedelta(days=1)).strftime("%d.%m.%Y")
        appstate.set_report_date(query.from_user.id, d)
        keyboard = [[InlineKeyboardButton("📋 Сформувати звіт", callback_data="build_report")]]
        await query.edit_message_text(
            f"✅ Дата звіту: {d}\n\nТепер скидайте оплати в групу.\nКоли закінчите — натисніть кнопку нижче.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "report_today":
        d = config.kyiv_today().strftime("%d.%m.%Y")
        appstate.set_report_date(query.from_user.id, d)
        keyboard = [[InlineKeyboardButton("📋 Сформувати звіт", callback_data="build_report")]]
        await query.edit_message_text(
            f"✅ Дата звіту: {d}\n\nТепер скидайте оплати в групу.\nКоли закінчите — натисніть кнопку нижче.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "report_custom":
        appstate.set_report_date(query.from_user.id, "waiting_date")
        await query.edit_message_text("Введіть дату у форматі ДД.ММ.РРРР:")

    elif data == "build_report":
        user_id = query.from_user.id
        if user_id not in appstate.current_report_date:
            await query.edit_message_text("❌ Спочатку оберіть дату звіту.")
            return
        report_date = appstate.current_report_date[user_id]
        reports, stats = parsing.build_report_and_stats(report_date)
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
        keyboard, dates = ui.get_reports_list_keyboard()
        if not dates:
            await query.edit_message_text("❌ Поки немає жодного звіту.")
        else:
            await query.edit_message_text("Оберіть дату звіту:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("rdelconfirm_"):
        d = data.replace("rdelconfirm_", "")
        db.db_delete_report(d)
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
        reports, stats = parsing.build_report_and_stats(d)
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
        locations = parsing.compute_location_data(d)
        if not locations:
            await query.edit_message_text(f"❌ Немає даних для звіту за {d}.")
            return
        keyboard = []
        for idx, item in enumerate(locations):
            mark = "✏️ " if item['edited'] else ""
            label = f"{mark}{item['location']} — {parsing.format_money(item['income'])} грн ({item['total_workers']} люд.)"
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
        locations = parsing.compute_location_data(d)
        if idx >= len(locations):
            await query.edit_message_text(f"❌ Позицію не знайдено.")
            return
        item = locations[idx]
        text = (
            f"📍 {item['location']}\n\n"
            f"⏱ Години: {parsing.format_hours(item['hours'])}\n"
            f"👷 Кількість людей: {item['total_workers']}\n"
            f"💸 Сума виплат: {parsing.format_money(item['paid_to_workers'])} грн\n"
            f"💰 Дохід (за тарифом): {parsing.format_money(item['income'])} грн"
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
        locations = parsing.compute_location_data(d)
        if idx >= len(locations):
            await query.edit_message_text(f"❌ Позицію не знайдено.")
            return
        location = locations[idx]['location']
        appstate.edit_state[query.from_user.id] = {"date": d, "location": location, "field": field, "idx": idx}
        await query.edit_message_text(
            f"📍 {location}\n\nВведіть нове значення для «{config.FIELD_LABELS[field]}»:"
        )

    elif data.startswith("rreset_"):
        rest = data.replace("rreset_", "")
        d, idx_str = rest.rsplit("_", 1)
        idx = int(idx_str)
        locations = parsing.compute_location_data(d)
        if idx >= len(locations):
            await query.edit_message_text(f"❌ Позицію не знайдено.")
            return
        location = locations[idx]['location']
        db.db_delete_override(d, location)
        await query.edit_message_text(f"✅ Правки для '{location}' скинуто до початкових значень.")

    elif data.startswith("show_"):
        rest = data.replace("show_", "")
        d, action = rest.rsplit("_", 1)

        if action == "cancel":
            await query.edit_message_text("Скасовано.")
            return

        reports, stats = parsing.build_report_and_stats(d)
        if not reports:
            await query.edit_message_text(f"❌ Немає даних для звіту за {d}.")
            return

        if action == "stats":
            await query.edit_message_text(f"📊 Статистика за {d}:")
        else:
            await query.edit_message_text(f"✅ Звіт за {d}:")

        await send_report_and_stats(context.bot, query.message.chat_id, d, reports, stats, mode=action)

    elif data == "waddnew":
        appstate.worker_flow_state[query.from_user.id] = {"mode": "add_name", "data": {}}
        await query.edit_message_text("Введіть ім'я нового працівника:")

    elif data == "wback":
        keyboard, workers = ui.get_workers_list_keyboard()
        msg = "Робітники:" if workers else "❌ Поки немає жодного працівника."
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wdelconfirm_"):
        worker_id = int(data.replace("wdelconfirm_", ""))
        db.db_delete_worker(worker_id)
        keyboard, workers = ui.get_workers_list_keyboard()
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
        appstate.worker_flow_state[query.from_user.id] = {"mode": "edit_field", "worker_id": worker_id, "field": field}
        await query.edit_message_text(f"Введіть нове значення для «{config.WORKER_FIELD_LABELS[field]}» (або «-», щоб очистити):")

    elif data.startswith("wview_"):
        worker_id = int(data.replace("wview_", ""))
        w = db.db_get_worker(worker_id)
        if not w:
            await query.edit_message_text("❌ Працівника не знайдено.")
            return
        await query.edit_message_text(
            ui.format_worker_card(w),
            parse_mode="Markdown",
            reply_markup=ui.get_worker_card_keyboard(worker_id)
        )

    elif data.startswith("cand_"):
        telegram_id = int(data.replace("cand_", ""))
        c = db.db_get_contact(telegram_id)
        if not c:
            await query.edit_message_text("❌ Кандидата не знайдено.")
            return
        keyboard = [
            [InlineKeyboardButton("➕ Додати в реєстр", callback_data=f"wclaim_{telegram_id}")],
            [InlineKeyboardButton("❌ Відхилити", callback_data=f"canddismiss_{telegram_id}")],
            [InlineKeyboardButton("◀️ До списку", callback_data="candback")],
        ]
        await query.edit_message_text(
            ui.format_candidate_card(c),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "candback":
        keyboard, candidates = ui.get_candidates_list_keyboard()
        msg = "Нові заявки (ще не в реєстрі):" if candidates else "❌ Поки немає нових заявок."
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("canddismiss_"):
        telegram_id = int(data.replace("canddismiss_", ""))
        db.db_dismiss_contact(telegram_id)
        keyboard, candidates = ui.get_candidates_list_keyboard()
        msg = "✅ Заявку відхилено.\n\n" + ("Нові заявки (ще не в реєстрі):" if candidates else "Поки немає нових заявок.")
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wclaim_"):
        telegram_id = int(data.replace("wclaim_", ""))
        c = db.db_get_contact(telegram_id)
        if not c:
            await query.edit_message_text("❌ Кандидата не знайдено.")
            return

        existing = db.db_find_matching_worker(
            username=c.get("username") or "", phone=c.get("phone") or "", telegram_id=telegram_id
        )
        if existing:
            keyboard = [
                [InlineKeyboardButton("🔗 Так, це він — об'єднати", callback_data=f"wmergeconfirm_{telegram_id}_{existing['id']}")],
                [InlineKeyboardButton("➕ Ні, це інша людина", callback_data=f"wforcenew_{telegram_id}")],
            ]
            text = (
                f"⚠️ Схоже, такий працівник вже є в реєстрі:\n\n{ui.format_worker_card(existing)}\n\n"
                f"Новий кандидат:\n{ui.format_candidate_card(c)}"
            )
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        worker_id = db.db_add_worker(
            name=c.get("full_name_ua") or c.get("first_name") or f"ID {telegram_id}",
            phone=c.get("phone") or "",
            username=c.get("username") or "",
            telegram_id=telegram_id,
            city=c.get("city_raw") or "",
        )
        db.db_mark_converted(telegram_id, worker_id)
        w = db.db_get_worker(worker_id)
        await query.edit_message_text(
            f"✅ Додано в реєстр:\n\n{ui.format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=ui.get_worker_card_keyboard(worker_id)
        )

    elif data.startswith("wmergeconfirm_"):
        rest = data.replace("wmergeconfirm_", "")
        telegram_id_str, existing_id_str = rest.split("_")
        telegram_id, existing_id = int(telegram_id_str), int(existing_id_str)
        c = db.db_get_contact(telegram_id)
        if c:
            for field, val in (("phone", c.get("phone", "")), ("username", c.get("username", "")), ("city", c.get("city_raw", ""))):
                if val and not db.db_get_worker(existing_id).get(field):
                    db.db_update_worker(existing_id, field, val)
            w = db.db_get_worker(existing_id)
            if not w.get("telegram_id"):
                conn = db.get_conn()
                conn.execute("UPDATE workers SET telegram_id = ? WHERE id = ?", (telegram_id, existing_id))
                conn.commit()
                conn.close()
        db.db_mark_converted(telegram_id, existing_id)
        w = db.db_get_worker(existing_id)
        await query.edit_message_text(
            f"✅ Об'єднано:\n\n{ui.format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=ui.get_worker_card_keyboard(existing_id)
        )

    elif data.startswith("wforcenew_"):
        telegram_id = int(data.replace("wforcenew_", ""))
        c = db.db_get_contact(telegram_id)
        if not c:
            await query.edit_message_text("❌ Кандидата не знайдено.")
            return
        worker_id = db.db_add_worker(
            name=c.get("full_name_ua") or c.get("first_name") or f"ID {telegram_id}",
            phone=c.get("phone") or "",
            username=c.get("username") or "",
            telegram_id=telegram_id,
            city=c.get("city_raw") or "",
        )
        db.db_mark_converted(telegram_id, worker_id)
        w = db.db_get_worker(worker_id)
        await query.edit_message_text(
            f"✅ Додано окремим записом:\n\n{ui.format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=ui.get_worker_card_keyboard(worker_id)
        )

    elif data.startswith("wmergestart_"):
        worker_id = int(data.replace("wmergestart_", ""))
        await query.edit_message_text(
            "З ким об'єднати цього працівника?",
            reply_markup=ui.get_merge_target_keyboard(worker_id)
        )

    elif data.startswith("wmergepick_"):
        rest = data.replace("wmergepick_", "")
        id1_str, id2_str = rest.split("_")
        id1, id2 = int(id1_str), int(id2_str)
        w1, w2 = db.db_get_worker(id1), db.db_get_worker(id2)
        if not w1 or not w2:
            await query.edit_message_text("❌ Одного з записів не знайдено.")
            return
        keyboard = [
            [InlineKeyboardButton("✅ Так, об'єднати", callback_data=f"wmergedo_{id1}_{id2}")],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"wview_{id1}")],
        ]
        text = f"Об'єднати ці два записи в один?\n\n{ui.format_worker_card(w1)}\n\n➕\n\n{ui.format_worker_card(w2)}"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wmergedo_"):
        rest = data.replace("wmergedo_", "")
        id1_str, id2_str = rest.split("_")
        id1, id2 = int(id1_str), int(id2_str)
        merged = db.db_merge_workers(id1, id2)
        await query.edit_message_text(
            f"✅ Об'єднано:\n\n{ui.format_worker_card(merged)}",
            parse_mode="Markdown",
            reply_markup=ui.get_worker_card_keyboard(id1)
        )

    elif data.startswith("wphoneadd_"):
        worker_id = int(data.replace("wphoneadd_", ""))
        appstate.worker_flow_state[query.from_user.id] = {"mode": "add_phone2", "worker_id": worker_id}
        await query.edit_message_text("Введіть додатковий номер телефону:")

    elif data.startswith("wphones_"):
        worker_id = int(data.replace("wphones_", ""))
        w = db.db_get_worker(worker_id)
        if not w:
            await query.edit_message_text("❌ Працівника не знайдено.")
            return
        await query.edit_message_text(
            f"Телефони — {w['name']}:",
            reply_markup=ui.get_phones_management_keyboard(worker_id)
        )

    elif data.startswith("wphoneview_"):
        rest = data.replace("wphoneview_", "")
        worker_id_str, token = rest.split("_", 1)
        worker_id = int(worker_id_str)
        label = "Основний номер" if token == "main" else "Додатковий номер"
        await query.edit_message_text(
            f"{label}. Що зробити?",
            reply_markup=ui.get_phone_action_keyboard(worker_id, token)
        )

    elif data.startswith("wphoneclearmain_"):
        worker_id = int(data.replace("wphoneclearmain_", ""))
        db.db_update_worker(worker_id, "phone", "")
        await query.edit_message_text(
            "✅ Основний номер очищено.",
            reply_markup=ui.get_phones_management_keyboard(worker_id)
        )

    elif data.startswith("wphoneeditextra_"):
        rest = data.replace("wphoneeditextra_", "")
        phone_id_str, worker_id_str = rest.split("_")
        phone_id, worker_id = int(phone_id_str), int(worker_id_str)
        appstate.worker_flow_state[query.from_user.id] = {"mode": "edit_phone_extra", "phone_id": phone_id, "worker_id": worker_id}
        await query.edit_message_text("Введіть нове значення номера:")

    elif data.startswith("wphonepromote_"):
        rest = data.replace("wphonepromote_", "")
        phone_id_str, worker_id_str = rest.split("_")
        phone_id, worker_id = int(phone_id_str), int(worker_id_str)
        db.db_promote_worker_phone(worker_id, phone_id)
        w = db.db_get_worker(worker_id)
        await query.edit_message_text(
            f"✅ Готово.\n\n{ui.format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=ui.get_phones_management_keyboard(worker_id)
        )

    elif data.startswith("wphonedel_"):
        rest = data.replace("wphonedel_", "")
        phone_id_str, worker_id_str = rest.split("_")
        phone_id, worker_id = int(phone_id_str), int(worker_id_str)
        db.db_delete_worker_phone(phone_id)
        w = db.db_get_worker(worker_id)
        await query.edit_message_text(
            f"✅ Номер видалено.\n\n{ui.format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=ui.get_phones_management_keyboard(worker_id)
        )

    elif data.startswith("assignpick_"):
        rest = data.replace("assignpick_", "")
        delivery_id_str, worker_id_str = rest.split("_")
        delivery_id = int(delivery_id_str)
        db.db_assign_worker(delivery_id, int(worker_id_str))
        await query.edit_message_reply_markup(reply_markup=ui.get_delivery_assign_keyboard(delivery_id))

    elif data.startswith("assignback_"):
        delivery_id = int(data.replace("assignback_", ""))
        await query.edit_message_reply_markup(reply_markup=ui.get_delivery_assign_keyboard(delivery_id))

    elif data.startswith("assign_"):
        delivery_id = int(data.replace("assign_", ""))
        workers = db.db_get_workers()
        if not workers:
            await query.answer("Реєстр робітників порожній — спочатку додай когось у 👷 Робітники.", show_alert=True)
            return
        keyboard = [
            [InlineKeyboardButton(w["name"] or f"#{w['id']}", callback_data=f"assignpick_{delivery_id}_{w['id']}")]
            for w in workers
        ]
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"assignback_{delivery_id}")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("unassign_"):
        rest = data.replace("unassign_", "")
        delivery_id_str, worker_id_str = rest.split("_")
        delivery_id = int(delivery_id_str)
        db.db_unassign_worker(delivery_id, int(worker_id_str))
        await query.edit_message_reply_markup(reply_markup=ui.get_delivery_assign_keyboard(delivery_id))

    elif data == "noop":
        pass  # інформаційна кнопка (наприклад "✅ Набрано" або ім'я без контакту) — нічого не робимо