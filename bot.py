import os
import re
import hashlib
import logging
from io import BytesIO

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from extractors import extract
from analyzer import analyze
from keywords_list import KEYWORDS_MESSAGE

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OUTPUT_CHAT_ID = int(os.environ["OUTPUT_CHAT_ID"])
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))

WAITING_DOC, WAITING_LINK = range(2)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("tenderbot")


def allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def safe_basename(name: str) -> str:
    stem = name.rsplit(".", 1)[0]
    stem = re.sub(r"[^\w\-. ]+", "_", stem, flags=re.UNICODE)
    return stem[:80] or "tender"


HELP_TEXT = (
    "Команды:\n"
    "/start — начать анализ ТЗ (пришли документ, потом ссылку)\n"
    "/keywords — ключевые слова для поиска лотов\n"
    "/cancel — отменить текущий анализ\n"
    "/help — это сообщение"
)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update.effective_user.id):
        await update.message.reply_text("Нет доступа.")
        return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text(
        "Привет. Пришли документ техзадания (PDF или DOCX).\n"
        "Команды: /keywords — ключевые слова, /cancel — отмена."
    )
    return WAITING_DOC


async def receive_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update.effective_user.id):
        return ConversationHandler.END

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Пришли файл PDF или DOCX.")
        return WAITING_DOC

    name = doc.file_name or "document"
    lower = name.lower()
    if lower.endswith(".doc"):
        await update.message.reply_text(
            "Старый формат .doc не поддерживается. Пересохрани в .docx и пришли снова."
        )
        return WAITING_DOC
    if not lower.endswith((".pdf", ".docx")):
        await update.message.reply_text("Поддерживаются только PDF и DOCX.")
        return WAITING_DOC

    await update.message.reply_text("Получил. Извлекаю текст…")

    try:
        tg_file = await doc.get_file()
        data = await tg_file.download_as_bytearray()
        text = extract(name, bytes(data))
    except Exception as e:
        log.exception("extract failed")
        await update.message.reply_text(f"Ошибка извлечения текста: {e}")
        return WAITING_DOC

    if len(text.strip()) < 200:
        await update.message.reply_text(
            "Не удалось извлечь осмысленный текст. Похоже, это скан без OCR — "
            "прогони через OCR (например, ABBYY FineReader или Adobe) и пришли снова."
        )
        return WAITING_DOC

    ctx.user_data["file_id"] = doc.file_id
    ctx.user_data["file_name"] = name
    ctx.user_data["text"] = text

    await update.message.reply_text(
        f"Текст извлечён ({len(text)} символов). Теперь пришли ссылку на тендер."
    )
    return WAITING_LINK


async def receive_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update.effective_user.id):
        return ConversationHandler.END

    link = (update.message.text or "").strip()
    if not link.startswith(("http://", "https://")):
        await update.message.reply_text(
            "Пришли ссылку, начинающуюся с http:// или https://"
        )
        return WAITING_LINK

    if "text" not in ctx.user_data:
        await update.message.reply_text("Сначала пришли документ. /start для начала.")
        return ConversationHandler.END

    await update.message.reply_text("Анализирую… 30–60 секунд.")

    try:
        md = analyze(ctx.user_data["text"], link)
    except Exception as e:
        log.exception("analyze failed")
        await update.message.reply_text(f"Ошибка анализа: {e}")
        return ConversationHandler.END

    file_name = ctx.user_data["file_name"]
    base = safe_basename(file_name)

    try:
        await ctx.bot.send_document(
            chat_id=OUTPUT_CHAT_ID,
            document=ctx.user_data["file_id"],
            caption=f"📄 {file_name}\n🔗 {link}",
        )
        md_bytes = BytesIO(md.encode("utf-8"))
        md_bytes.name = f"{base}_analysis.md"
        await ctx.bot.send_document(
            chat_id=OUTPUT_CHAT_ID,
            document=md_bytes,
            filename=md_bytes.name,
        )
    except Exception as e:
        log.exception("forward failed")
        await update.message.reply_text(
            f"Анализ готов, но ошибка отправки в рабочий чат: {e}"
        )
        return ConversationHandler.END

    await update.message.reply_text("Готово. Результат отправлен.")
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("Отменено. /start чтобы начать заново.")
    return ConversationHandler.END


async def keywords_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    await update.message.reply_text(KEYWORDS_MESSAGE)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    await update.message.reply_text(HELP_TEXT)


async def unknown_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    await update.message.reply_text(f"Не знаю такую команду.\n\n{HELP_TEXT}")


async def fallback_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Не понял. Начни с /start и пришли документ техзадания."
    )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Document.ALL, receive_doc),
        ],
        states={
            WAITING_DOC: [MessageHandler(filters.Document.ALL, receive_doc)],
            WAITING_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("keywords", keywords_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    log.info("Bot starting, output chat = %s", OUTPUT_CHAT_ID)

    if WEBHOOK_URL:
        path = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
        secret = hashlib.sha256((BOT_TOKEN + "::secret").encode()).hexdigest()[:32]
        full_url = f"{WEBHOOK_URL}/{path}"
        log.info("Webhook mode: listening on :%s, telegram → %s", PORT, full_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=path,
            webhook_url=full_url,
            secret_token=secret,
        )
    else:
        log.info("Polling mode (no WEBHOOK_URL set)")
        app.run_polling()


if __name__ == "__main__":
    main()
