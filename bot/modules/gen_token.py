"""Generate token.pickle via /token command in bot.
OOB flow — no redirect URI needed, no localhost, no callback server."""
import os
from asyncio import Event, wait_for, TimeoutError as AsyncTimeout
from pickle import dump as pdump, load as pload

from pyrogram.enums import ChatType
from pyrogram.filters import create, user, text, private
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import send_message, delete_message

_STOP = "gentoken_stop"
_TIMEOUT = 300
SCOPES = ["https://www.googleapis.com/auth/drive"]
OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
CREDENTIALS_FILE = "/usr/src/app/credentials.json"
TOKEN_FILE = "/usr/src/app/token.pickle"

_pending = {}


def _stop_filter(uid):
    async def _check(_, __, update):
        return update.data == _STOP and update.from_user.id == uid
    return create(_check)


def _stop_btns():
    btns = ButtonMaker()
    btns.data_button("Cancel", data=_STOP)
    return btns.build_menu(1)


def _header(name):
    return f"⌬ <u><b>Google Drive Token Generator</b></u>\n│ <b>{name}</b>"


async def _wait_input(user_id, timeout=_TIMEOUT):
    event = Event()
    result = [None]

    async def _on_text(_, msg):
        await delete_message(msg)
        result[0] = ("text", msg.text or "")
        event.set()

    async def _on_stop(_, q):
        await q.answer()
        result[0] = ("stop", None)
        event.set()

    h1 = TgClient.bot.add_handler(
        MessageHandler(_on_text, filters=user(user_id) & text & private), group=-1)
    h2 = TgClient.bot.add_handler(
        CallbackQueryHandler(_on_stop, filters=_stop_filter(user_id)), group=-1)
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
    h = _header(user_name)
    btns = _stop_btns()

    if not os.path.exists(CREDENTIALS_FILE):
        return await send_message(
            message,
            f"{h}\n┃\n┖ <b>credentials.json not found!</b>\n"
            "Send the file as a document first.")

    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "rb") as f:
                creds = pload(f)
            if creds and creds.valid:
                return await send_message(message, f"{h}\n┃\n┖ <b>Already valid!</b>")
        except Exception:
            pass

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE, scopes=SCOPES,
        redirect_uri=OOB_REDIRECT_URI,
    )

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    _pending[user_id] = flow

    await send_message(
        message,
        f"{h}\n┃\n"
        f"┠  <b>Step 1:</b> Open link below in any browser\n"
        f"┠  <b>Step 2:</b> Login → Allow access\n"
        f"┠  <b>Step 3:</b> Copy the code shown on screen\n"
        f"┠  <b>Step 4:</b> Paste the code here\n"
        f"┖ <a href='{auth_url}'>🔐 Authorize Google Drive</a>",
        btns,
    )

    result = await _wait_input(user_id, _TIMEOUT)

    if result is None:
        _pending.pop(user_id, None)
        return await send_message(message, f"{h}\n┃\n┖ <b>Timed Out!</b>")

    if result[0] == "stop":
        _pending.pop(user_id, None)
        return await send_message(message, f"{h}\n┃\n┖ <b>Cancelled.</b>")

    code = result[1].strip()
    if not code:
        _pending.pop(user_id, None)
        return await send_message(message, f"{h}\n┃\n┖ <b>No code provided.</b>")

    stored_flow = _pending.pop(user_id, None)
    if stored_flow is None:
        return await send_message(message, f"{h}\n┃\n┖ <b>Session expired. Try /token again.</b>")

    try:
        stored_flow.fetch_token(code=code)
        creds = stored_flow.credentials
        with open(TOKEN_FILE, "wb") as f:
            pdump(creds, f)
        await send_message(
            message,
            f"{h}\n┃\n"
            "┠  <b>token.pickle generated!</b>\n"
            "┠  Set: <code>USE_SERVICE_ACCOUNTS=False</code>\n"
            "┖  <code>/restart</code> to apply",
        )
    except Exception as e:
        err = str(e)
        if "invalid_grant" in err or "bad verification code" in err.lower():
            hint = "\n┠  <i>Code expired or invalid. Try /token again.</i>"
        elif "Access blocked" in err or "redirect_uri" in err:
            hint = (
                "\n┠  <i>OOB flow may be blocked by Google for this project.</i>"
                "\n┠  <i>Alternative: generate token.pickle on your laptop,</i>"
                "\n┠  <i>then send it as a document to this bot.</i>"
            )
        else:
            hint = ""
        await send_message(
            message,
            f"{h}\n┃\n┖ <b>Error:</b> <i>{err}</i>{hint}",
        )
