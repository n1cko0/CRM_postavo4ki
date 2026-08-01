import asyncio

from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from aiohttp import web

import config
import db
import state
import handlers
import dashboard

# Підвантажуємо збережені вибори дат звітів (переживають рестарт бота) —
# робимо це тут, а не в db.py, щоб уникнути циклічного імпорту (state -> db -> state)
state.current_report_date.update(db.db_load_user_states())


async def main():
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("mycities", handlers.mycities))
    app.add_handler(CommandHandler("addcity", handlers.addcity))
    app.add_handler(CommandHandler("removecity", handlers.removecity))
    app.add_handler(CommandHandler("addalias", handlers.addalias))
    app.add_handler(CommandHandler("removealias", handlers.removealias))

    # group=0: сначала пробуем перехватить сообщение в группе как "оплату"
    # (UpdateType.MESSAGE — явно тільки НОВІ повідомлення, інакше цей хендлер
    # перехоплював би й редагування раніше, ніж group_edited_message_handler)
    app.add_handler(MessageHandler(
        filters.Chat(config.REPORT_GROUP_ID) & filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE,
        handlers.group_message_handler
    ), group=0)

    # окреме прослуховування редагувань повідомлень у групі (Telegram шле це іншим типом update)
    app.add_handler(MessageHandler(
        filters.Chat(config.REPORT_GROUP_ID) & filters.TEXT & ~filters.COMMAND & filters.UpdateType.EDITED_MESSAGE,
        handlers.group_edited_message_handler
    ), group=0)

    # group=1: этот хендлер получит апдейт независимо от того, что сделал group_message_handler,
    # поэтому кнопки в группе тоже обрабатываются
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text_handler), group=1)

    app.add_handler(CallbackQueryHandler(handlers.button_handler))

    webapp = dashboard.build_web_app()
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.WEB_PORT)
    await site.start()
    config.logger.info(f"Веб-дашборд запущено на порту {config.WEB_PORT}")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    config.logger.info("Бот запущено!")

    try:
        await asyncio.Event().wait()  # тримаємо процес живим, поки Railway не зупинить його
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())