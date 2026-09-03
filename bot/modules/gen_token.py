"""Generate token.pickle via /token command in bot.

Flow:
  1. /token → bot asks user to upload token.pickle
  2. User generates token.pickle externally (Termux / Colab / laptop)
  3. User sends token.pickle to bot
  4. Bot saves it → done

OOB flow (urn:ietf:wg:oauth:2.0:oob) is deprecated by Google since Oct 2022.
So bot cannot generate token directly — user must generate externally.
"""
import os
from asyncio import Event, wait_for, TimeoutError as AsyncTimeout
from pickle import load as pload

from pyrogram.enums import ChatType
from pyrogram.filters import create, user, text, private, document
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import send_message, delete_message

_STOP = "gentoken_stop"
_TIMEOUT = 300
TOKEN_FILE = "/usr/src/app/token.pickle"
CREDENTIALS_FILE = "/usr/src/app/credentials.json"

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


INSTRUCTIONS = """<b>How to generate token.pickle:</b>

<b>📱 Android (Termux):</b>
<code>pkg install python git</code>
<code>pip install google-auth google-auth-oauthlib</code>
<code>git clone https://github.com/AaryaKrishna19/Token-Pickle-Generator-Termux</code>
<code>cd Token-Pickle-Generator-Termux</code>
<code>pip install -r requirements.txt</code>
Put <code>credentials.json</code> in that folder, then:
<code>python generate_token.py</code>
Copy <code>token.pickle</code> and send here.

<b>💻 Laptop/PC:</b>
<code>pip install google-auth google-auth-oauthlib</code>
Create <code>gen.py</code> with this code:
<code>from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file("credentials.json", ["https://www.googleapis.com/auth/drive"])
creds = flow.run_local_server(port=8080)
import pickle
with open("token.pickle","wb") as f: pickle.dump(creds,f)
print("Done!")</code>
Run: <code>python gen.py</code>
Browser opens → Login → Allow → send <code>token.pickle</code> here.

<b>☁️ Google Colab:</b>
Upload <code>credentials.json</code> to Colab, run same code, download <code>token.pickle</code>."""


async def _wait_for_document(user_id, filename, timeout=_TIMEOUT):
    """Wait for user to send a specific file as a document."""
    event = Event()
    result = [None]

    async def _on_doc(_, msg):
        if msg.document and msg.document.file_name == filename:
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


@new_task
async def gen_gdrive_token(_, message):
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"
    h = _header(user_name)
    btns = _stop_btns()

    # Ask user to upload token.pickle
    await send_message(
        message,
        f"{h}\n┃\n"
        "┠  Send your <code>token.pickle</code> file\n"
        "┠  <i>Generate it using Termux / Colab / Laptop</i>\n"
        f"┖  <i>Timeout: {_TIMEOUT}s</i>\n\n"
        f"{INSTRUCTIONS}",
        btns,
    )

    doc_result = await _wait_for_document(user_id, "token.pickle", _TIMEOUT)

    if doc_result is None:
        return await send_message(message, f"{h}\n┃\n┖ <b>Timed Out!</b>")
    if doc_result == ("stop", None):
        return await send_message(message, f"{h}\n┃\n┖ <b>Cancelled.</b>")

    doc_message = doc_result

    # Download token.pickle
    try:
        await TgClient.bot.download_media(doc_message, file_name=TOKEN_FILE)
    except Exception as e:
        return await send_message(
            message, f"{h}\n┃\n┖ <b>Download failed:</b> <i>{e}</i>")

    if not os.path.exists(TOKEN_FILE):
        return await send_message(
            message, f"{h}\n┃\n┖ <b>token.pickle not saved. Try again.</b>")

    # Validate: try loading it
    try:
        with open(TOKEN_FILE, "rb") as f:
            creds = pload(f)
        if not creds or not hasattr(creds, "token"):
            os.remove(TOKEN_FILE)
            return await send_message(
                message,
                f"{h}\n┃\n┖ <b>Invalid token.pickle.</b>\n"
                "<i>File must be a valid Google OAuth credentials pickle.</i>")
    except Exception as e:
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass
        return await send_message(
            message,
            f"{h}\n┃\n┖ <b>Cannot read token.pickle:</b> <i>{e}</i>\n"
            "<i>Make sure it was generated with correct credentials.json.</i>")

    # Clean old credentials.json if exists
    if os.path.exists(CREDENTIALS_FILE):
        try:
            os.remove(CREDENTIALS_FILE)
        except OSError:
            pass

    await send_message(
        message,
        f"{h}\n┃\n"
        "┠  <b>token.pickle saved!</b>\n"
        "┠  Set: <code>USE_SERVICE_ACCOUNTS=False</code>\n"
        "┖  <code>/restart</code> to apply",
    )
