"""Accept token.pickle upload via /token command."""
import os
from pickle import load as pload

from pyrogram.enums import ChatType
from pyrogram.filters import user, private, document
from pyrogram.handlers import MessageHandler

from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.message_utils import send_message

TOKEN_FILE = "/usr/src/app/token.pickle"


@new_task
async def gen_gdrive_token(_, message):
    if message.chat.type != ChatType.PRIVATE:
        return

    if message.document and message.document.file_name == "token.pickle":
        try:
            await TgClient.bot.download_media(message, file_name=TOKEN_FILE)
            with open(TOKEN_FILE, "rb") as f:
                creds = pload(f)
            if not creds or not hasattr(creds, "token"):
                os.remove(TOKEN_FILE)
                return await send_message(message, "Invalid token.pickle.")
            await send_message(
                message,
                "<b>token.pickle saved!</b>\n"
                "Set: <code>USE_SERVICE_ACCOUNTS=False</code>\n"
                "<code>/restart</code> to apply",
            )
        except Exception as e:
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            await send_message(message, f"Error: <i>{e}</i>")
    else:
        await send_message(message, "Send your <code>token.pickle</code> file.")
