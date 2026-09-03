"""Generate token.pickle for Google Drive via /token command.

Flow:
1. /token → bot asks for credentials.json
2. User sends credentials.json file (or sends /token again if already saved)
3. Bot saves credentials.json → generates Google auth link
4. User clicks link → authorizes → pastes code
5. Bot saves token.pickle
"""
import os
from asyncio import Event, wait_for, TimeoutError as AsyncTimeout

from pyrogram.enums import ChatType, MessageMediaType
from pyrogram.filters import create, user, text, private
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from aiofiles.os import remove as aioremove

from ..core.tg_client import TgClient
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import get_readable_time
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    send_message,
    edit_message,
    delete_message,
)

_STOP = "gentoken_stop"
_TIMEOUT = 180
SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_FILE = "/usr/src/app/credentials.json"
TOKEN_FILE = "token.pickle"


def _stop_filter(uid):
    async def _check(_, __, update):
        return update.data == _STOP and update.from_user.id == uid
    return create(_check)


def _stop_btns():
    btns = ButtonMaker()
    btns.data_button("Cancel", data=_STOP)
    return btns.build_menu(1)


def _header(user_name):
    return (
        "⌬ <u><i><b>Google Drive Token Generator</b></i></u>\n│\n"
        f"│ <b>User</b> → <b>{user_name}</b>"
    )


async def _wait_for_file_or_text(user_id, timeout=_TIMEOUT):
    """Wait for user to send a file (credentials.json) or text (auth code)."""
    event = Event()
    result = [None]

    async def _on_media(_, message):
        await delete_message(message)
        if message.document and message.document.file_name and message.document.file_name.endswith(".json"):
            path = await TgClient.bot.download_media(message, file_name=CREDENTIALS_FILE)
            result[0] = ("FILE", path)
            event.set()

    async def _on_text(_, message):
        await delete_message(message)
        result[0] = ("TEXT", message.text or "")
        event.set()

    async def _on_stop(_, query):
        await query.answer()
        result[0] = ("STOP", None)
        event.set()

    all_media = create(
        lambda flt, _, m: bool(m.media and getattr(m.media, "value", None) != "text")
    )

    h1 = TgClient.bot.add_handler(
        MessageHandler(_on_text, filters=user(user_id) & text & private),
        group=-1,
    )
    h2 = TgClient.bot.add_handler(
        MessageHandler(
            _on_media,
            filters=user(user_id) & private & (__import__("pyrogram.filters", fromlist=["document"]).document),
        ),
        group=-1,
    )
    h3 = TgClient.bot.add_handler(
        CallbackQueryHandler(_on_stop, filters=_stop_filter(user_id)),
        group=-1,
    )
    try:
        await wait_for(event.wait(), timeout)
    except AsyncTimeout:
        result[0] = None
    finally:
        TgClient.bot.remove_handler(*h1)
        TgClient.bot.remove_handler(*h2)
        TgClient.bot.remove_handler(*h3)
    return result[0]


async def _stop_or_timeout(value, msg, h):
    if value is None:
        await edit_message(msg, f"{h}\n┃\n┖ <b>Timed Out!</b>\n┖ <i>Process Stopped.</i>")
        return True
    if value == ("STOP", None):
        await edit_message(msg, f"{h}\n┃\n┖ <b>Process Stopped.</b>")
        return True
    return False


@new_task
async def gen_gdrive_token(_, message):
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"
    btns = _stop_btns()
    h = _header(user_name)

    # First check: does credentials.json already exist?
    if not os.path.exists(CREDENTIALS_FILE):
        await send_message(
            message,
            f"{h}\n┃\n"
            "┖ <b>Send your <code>credentials.json</code> file now.</b>\n\n"
            "┃ How to get it:\n"
            "┃ 1. Go to <a href='https://console.cloud.google.com/apis/credentials'>Google Cloud Console</a>\n"
            "┃ 2. Create OAuth 2.0 Client ID → Desktop app\n"
            "┃ 3. Download JSON file\n"
            "┃\n"
            "┖ <i>Send the file (not a link).</i>",
            btns,
        )

        got = await _wait_for_file_or_text(user_id)
        if await _stop_or_timeout(got, await message.reply("⏳"), h):
            return

        kind, val = got
        if kind == "TEXT":
            # If user sent a code instead, try it directly
            code = val.strip()
            return await _finish_token(message, h, code, btns)

    # Now credentials.json exists — generate auth link
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        await send_message(message, f"{h}\n┃\n┖ <b>google-auth-oauthlib not installed.</b>")
        return

    # Check if token already valid
    if os.path.exists(TOKEN_FILE):
        from pickle import load as pload
        try:
            with open(TOKEN_FILE, "rb") as f:
                creds = pload(f)
            if creds and creds.valid:
                await send_message(
                    message,
                    f"{h}\n┃\n"
                    "┖ <b>token.pickle already exists and is valid!</b>\n"
                    "┃ Send <code>/token</code> again after removing existing token.pickle\nto regenerate.",
                )
                return
        except Exception:
            pass

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    await send_message(
        message,
        f"{h}\n┃\n"
        "┠ <b>Step 1:</b> Click the link below to authorize:\n"
        f"┖ <a href='{auth_url}'>🔐 Authorize Google Drive</a>\n\n"
        "┃ After authorizing, Google will show a code.\n"
        "┃ Send that code here.\n\n"
        f"<i>Timeout: {get_readable_time(_TIMEOUT)}</i>",
        btns,
    )

    code = await _invoke_text(user_id)
    if await _stop_or_timeout(code, await message.reply("⏳"), h):
        return

    return await _finish_token(message, h, code, btns)


async def _invoke_text(user_id, timeout=_TIMEOUT):
    event = Event()
    result = [None]

    async def _on_text(_, message):
        await delete_message(message)
        result[0] = message.text or ""
        event.set()

    async def _on_stop(_, query):
        await query.answer()
        result[0] = _STOP
        event.set()

    h1 = TgClient.bot.add_handler(
        MessageHandler(_on_text, filters=user(user_id) & text & private),
        group=-1,
    )
    h2 = TgClient.bot.add_handler(
        CallbackQueryHandler(_on_stop, filters=_stop_filter(user_id)),
        group=-1,
    )
    try:
        await wait_for(event.wait(), timeout)
    except AsyncTimeout:
        result[0] = None
    finally:
        TgClient.bot.remove_handler(*h1)
        TgClient.bot.remove_handler(*h2)
    return result[0]


async def _finish_token(message, h, code, btns):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        flow.fetch_token(code=code.strip())
        creds = flow.credentials
    except Exception as e:
        await send_message(message, f"{h}\n┃\n┖ <b>Token exchange failed:</b> <i>{e}</i>")
        return

    try:
        from pickle import dump as pdump
        with open(TOKEN_FILE, "wb") as f:
            pdump(creds, f)
    except Exception as e:
        await send_message(message, f"{h}\n┃\n┖ <b>Failed to save token.pickle:</b> <i>{e}</i>")
        return

    await send_message(
        message,
        f"{h}\n┃\n"
        "┠  <b>token.pickle generated successfully!</b>\n"
        "┃\n"
        "┠ Now set in bot config:\n"
        "┠  <code>USE_SERVICE_ACCOUNTS=False</code>\n"
        "┃\n"
        "┖ Then <code>/restart</code> the bot.",
    )
