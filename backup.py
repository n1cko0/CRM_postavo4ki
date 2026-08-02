import asyncio
from datetime import datetime, timedelta

import config

BACKUP_HOUR = 3  # 03:00 за київським часом


async def send_db_backup(bot):
    """Надсилає поточний файл бази даних усім дозволеним користувачам у особисті повідомлення."""
    date_str = config.kyiv_today().strftime("%d.%m.%Y")
    for user_id in config.ALLOWED_USERS:
        try:
            with open(config.DB_FILE, "rb") as f:
                await bot.send_document(
                    chat_id=user_id,
                    document=f,
                    filename=f"postavo4ki_backup_{date_str}.db",
                    caption=f"🗄 Щоденний бекап бази за {date_str}"
                )
            config.logger.info(f"Бекап бази надіслано користувачу {user_id}")
        except Exception as e:
            config.logger.error(f"Не вдалося надіслати бекап користувачу {user_id}: {e}")


async def daily_backup_loop(bot):
    """Раз на добу, о 03:00 за Києвом, надсилає файл бази даних обом дозволеним
    користувачам — щоб завжди була свіжа копія на випадок проблем з Volume
    (як це вже одного разу сталося через випадкову помилку в шляху до файлу)."""
    while True:
        now = datetime.now(config.KYIV_TZ)
        next_run = now.replace(hour=BACKUP_HOUR, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()
        config.logger.info(
            f"Наступний бекап бази: {next_run.strftime('%d.%m.%Y %H:%M')} за Києвом "
            f"(через {wait_seconds / 3600:.1f} год)"
        )
        await asyncio.sleep(wait_seconds)
        await send_db_backup(bot)


async def backup_now_command(update, context):
    """/backupnow — ручний тригер бекапу прямо зараз, щоб перевірити, не чекаючи ночі."""
    if update.effective_user.id not in config.ALLOWED_USERS:
        return
    await update.message.reply_text("⏳ Готую бекап бази...")
    await send_db_backup(context.bot)