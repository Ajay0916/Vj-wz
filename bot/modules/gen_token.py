"""Generate token.pickle — exact same logic as CodeRed-001/TokenPickle."""
import os
from asyncio import Event, wait_for, TimeoutError as AsyncTimeout
from pickle import dump as pdump, load as pload

from pyrogram.enums import ChatType
from pyrogram.filters import create, user, text, private
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import get_readable_time
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    send_message,
    edit_message,
    delete_message,
)

_STOP = "gentoken_stop"
_TIMEOUT = 300
SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_FILE = "/usr/src/app/credentials.json"
TOKEN_FILE = "/usr/src/app/token.pickle"


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


@new_task
async def gen_gdrive_token(_, message):
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"
    btns = _stop_btns()
    h = _header(user_name)

    if not os.path.exists(CREDENTIALS_FILE):
        await send_message(
            message,
            f"{h}\n┃\n"
            "┖ <b>credentials.json not found!</b>\n"
            "Send your <code>credentials.json</code> file here first.",
        )
        return

    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "rb") as f:
                creds = pload(f)
            if creds and creds.valid:
                await send_message(
                    message,
                    f"{h}\n┃\n"
                    "┖ <b>token.pickle already valid!</b>\n"
                    "Remove it first to regenerate.",
                )
                return
        except Exception:
            pass

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        await send_message(message, f"{h}\n┃\n┖ <b>google-auth-oauthlib not installed.</b>")
        return

    # Same logic as CodeRed-001/TokenPickle
    try:
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    except Exception as e:
        await send_message(message, f"{h}\n┃\n┖ <b>Error:</b> <i>{e}</i>")
        return

    await send_message(
        message,
        f"{h}\n┃\n"
        f"┖ <a href='{auth_url}'>🔐 Click here to Authorize</a>\n\n"
        "After Allow, page will redirect with error (normal).\n"
        "Copy the <b>code</b> from URL bar:\n"
        "<code>...?code=<b>4/0Axx...</b>&amp;scope=...</code>\n\n"
        "Paste the code here.",
        btns,
    )

    code = await _invoke_text(user_id)
    if code is None:
        return await send_message(message, f"{h}\n┃\n┖ <b>Timed Out!</b>")
    if code == _STOP:
        return await send_message(message, f"{h}\n┃\n┖ <b>Cancelled.</b>")

    code = code.strip()

    # Extract from URL if full URL pasted
    if "code=" in code:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(code if code.startswith("http") else f"http://x?{code}")
        extracted = parse_qs(parsed.query).get("code", [None])[0]
        if extracted:
            code = extracted

    try:
        flow2 = InstalledAppFlow.from_client_secrets_file(
            CREDENTIALS_FILE, SCOPES,
            redirect_uri="http://localhost",
        )
        flow2.fetch_token(code=code)
        creds = flow2.credentials
    except Exception as e:
        return await send_message(
            message,
            f"{h}\n┃\n┖ <b>Token exchange failed:</b> <i>{e}</i>",
        )

    try:
        with open(TOKEN_FILE, "wb") as f:
            pdump(creds, f)
    except Exception as e:
        return await send_message(message, f"{h}\n┃\n┖ <b>Save failed:</b> <i>{e}</i>")

    await send_message(
        message,
        f"{h}\n┃\n"
        "┠  <b>token.pickle generated!</b>\n"
        "┠ Set config: <code>USE_SERVICE_ACCOUNTS=False</code>\n"
        "┖ <code>/restart</code>",
    )
