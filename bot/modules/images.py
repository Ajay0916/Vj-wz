import os
import re

from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import handleIndex, new_task
from ..helper.ext_utils.db_handler import database
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    send_message,
    edit_message,
    delete_message,
)


def _extract_http_links(*texts):
    """All http(s) links found across the given texts (line/space separated)."""
    links = []
    for text in texts:
        if not text:
            continue
        links.extend(
            part.strip()
            for part in re.split(r"\s+", text)
            if part.strip().startswith("http")
        )
    return links


def _is_image_document(m):
    """True if message is an image sent as a file (document)."""
    return bool(
        m.document
        and m.document.mime_type
        and m.document.mime_type.startswith("image")
        and m.document.file_size <= 5242880 * 2
    )


async def _document_as_photo(client, m):
    """Re-upload an image document as a photo so the gallery (photo-only) can show it."""
    path = await m.download()
    if not path:
        return None
    try:
        sent = await client.send_photo("me", path)
        return sent.photo.file_id
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def _save_images(editable, items):
    """Append new images (dedup) and persist the gallery."""
    existing = set(Config.IMAGES)
    new = [item for item in items if item not in existing]
    if not new:
        await edit_message(editable, "<b>Images already exist in the gallery.</b>")
        return
    Config.IMAGES.extend(new)
    if Config.DATABASE_URL:
        await database.update_config({"IMAGES": Config.IMAGES})
    label = "Image Added" if len(new) == 1 else "Images Added"
    await edit_message(
        editable,
        f"⌬ <b><u>{label}</u></b>\n│\n┖ <b>Total Images :</b> <code>{len(Config.IMAGES)}</code>",
    )


@new_task
async def picture_add(client, message):
    resm = message.reply_to_message
    editable = await send_message(message, "<i>Fetching Input ...</i>")
    if "-b" in message.command:
        b_idx = message.command.index("-b")
        count = 0
        if len(message.command) > b_idx + 1 and message.command[b_idx + 1].isdigit():
            count = int(message.command[b_idx + 1])
        links = _extract_http_links(
            " ".join(message.command[1:]), resm.text if resm else ""
        )
        if count:
            links = links[:count]
        if links:
            return await _save_images(editable, links)
        if resm and (resm.photo or _is_image_document(resm)):
            try:
                group = await resm.get_media_group()
            except Exception:
                group = [resm]
            pics = []
            for m in group:
                if m.photo and m.photo.file_size <= 5242880 * 2:
                    pics.append(m.photo.file_id)
                elif _is_image_document(m):
                    fid = await _document_as_photo(client, m)
                    if fid:
                        pics.append(fid)
            if not pics:
                return await edit_message(
                    editable, "<i>Media is Not Supported! Only Photos!!</i>"
                )
            if count:
                pics = pics[:count]
            return await _save_images(editable, pics)
        return await edit_message(
            editable,
            "<b>Bulk (-b):</b> reply karo multiple links wale message ya album photo ko. "
            "<code>-b &lt;n&gt;</code> = sirf pehle N items.",
        )
    if len(message.command) > 1 or resm and resm.text:
        msg_text = resm.text if resm else message.command[1]
        if not msg_text.startswith("http"):
            return await edit_message(
                editable, "<b>Not a Valid Link, Must Start with 'http'</b>"
            )
        pic_add = msg_text.strip()
        return await _save_images(editable, [pic_add])
    elif resm and (resm.photo or _is_image_document(resm)):
        if resm.photo:
            if resm.photo.file_size > 5242880 * 2:
                return await edit_message(
                    editable, "<i>Media is Not Supported! Only Photos!!</i>"
                )
            pic_add = resm.photo.file_id
        else:
            pic_add = await _document_as_photo(client, resm)
            if not pic_add:
                return await edit_message(
                    editable, "<i>Media is Not Supported! Only Photos!!</i>"
                )
        return await _save_images(editable, [pic_add])
    else:
        help_msg = f"""⌬ <b><u>Add Image Usage</u></b>
│
┠ <b>Reply to Link:</b> <code>/{BotCommands.AddImageCommand} {{link}}</code>
┠ <b>Reply to Photo/File:</b> <code>/{BotCommands.AddImageCommand}</code>
┠ <b>Bulk (-b):</b> <code>/{BotCommands.AddImageCommand} -b</code> reply to multi-link message / album
┠ <b>Bulk count:</b> <code>/{BotCommands.AddImageCommand} -b 10</code> sirf pehle 10
┖ <b>Supported:</b> <i>Telegra.ph, DDL links, Telegram photos/files</i>"""
        return await edit_message(editable, help_msg)


@new_task
async def pictures(_, message):
    if not Config.IMAGES:
        await send_message(
            message,
            f"<b>No Photo to Show !</b> Add by <code>/{BotCommands.AddImageCommand}</code>",
        )
    else:
        to_edit = await send_message(
            message, "<i>Generating Grid of your Images...</i>"
        )
        buttons = ButtonMaker()
        user_id = message.from_user.id
        buttons.data_button("\u00ab", f"images {user_id} turn -1")
        buttons.data_button("\u00bb", f"images {user_id} turn 1")
        buttons.data_button("Remove Image", f"images {user_id} remov 0")
        buttons.data_button("Close", f"images {user_id} close")
        buttons.data_button("Remove All", f"images {user_id} removall", "footer")
        await delete_message(to_edit)
        total = len(Config.IMAGES)
        await send_message(
            message,
            f"⌬ <b><u>Image Gallery</u></b>\n│\n┖ \U0001f304 <b>No. : 1 / {total}</b>",
            buttons.build_menu(2),
            photo=Config.IMAGES[0],
        )


@new_task
async def pics_callback(_, query):
    message = query.message
    user_id = query.from_user.id
    data = query.data.split()
    if user_id != int(data[1]):
        await query.answer(text="Not Authorized User!", show_alert=True)
        return
    if data[2] == "turn":
        await query.answer()
        if not Config.IMAGES:
            await delete_message(message)
            await send_message(
                message,
                f"<b>No Photo to Show !</b> Add by <code>/{BotCommands.AddImageCommand}</code>",
            )
            return
        ind = handleIndex(int(data[3]), Config.IMAGES)
        total = len(Config.IMAGES)
        no = ind + 1
        pic_info = f"⌬ <b><u>Image Gallery</u></b>\n│\n┖ \U0001f304 <b>No. : {no} / {total}</b>"
        buttons = ButtonMaker()
        buttons.data_button("\u00ab", f"images {data[1]} turn {ind - 1}")
        buttons.data_button("\u00bb", f"images {data[1]} turn {ind + 1}")
        buttons.data_button("Remove Image", f"images {data[1]} remov {ind}")
        buttons.data_button("Close", f"images {data[1]} close")
        buttons.data_button("Remove All", f"images {data[1]} removall", "footer")
        if message.media:
            await edit_message(
                message, pic_info, buttons.build_menu(2), photo=Config.IMAGES[ind]
            )
        else:
            await delete_message(message)
            await send_message(
                message,
                pic_info,
                buttons.build_menu(2),
                photo=Config.IMAGES[ind],
            )
    elif data[2] == "remov":
        Config.IMAGES.pop(int(data[3]))
        if Config.DATABASE_URL:
            await database.update_config({"IMAGES": Config.IMAGES})
        await query.answer("Image Successfully Deleted", show_alert=True)
        if len(Config.IMAGES) == 0:
            await delete_message(message)
            await send_message(
                message,
                f"<b>No Photo to Show !</b> Add by <code>/{BotCommands.AddImageCommand}</code>",
            )
            return
        ind = int(data[3])
        ind = min(ind, len(Config.IMAGES) - 1)
        total = len(Config.IMAGES)
        no = ind + 1
        pic_info = f"⌬ <b><u>Image Gallery</u></b>\n│\n┖ \U0001f304 <b>No. : {no} / {total}</b>"
        buttons = ButtonMaker()
        buttons.data_button("\u00ab", f"images {data[1]} turn {ind - 1}")
        buttons.data_button("\u00bb", f"images {data[1]} turn {ind + 1}")
        buttons.data_button("Remove Image", f"images {data[1]} remov {ind}")
        buttons.data_button("Close", f"images {data[1]} close")
        buttons.data_button("Remove All", f"images {data[1]} removall", "footer")
        if message.media:
            await edit_message(
                message, pic_info, buttons.build_menu(2), photo=Config.IMAGES[ind]
            )
        else:
            await delete_message(message)
            await send_message(
                message,
                pic_info,
                buttons.build_menu(2),
                photo=Config.IMAGES[ind],
            )
    elif data[2] == "removall":
        Config.IMAGES.clear()
        if Config.DATABASE_URL:
            await database.update_config({"IMAGES": Config.IMAGES})
        await query.answer("All Images Successfully Deleted", show_alert=True)
        await delete_message(message)
        await send_message(
            message,
            f"<b>No Images to Show !</b> Add by <code>/{BotCommands.AddImageCommand}</code>",
        )
    else:
        await query.answer()
        await delete_message(message)
        if message.reply_to_message:
            await delete_message(message.reply_to_message)
