"""Generate token.pickle via /token command in bot.
OOB flow — no redirect URI needed, no localhost, no callback server.
Every /token call: auto-delete old credentials.json + token.pickle,
ask user to send fresh credentials.json, then generate token."""
import os
from asyncio import Event, wait_for, TimeoutError as AsyncTimeout
from pickle import dump as pdump, load as pload

from pyrogram.enums import ChatType
from pyrogram.filters import create, user, text, private, document
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


def _clean_credentials():
    for f in (CREDENTIALS_FILE,):
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


async def _wait_for_document(user_id, timeout=_TIMEOUT):
    """Wait for user to send credentials.json as a document."""
    event = Event()
    result = [None]

    async def _on_doc(_, msg):
        if msg.document and msg.document.file_name == "credentials.json":
            result[0] = msg
            event.set()

    async def _on_stop(_, q):
        await q.answer()
        result[0] = ("stop", None)
        event.set()

    h1 = TgClient.bot.add_handler(
        MessageHandler(_on_doc, filters=user(user_id) & document & private), group=-1)
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

    # Step 0: Delete old credentials.json + token.pickle
    _clean_credentials()

    # Step 1: Ask user to send credentials.json
    await send_message(
        message,
        f"{h}\n┃\n"
        "┠  <b>Step 1:</b> Send your <code>credentials.json</code> file\n"
        "┠  <i>(Google Cloud Console → OAuth 2.0 Client ID → Desktop type)</i>\n"
        f"┖  <i>Timeout: {_TIMEOUT}s</i>",
        btns,
    )

    doc_result = await _wait_for_document(user_id, _TIMEOUT)

    if doc_result is None:
        return await send_message(message, f"{h}\n┃\n┖ <b>Timed Out! No file received.</b>")
    if doc_result == ("stop", None):
        return await send_message(message, f"{h}\n┃\n┖ <b>Cancelled.</b>")

    doc_message = doc_result

    # Step 2: Download credentials.json
    try:
        await TgClient.bot.download_media(doc_message, file_name=CREDENTIALS_FILE)
    except Exception as e:
        return await send_message(
            message, f"{h}\n┃\n┖ <b>Download failed:</b> <i>{e}</i>")

    if not os.path.exists(CREDENTIALS_FILE):
        return await send_message(
            message, f"{h}\n┃\n┖ <b>credentials.json not saved. Try again.</b>")

    # Step 3: Build OOB auth URL
    from google_auth_oauthlib.flow import Flow

    try:
        flow = Flow.from_client_secrets_file(
            CREDENTIALS_FILE, scopes=SCOPES,
            redirect_uri=OOB_REDIRECT_URI,
        )
    except Exception as e:
        _clean_credentials()
        return await send_message(
            message, f"{h}\n┃\n┖ <b>Invalid credentials.json:</b> <i>{e}</i>")

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    _pending[user_id] = flow

    await send_message(
        message,
        f"{h}\n┃\n"
        f"┠  <b>Step 2:</b> Open link below in any browser\n"
        f"┠  <b>Step 3:</b> Login → Allow access\n"
        f"┠  <b>Step 4:</b> Copy the code shown on screen\n"
        f"┠  <b>Step 5:</b> Paste the code here\n"
        f"┖ <a href='{auth_url}'>🔐 Authorize Google Drive</a>",
        btns,
    )

    # Step 4: Wait for auth code
    result = await _wait_input(user_id, _TIMEOUT)

    if result is None:
        _pending.pop(user_id, None)
        _clean_credentials()
        return await send_message(message, f"{h}\n┃\n┖ <b>Timed Out!</b>")

    if result[0] == "stop":
        _pending.pop(user_id, None)
        _clean_credentials()
        return await send_message(message, f"{h}\n┃\n┖ <b>Cancelled.</b>")

    code = result[1].strip()
    if not code:
        _pending.pop(user_id, None)
        _clean_credentials()
        return await send_message(message, f"{h}\n┃\n┖ <b>No code provided.</b>")

    stored_flow = _pending.pop(user_id, None)
    if stored_flow is None:
        _clean_credentials()
        return await send_message(message, f"{h}\n┃\n┖ <b>Session expired. Try /token again.</b>")

    # Step 5: Exchange code for token
    try:
        stored_flow.fetch_token(code=code)
        creds = stored_flow.credentials
        with open(TOKEN_FILE, "wb") as f:
            pdump(creds, f)
        # Clean credentials.json after success
        try:
            os.remove(CREDENTIALS_FILE)
        except OSError:
            pass
        await send_message(
            message,
            f"{h}\n┃\n"
            "┠  <b>token.pickle generated!</b>\n"
            "┠  <b>credentials.json auto-deleted.</b>\n"
            "┠  Set: <code>USE_SERVICE_ACCOUNTS=False</code>\n"
            "┖  <code>/restart</code> to apply",
        )
    except Exception as e:
        _clean_credentials()
        err = str(e)
        if "invalid_grant" in err or "bad verification code" in err.lower():
            hint = "\n┠  <i>Code expired or invalid. Try /token again.</i>"
        elif "Access blocked" in err or "redirect_uri" in err:
            hint = (
                "\n┠  <i>OOB flow blocked by Google for this project.</i>"
                "\n┠  <i>Generate token.pickle on your laptop,</i>"
                "\n┠  <i>then send it as a document to this bot.</i>"
            )
        else:
            hint = ""
        await send_message(
            message,
            f"{h}\n┃\n┖ <b>Error:</b> <i>{err}</i>{hint}",
        )
