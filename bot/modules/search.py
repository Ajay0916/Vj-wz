import re
from niquests import AsyncSession
from html import escape
from secrets import token_hex
from urllib.parse import quote
from pyrogram.enums import ButtonStyle

from .. import LOGGER, bot_loop
from ..core.config_manager import Config
from ..core.torrent_manager import TorrentManager
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import get_readable_file_size
from ..helper.ext_utils.telegraph_helper import telegraph
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import edit_message, send_message

_LOCKER_HOSTS = (
    "nitroflare.com", "uploadgig.com", "rapidgator.net", "keep2share.cc",
    "k2s.cc", "filecrypt.cc", "katfile.com", "turbobit.net", "hitfile.net",
    "alfafile.net", "uploadrar.com", "userscloud.com", "file-upload.com",
    "fboom.me", "douploads.net", "hxfile.co", "dropapk.to", "uploadboy.com",
    "upload.ee", "ddownload.com", "wdupload.com", "dailyuploads.net",
    "4funbox.co", "mega.nz",
)


def _dl_link(url, name="", ext="", short=""):
    """Build a download link for a result. Google Drive URLs are linked
    directly so WZML-X can resolve the Drive ID natively (its extractor
    fails on proxied drive URLs); file-locker pages (nitroflare/uploadgig/
    rapidgator/...) also link directly - proxying them always 502s; results
    with a short token get a tiny /torrent_file/<token> link; everything
    else goes through the API proxy with a filename slug so browsers that
    ignore Content-Disposition still save the file with a real name."""
    if "drive.usercontent.google.com" in url or "drive.google.com" in url:
        return url
    if any(h in url for h in _LOCKER_HOSTS):
        return url
    if short:
        return "{}/api/v1/torrent_file/{}".format(Config.SEARCH_API_LINK, short)
    slug = re.sub(r"[^a-zA-Z0-9._-]", "_", str(name or "download"))[:80] or "download"
    slug = slug.strip("._-")
    if not re.search(r"\.[a-z0-9]{2,5}$", slug, re.I):
        if re.fullmatch(r"[a-z0-9]{2,8}", str(ext), re.I):
            slug = slug.rstrip("._-") + "." + str(ext).lower()
        else:
            m = re.search(
                r"(?<![a-z0-9])(pdf|epub|mobi|azw3|djvu|fb2|zip|rar|mp3|m4b|torrent)(?![a-z0-9])",
                slug + " " + str(url),
                re.I,
            )
            slug = slug.rstrip("._-") + ("." + m.group(1).lower() if m else ".dl")
    return "{}/api/v1/torrent_file/{}?url={}&name={}".format(
        Config.SEARCH_API_LINK, quote(slug), quote(url), quote(str(name or ""))
    )


def _magnet_share_link(magnet, short=""):
    """'Share Magnet to Telegram' link; uses the API short token when the
    result has one so the shared link isn't a 1KB+ magnet string."""
    if short:
        target = "{}/api/v1/magnet/{}".format(Config.SEARCH_API_LINK, short)
    else:
        target = magnet
    return "http://t.me/share/url?url={}".format(quote(target))


def _share_link(url):
    """Telegram share URL for a direct/alt download link."""
    return "http://t.me/share/url?url={}".format(quote(url))


def _api_headers():
    """Headers for every search-API call; sends the PIN automatically
    when it is set in settings."""
    headers = {}
    if Config.API_PIN:
        headers["X-API-Pin"] = Config.API_PIN
    return headers
PLUGINS = []
SITES = None
SITE_STATUS = {}
TELEGRAPH_LIMIT = 9999999
# rentry.co hard cap is 200K chars; chunk below it.
RENTRY_CHUNK = 180000


async def _refresh_sites():
    """Fetch the enabled site list from the API so disabled sites disappear
    from the buttons without a bot restart."""
    global SITES, SITE_STATUS
    try:
        async with AsyncSession() as client:
            response = await client.get(
                f"{Config.SEARCH_API_LINK}/api/v1/sites", headers=_api_headers()
            )
            data = response.json()
        sites = data.get("sites")
        if isinstance(sites, list):
            SITES = {
                str(item["site"]): str(item["name"])
                for item in sites
                if item.get("site") and item.get("name")
            }
        else:
            SITES = {
                str(site): str(site).capitalize()
                for site in data["supported_sites"]
        }
        SITE_STATUS = {}
        try:
            async with AsyncSession() as client:
                response = await client.get(
                    f"{Config.SEARCH_API_LINK}/api/v1/sites/status", headers=_api_headers()
                )
                status_data = response.json()
            for item in status_data.get("sites", []):
                site = item.get("site")
                if site:
                    SITE_STATUS[str(site)] = item
        except Exception as e:
            LOGGER.warning(f"{e} Can't refresh site status from SEARCH_API_LINK")
        SITES["all"] = "All"
        return True
    except Exception as e:
        LOGGER.error(f"{e} Can't refresh sites from SEARCH_API_LINK")
        return False


async def initiate_search_tools():
    if Config.DISABLE_TORRENTS or Config.DISABLE_SEARCH:
        LOGGER.warning("Torrents are disabled. Skipping search plugin initialization.")
        return
    qb_plugins = await TorrentManager.qbittorrent.search.plugins()
    if qb_plugins:
        names = [plugin.name for plugin in qb_plugins]
        await TorrentManager.qbittorrent.search.uninstall_plugin(names)
        PLUGINS.clear()
    if Config.SEARCH_PLUGINS:
        await TorrentManager.qbittorrent.search.install_plugin(Config.SEARCH_PLUGINS)

    if Config.SEARCH_API_LINK:
        await _refresh_sites()
        if SITES is None:
            LOGGER.error(
                "Can't fetch sites from SEARCH_API_LINK, make sure it uses the latest API version"
            )


def _site_status(site):
    return SITE_STATUS.get(site) or {}


def _site_display_name(site):
    if site in GROUP_NAMES:
        return GROUP_NAMES[site]
    name = SITES.get(site, str(site).capitalize()) if SITES else str(site).capitalize()
    status = _site_status(site)
    if status.get("manual_blocked"):
        return f"⛔ {name}"
    if status.get("blocked"):
        return f"⚠️ {name}"
    return name


def _group_sites_param(group):
    """Comma-separated enabled site ids for a group button, or "" for All."""
    if group == "all" or not SITES:
        return ""
    members = GROUP_SITES.get(group)
    if not members:
        return ""
    return ",".join(s for s in SITES if s in members and s != "all")


# Site button ordering: page 1 = all + important general + course sites,
# page 2 = anime/movie/book sites. Any site not listed here stays on page 1.
PAGE2_SITES = {
    "nyaasi",
    "yts",
}

# Sites excluded from -a (all-sites) search: audiobookbay has its own
# dedicated button, so it shouldn't flood every "search all" result.
ALL_SITES_EXCLUDE = {
    "audiobookbay",
}


def _site_sort_key(item):
    site, name = item
    if site == "all":
        return (0, 0, name.lower())
    page = 2 if site in PAGE2_SITES else 1
    status = _site_status(site)
    if status.get("manual_blocked"):
        rank = 3
    elif status.get("blocked"):
        rank = 2
    else:
        rank = 1
    return (page, rank, name.lower())


async def search(
    key, site, message, method, category="all", quality="", language="", format_="",
    size="all",
):
    opts = {}
    if method.startswith("api"):
        opts = _search_opts(message)
        limit = _search_limit(message)
        if method == "apisearch":
            extra = f" opts={opts}" if opts else ""
            LOGGER.info(f"API Searching: {key} from {site} (limit={limit}){extra}")
            if site in GROUP_NAMES or "," in site:
                api = f"{Config.SEARCH_API_LINK}/api/v1/all/search?query={quote(key)}&limit={limit}"
                group_sites = _group_sites_param(site)
                if not group_sites and "," in site:
                    group_sites = site
                if not group_sites and opts.get("all_sites") and SITES:
                    group_sites = ",".join(
                        s for s in SITES if s != "all" and s not in ALL_SITES_EXCLUDE
                    )
                hide = opts.get("hide_sites")
                if hide:
                    hidden = {
                        s.strip().lower()
                        for s in str(hide).split(",")
                        if s.strip()
                    }
                    if not group_sites and SITES:
                        # All-button / default combo: honour -hs by searching
                        # every enabled site except the hidden ones.
                        group_sites = ",".join(s for s in SITES if s != "all")
                    group_sites = ",".join(
                        s
                        for s in group_sites.split(",")
                        if s.strip().lower() not in hidden
                    )
                if group_sites:
                    api += f"&sites={quote(group_sites)}"
                elif hide:
                    await edit_message(
                        message,
                        "Saari sites hide ho gayi hain — <code>-hs</code> list check karo.",
                    )
                    return
            else:
                api = f"{Config.SEARCH_API_LINK}/api/v1/search?site={quote(site)}&query={quote(key)}&limit={limit}"
            if category and category != "all":
                api += f"&category={quote(category)}"
            if quality and quality != "all":
                api += f"&quality={quote(quality)}"
            if language and language != "all":
                api += f"&language={quote(language)}"
            if format_ and format_ != "all":
                api += f"&format={quote(format_)}"
            if size == "small":
                api += "&max_size=1GB"
            elif size == "medium":
                api += "&min_size=1GB&max_size=3GB"
            elif size == "large":
                api += "&min_size=3GB"
        elif method == "apitrend":
            LOGGER.info(f"API Trending from {site}")
            if site in GROUP_NAMES:
                api = f"{Config.SEARCH_API_LINK}/api/v1/all/trending?limit={limit}"
                group_sites = _group_sites_param(site)
                if group_sites:
                    api += f"&sites={quote(group_sites)}"
            else:
                api = f"{Config.SEARCH_API_LINK}/api/v1/trending?site={site}&limit={limit}"
        elif method == "apirecent":
            LOGGER.info(f"API Recent from {site}")
            if site in GROUP_NAMES:
                api = f"{Config.SEARCH_API_LINK}/api/v1/all/recent?limit={limit}"
                group_sites = _group_sites_param(site)
                if group_sites:
                    api += f"&sites={quote(group_sites)}"
            else:
                api = f"{Config.SEARCH_API_LINK}/api/v1/recent?site={site}&limit={limit}"
        api += _api_extra_params(opts, method, is_all=bool(opts.get("all_sites") and method == "apisearch"))
        try:
            page_spec = str(opts.get("page") or "").strip()
            queries = [q.strip() for q in str(key).split(",") if q.strip()][:3]
            multi_query = method == "apisearch" and len(queries) > 1
            if opts.get("timeout"):
                req_timeout = int(opts["timeout"]) + 60
            elif method == "apisearch" and opts.get("all_sites"):
                req_timeout = 900
            else:
                req_timeout = 60
            if page_spec and page_spec not in ("1",) and (page_spec == "0" or "-" in page_spec):
                req_timeout = max(req_timeout, 300)
            async with AsyncSession(timeout=req_timeout) as client:
                if method == "apisearch" and ((page_spec and page_spec != "1") or multi_query):
                    # -p spec (N | 0 | A-B) is passed to the API, which
                    # paginates/dedups server-side; comma key => each query.
                    # Merged as-is; dedup (by hash/name) only with -du.
                    merged = []
                    search_results = {}
                    dedup = bool(opts.get("dedup"))
                    for q in queries:
                        qapi = api.replace(quote(key), quote(q)) if multi_query else api
                        if page_spec and page_spec != "1":
                            qapi += f"&page={quote(page_spec)}"
                        resp = await client.get(qapi, headers=_api_headers())
                        data = resp.json()
                        if not isinstance(data, dict):
                            continue
                        search_results = data
                        if data.get("error") or data.get("detail"):
                            continue
                        merged.extend(data.get("data") or [])
                    if dedup:
                        merged = _dedup_rows(merged)
                    search_results = dict(search_results)
                    search_results["data"] = merged
                    search_results["total"] = len(merged)
                else:
                    if method != "apisearch" and page_spec.isdigit() and int(page_spec) > 1:
                        api += f"&page={page_spec}"
                    response = await client.get(api, headers=_api_headers())
                    search_results = response.json()
                    if (
                        method == "apisearch"
                        and opts.get("dedup")
                        and isinstance(search_results, dict)
                    ):
                        # -du on a normal single-page search too: same
                        # release from multiple sites shows only once.
                        search_results = dict(search_results)
                        search_results["data"] = _dedup_rows(search_results.get("data") or [])
                        search_results["total"] = len(search_results["data"])
            if isinstance(search_results, dict):
                api_error = search_results.get("error") or search_results.get("detail")
                if api_error:
                    if "pin" in str(api_error).lower():
                        await edit_message(
                            message,
                            "🔑 Please give API pin for result.\n"
                            "/settings → <code>API_PIN</code> me sahi PIN set karo. "
                            "Current: <code>{}</code>".format(
                                escape(str(Config.API_PIN or "None"))
                            ),
                        )
                    else:
                        await edit_message(
                            message,
                            f"{escape(str(api_error))}\nTorrent Site:- <i>{_site_display_name(site)}</i>",
                        )
                    return
            if search_results["total"] == 0:
                await edit_message(
                    message,
                    f"No result found for <i>{key or 'results'}</i>\nTorrent Site:- <i>{_site_display_name(site)}</i>",
                )
                return
            relaxed_filters = search_results.get("relaxed_filters")
            search_results = search_results["data"]
            if method == "apisearch":
                search_results = _apply_client_filters(search_results, opts, key)
            if not search_results:
                await edit_message(
                    message,
                    f"No result found for <i>{key or 'results'}</i> with the applied filters\nTorrent Site:- <i>{_site_display_name(site)}</i>",
                )
                return
            msg = f"<b>Found {min(len(search_results), TELEGRAPH_LIMIT)}</b>"
            if method == "apitrend":
                msg += f" <b>trending result(s)\nTorrent Site:- <i>{_site_display_name(site)}</i></b>"
            elif method == "apirecent":
                msg += (
                    f" <b>recent result(s)\nTorrent Site:- <i>{_site_display_name(site)}</i></b>"
                )
            else:
                msg += f" <b>result(s) for <i>{key}</i>\nTorrent Site:- <i>{_site_display_name(site)}</i></b>"
            if relaxed_filters:
                msg += " <i>(filters relaxed)</i>"
        except Exception as e:
            await edit_message(message, str(e))
            return
    else:
        LOGGER.info(f"PLUGINS Searching: {key} from {site}")
        search = await TorrentManager.qbittorrent.search.start(
            pattern=key, plugins=[site], category="all"
        )
        search_id = search.id
        while True:
            result_status = await TorrentManager.qbittorrent.search.status(search_id)
            status = result_status[0].status
            if status != "Running":
                break
        dict_search_results = await TorrentManager.qbittorrent.search.results(
            id=search_id, limit=TELEGRAPH_LIMIT
        )
        search_results = dict_search_results.results
        total_results = dict_search_results.total
        if total_results == 0:
            await edit_message(
                message,
                f"No result found for <i>{key}</i>\nTorrent Site:- <i>{site.capitalize()}</i>",
            )
            return
        msg = f"<b>Found {min(total_results, TELEGRAPH_LIMIT)}</b>"
        msg += f" <b>result(s) for <i>{key}</i>\nTorrent Site:- <i>{site.capitalize()}</i></b>"
        await TorrentManager.qbittorrent.search.delete(search_id)
    link = await get_result(search_results, key, message, method)
    buttons = ButtonMaker()
    links = link.split("\n")
    if len(links) == 1:
        buttons.url_button("🔎 VIEW", links[0], style=ButtonStyle.PRIMARY)
    else:
        for i, l in enumerate(links, 1):
            buttons.url_button(f"VIEW {i}", l)
    if (Config.SEARCH_RESULT_HOST or "telegraph") == "rentry":
        # Telegram confirms external-domain buttons (Android); the message
        # text link opens directly, so show it too. Telegraph pages are
        # Telegram-owned and open straight from the button - no text needed.
        msg += "\n\n" + "\n".join(links)
    button = buttons.build_menu(1)
    await edit_message(message, msg, button)
    if method.startswith("api") and search_results and opts.get("auto_leech"):
        links = _leech_links(search_results)
        if not links:
            await edit_message(
                message,
                "Kisi result me leech karne layak link (magnet/torrent) nahi mila.",
            )
            return
        await _start_leech(message, links, opts)


def _leech_links(results):
    """Link to auto-leech: first (top) result with a magnet/torrent."""
    for r in results:
        link = (
            r.get("magnet")
            or r.get("torrent")
            or r.get("download")
            or r.get("url")
        )
        if link:
            return [link]
    return []


async def _start_leech(message, links, opts=None):
    """Kick off leech for the -auto result.

    Reuses the bot's own leech command path (Mirror) so qbittorrent/aria2
    selection, options and status tracking behave exactly like /leech."""
    from .mirror_leech import Mirror
    from ..helper.telegram_helper.bot_commands import BotCommands

    if Config.DISABLE_LEECH:
        await edit_message(message, "Leech command is disabled.")
        return
    cmd = "/{}".format(BotCommands.LeechCommand[0])
    args = (opts or {}).get("leech_args") or []
    txt = "{} {} {}".format(cmd, links[0], " ".join(args)).strip()
    nextmsg = await send_message(message, txt)
    if not nextmsg or not getattr(nextmsg, "id", None):
        return
    try:
        nextmsg = await message._client.get_messages(
            chat_id=message.chat.id, message_ids=nextmsg.id
        )
    except Exception:
        return
    if message.from_user:
        nextmsg.from_user = message.from_user
    else:
        nextmsg.sender_chat = message.sender_chat
    bot_loop.create_task(Mirror(message._client, nextmsg, is_leech=True).new_event())


async def get_result(search_results, key, message, method):
    telegraph_content = []
    if method == "apirecent":
        msg = "<h4>API Recent Results</h4>"
    elif method == "apisearch":
        msg = f"<h4>API Search Result(s) For {key}</h4>"
    elif method == "apitrend":
        msg = "<h4>API Trending Results</h4>"
    else:
        msg = f"<h4>PLUGINS Search Result(s) For {key}</h4>"
    for index, result in enumerate(search_results, start=1):
        if method.startswith("api"):
            try:
                if result.get("name"):
                    msg += f"<code><a href='{result.get('url') or '#'}'>{escape(result['name'])}</a></code><br>"
                if "torrents" in result.keys():
                    for subres in result["torrents"]:
                        msg += f"<b>Quality: </b>{subres['quality']} | <b>Type: </b>{subres['type']} | "
                        msg += f"<b>Size: </b>{subres['size']}<br>"
                        if subres.get("torrent"):
                            _dl = _dl_link(subres["torrent"], result.get("name") or "", subres.get("extension") or "")
                            msg += "<a href='{}'>Direct Link</a>".format(_dl)
                            msg += " | <a href='{}'>Share</a>".format(_share_link(_dl))
                        if subres.get("torrent") and subres.get("magnet"):
                            msg += " | "
                        if subres.get("magnet"):
                            msg += "<b>Share Magnet to</b> "
                            msg += "<a href='{}'>Telegram</a>".format(
                                _magnet_share_link(subres["magnet"], subres.get("magnet_short") or "")
                            )
                        msg += "<br>"
                    msg += "<br>"
                else:
                    if result.get("size"):
                        msg += f"<b>Size: </b>{result['size']}<br>"
                    try:
                        msg += f"<b>Seeders: </b>{result['seeders']} | <b>Leechers: </b>{result['leechers']}<br>"
                    except Exception:
                        pass
                    tags = []
                    if result.get("site"):
                        tags.append(f"Site: {escape(str(result['site']))}")
                    if result.get("category"):
                        tags.append(f"Category: {escape(str(result['category']))}")
                    if result.get("quality"):
                        tags.append(f"Quality: {escape(str(result['quality']))}")
                    if result.get("language"):
                        tags.append(f"Language: {escape(str(result['language']))}")
                    if result.get("format"):
                        tags.append(f"Format: {escape(str(result['format']))}")
                    authors = result.get("authors") or (
                        [result["author"]] if result.get("author") else None
                    )
                    if authors:
                        tags.append(
                            "Author: " + escape(", ".join(str(a) for a in authors))
                        )
                    if result.get("date"):
                        tags.append(f"Date: {escape(str(result['date']))}")
                    if result.get("uploader"):
                        tags.append(f"Uploader: {escape(str(result['uploader']))}")
                    if tags:
                        msg += "<b>" + " | ".join(tags) + "</b><br>"
                    files = result.get("files")
                    if isinstance(files, list) and files:
                        msg += "<b>Files:</b><br>"
                        for f in files[:3]:
                            msg += f"• {escape(str(f))[:90]}<br>"
                        if len(files) > 3:
                            msg += f"<i>+{len(files) - 3} more</i><br>"
                    _links = []
                    if result.get("torrent"):
                        _dl = _dl_link(result["torrent"], result.get("name") or "", result.get("extension") or "", result.get("short") or "")
                        _links.append("<a href='{}'>Direct Link</a>".format(_dl))
                        _links.append("<a href='{}'>Share</a>".format(_share_link(_dl)))
                    if result.get("download"):
                        _alt = _dl_link(result["download"], result.get("name") or "", result.get("extension") or "", result.get("download_short") or "")
                        _links.append("<a href='{}'>Alt Link</a>".format(_alt))
                        _links.append("<a href='{}'>Share</a>".format(_share_link(_alt)))
                    if _links:
                        msg += " | ".join(_links)
                    if result.get("magnet"):
                        msg += (" | " if _links else "") + "<b>Share Magnet to</b> "
                        msg += "<a href='{}'>Telegram</a>".format(
                            _magnet_share_link(result["magnet"], result.get("magnet_short") or "")
                        )
                    if result.get("torrent") or result.get("magnet"):
                        msg += "<br><br>"
                    else:
                        msg += "<br>"
            except Exception:
                continue
        else:
            msg += f"<a href='{result.descrLink}'>{escape(result.fileName)}</a><br>"
            msg += f"<b>Size: </b>{get_readable_file_size(result.fileSize)}<br>"
            msg += f"<b>Seeders: </b>{result.nbSeeders} | <b>Leechers: </b>{result.nbLeechers}<br>"
            link = result.fileUrl
            if link.startswith("magnet:"):
                msg += f"<b>Share Magnet to</b> <a href='http://t.me/share/url?url={quote(link)}'>Telegram</a><br><br>"
            else:
                msg += f"<a href='{link}'>Direct Link</a><br><br>"

        if len(msg.encode("utf-8")) > 39000:
            telegraph_content.append(msg)
            msg = ""

        if index == TELEGRAPH_LIMIT:
            break

    if msg != "":
        telegraph_content.append(msg)

    if (Config.SEARCH_RESULT_HOST or "telegraph") == "rentry":
        try:
            return await _publish_rentry(search_results, key, method, message)
        except Exception as e:
            LOGGER.warning(f"rentry publish failed, falling back to telegraph: {e}")

    await edit_message(
        message, f"<b>Creating</b> {len(telegraph_content)} <b>Telegraph pages.</b>"
    )
    path = [
        (
            await telegraph.create_page(
                title="Mirror-leech-bot Torrent Search", content=content
            )
        )["path"]
        for content in telegraph_content
    ]
    if len(path) > 1:
        await edit_message(
            message, f"<b>Editing</b> {len(telegraph_content)} <b>Telegraph pages.</b>"
        )
        await telegraph.edit_telegraph(path, telegraph_content)
    return f"https://telegra.ph/{path[0]}"


def _rentry_blocks(search_results, key, method):
    """Search results as markdown blocks (header + one per result) so large
    sets can be chunked across multiple rentry entries (200K char cap)."""
    if method == "apirecent":
        head = "# API Recent Results"
    elif method == "apisearch":
        head = f"# API Search Result(s) For {key}"
    elif method == "apitrend":
        head = "# API Trending Results"
    else:
        head = f"# PLUGINS Search Result(s) For {key}"
    blocks = [head + f"\n\n**{len(search_results)} results**\n\n---\n"]
    for index, result in enumerate(search_results, 1):
        name = re.sub(r"([\[\]()*_`])", r"\\\1", str(result.get("name") or "?"))
        url = result.get("url") or "#"
        parts = []
        if result.get("size"):
            parts.append(f"Size: {result['size']}")
        if result.get("seeders") is not None:
            s_line = f"Seeders: {result['seeders']}"
            if result.get("leechers") is not None:
                s_line += f" | Leechers: {result['leechers']}"
            parts.append(s_line)
        tags = []
        for t in ("site", "category", "quality", "language", "format", "date", "uploader"):
            if result.get(t):
                tags.append(str(result[t]))
        line = f"{index}. **[{name}]({url})**"
        if parts:
            line += " — " + " — ".join(parts)
        if tags:
            line += " — " + " • ".join(tags)
        links = []
        if result.get("torrent"):
            dl = _dl_link(
                result["torrent"],
                result.get("name") or "",
                result.get("extension") or "",
                result.get("short") or "",
            )
            links.append(f"[📥 Direct Link]({dl})")
            links.append(f"[📤 Share]({_share_link(dl)})")
        if result.get("download"):
            alt = _dl_link(
                result["download"],
                result.get("name") or "",
                result.get("extension") or "",
                result.get("download_short") or "",
            )
            links.append(f"[🔗 Alt Link]({alt})")
            links.append(f"[📤 Share]({_share_link(alt)})")
        if result.get("magnet"):
            m = result["magnet"]
            if result.get("magnet_short"):
                m = f"{Config.SEARCH_API_LINK}/api/v1/magnet/{result['magnet_short']}"
            links.append(f"[🧲 Magnet]({m})")
            links.append(f"[📤 Telegram]({_magnet_share_link(result['magnet'], result.get('magnet_short') or '')})")
        if links:
            blocks.append(line + "\n\n   " + " | ".join(links) + "\n\n")
        else:
            blocks.append(line + "\n\n")
    return blocks


def _pack_chunks(blocks, limit):
    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) > limit:
            chunks.append(cur)
            cur = b
        else:
            cur += b
    if cur:
        chunks.append(cur)
    return chunks


async def _rentry_new(text):
    """Create one rentry entry; returns the public URL."""
    async with AsyncSession() as client:
        resp = await client.get("https://rentry.co/", timeout=30)
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', resp.text)
        if not m:
            raise RuntimeError("rentry: csrf token not found")
        data = {
            "csrfmiddlewaretoken": m.group(1),
            "text": text,
            "edit_code": token_hex(6),
        }
        resp = await client.post(
            "https://rentry.co/api/new",
            data=data,
            headers={"Referer": "https://rentry.co/"},
            timeout=60,
        )
        js = resp.json()
    if js.get("status") != "200":
        raise RuntimeError("rentry: {}".format(js.get("content") or js))
    return js["url"]


async def _publish_rentry(search_results, key, method, message):
    blocks = _rentry_blocks(search_results, key, method)
    chunks = _pack_chunks(blocks, RENTRY_CHUNK)
    await edit_message(message, f"<b>Creating</b> {len(chunks)} <b>rentry page(s).</b>")
    links = []
    for text in chunks:
        links.append(await _rentry_new(text))
    return "\n".join(links)


API_PAGE_SIZE = 18
SEARCH_CATEGORIES = ["all", "movies", "tv", "anime", "audiobook", "books", "courses", "apps", "games", "music"]

FILTER_QUALITY = ["all", "480", "720", "1080", "4k"]
FILTER_LANGUAGE = ["all", "hindi", "english", "tamil", "telugu", "dual"]
FILTER_FORMAT = ["all", "pdf", "epub", "mobi"]
FILTER_SIZES = ["all", "small", "medium", "large"]

# In-memory filter state (user_id -> dict) so the second filter page can use
# short callbacks (Telegram callback_data is limited to 64 bytes).
FILTER_STATE = {}

# Per-command search options set with /search -l <N> -s <S> -p <spec> -f
# (keyed by (user_id, msg_id) because callbacks only carry the original
# command message id).
SEARCH_OPTS = {}


_DIGIT_FLAGS = {
    "-l": "limit",
    "-s": "seeders",
    "-y": "year",
    "-se": "season",
    "-ep": "episode",
    "-to": "timeout",
}
_WORD_FLAGS = {
    "-p": "page",
    "-x": "include",
    "-g": "site",
    "-hs": "hide_sites",
    "-q": "quality",
    "-lng": "language",
    "-c": "category",
    "-z": "size",
    "-S": "sort",
    "-o": "order",
    "-y": "year",
    "-e": "exclude",
    "-mx": "max_size",
    "-k": "keywords",
    "-w": "format",
    "-au": "author",
}
_FLAG_ONLY = {
    "-f": "fresh",
    "-du": "dedup",
    "-auto": "auto_leech",
    "-ex": "exact",
    "-ad": "adult",
    "-nv": "no_video",
    "-ov": "only_video",
    "--help": "help",
}

_PRESETS = {
    "-hd": {"language": "hindi", "quality": "1080"},
    "-4k": {"quality": "4k"},
    "-480": {"quality": "480"},
    "-720": {"quality": "720"},
    "-1080": {"quality": "1080"},
    "-bd": {"source": "bluray"},
    "-wr": {"source": "webrip,web-dl,webdl"},
    "-pdf": {"format": "pdf"},
    "-epub": {"format": "epub"},
    "-mobi": {"format": "mobi"},
    "-ab": {"category": "audiobook"},
}

# Mirror/leech args forwarded to the leech task when combined with -auto
# (e.g. /s oppenheimer -auto -sp 2GB -n Oppy). Search flags win on conflict.
_LEECH_FLAGS = {
    "-doc", "-med", "-d", "-j", "-b", "-sv", "-ss", "-fd", "-fu",
    "-hl", "-bt", "-ut", "-yt", "-i", "-sp", "-n", "-m", "-meta",
    "-up", "-gc", "-rcf", "-au", "-ap", "-h", "-t", "-ca", "-cv",
    "-ns", "-tl", "-ff",
}
_LEECH_FLAGS_WITH_VALUE = {
    "-i", "-sp", "-n", "-m", "-meta", "-up", "-gc", "-rcf",
    "-au", "-ap", "-h", "-t", "-ca", "-cv", "-ns", "-tl",
}


def _parse_search_cmd(text):
    """Split '/search data science -l 15 -g 1337x,tgx -q 1080p -f -du' into
    ('data science', {'limit': 15, 'site': '1337x,tgx', 'quality': '1080p',
    'fresh': True, 'dedup': True}).

    Args are only recognised at the END, after the key - putting them
    before the key would be confusing."""
    text = text or ""
    # Hold quoted phrases ("data science") as single tokens so spaced words
    # work as flag values: -k "machine learning" stays one keyword.
    quoted = {}

    def _hold(m):
        idx = len(quoted)
        quoted[idx] = m.group(1)
        return "__Q{}__".format(idx)

    text = re.sub(r'"([^"]+)"', _hold, text)
    parts = text.split()
    opts = {}
    i = len(parts) - 1
    while i > 0:
        part = parts[i]
        if part.isdigit() and i - 1 > 0 and parts[i - 1] in _DIGIT_FLAGS:
            opts[_DIGIT_FLAGS[parts[i - 1]]] = int(part)
            i -= 2
            continue
        if i - 1 > 0 and parts[i - 1] in _WORD_FLAGS and not part.startswith("-"):
            opts[_WORD_FLAGS[parts[i - 1]]] = part
            i -= 2
            continue
        if part in _FLAG_ONLY:
            opts[_FLAG_ONLY[part]] = True
            i -= 1
            continue
        if part == "-a":
            opts["site"] = "all"
            opts["all_sites"] = True
            i -= 1
            continue
        if part in _PRESETS:
            opts.update(_PRESETS[part])
            i -= 1
            continue
        if part in _LEECH_FLAGS:
            opts.setdefault("leech_args", [])
            value = (
                parts[i + 1]
                if i + 1 < len(parts) and not parts[i + 1].startswith("-")
                else None
            )
            if value is not None and part in _LEECH_FLAGS_WITH_VALUE:
                opts["leech_args"].insert(0, value)
            opts["leech_args"].insert(0, part)
            i -= 1
            continue
        if (
            i - 1 > 0
            and parts[i - 1] in _LEECH_FLAGS_WITH_VALUE
        ):
            # Current token is the value of a leech flag - skip it, the flag
            # itself is handled on the next iteration.
            i -= 1
            continue
        break
    key = " ".join(parts[1 : i + 1]).strip()

    def _restore(v):
        if isinstance(v, str):
            return re.sub(
                r"__Q(\d+)__",
                lambda m: quoted.get(int(m.group(1)), ""),
                v,
            )
        if isinstance(v, list):
            return [_restore(x) for x in v]
        return v

    key = _restore(key)
    opts = {k: _restore(v) for k, v in opts.items()}
    return key, opts


def _search_opts(message):
    """Options set on the original /search command, else {}.

    Callbacks arrive on a message sent by the bot, so from_user is not the
    clicking user; the reply chain to the original /search command is the
    reliable lookup key."""
    src = message.reply_to_message or message
    opts = SEARCH_OPTS.get(src.id)
    if not opts:
        try:
            uid = message.from_user.id
        except AttributeError:
            uid = None
        if uid is not None:
            opts = SEARCH_OPTS.get((uid, src.id))
            if not opts:
                opts = SEARCH_OPTS.get(uid)
    return opts or {}


def _search_limit(message):
    """Result limit: -l value if given, else SEARCH_LIMIT (0 = unlimited)."""
    limit = _search_opts(message).get("limit")
    return Config.SEARCH_LIMIT if limit is None else limit


_SIZE_RE = re.compile(
    r"^([<>]?)\s*([0-9.]+ ?(?:tb|gb|mb|kb))(?:-([0-9.]+ ?(?:tb|gb|mb|kb)))?$",
    re.I,
)


def _size_bounds(value):
    """Turn '-z 1GB-3GB' / '-z <1GB' / '-z >3GB' into (min_size, max_size)."""
    m = _SIZE_RE.match((value or "").strip())
    if not m:
        return "", ""
    op = m.group(1)
    lo = m.group(2).replace(" ", "").lower()
    hi = (m.group(3) or "").replace(" ", "").lower()
    if hi:
        return lo, hi
    if op == "<":
        return "", lo
    return lo, ""


def _api_extra_params(opts, method, is_all=False):
    """Query-string params for the search API from command-line args."""
    params = []
    if method == "apisearch":
        if opts.get("timeout"):
            params.append(f"timeout={opts['timeout']}")
        elif is_all:
            # -a searches every site: don't let the per-site deadline
            # (40s) skip slow sites - wait up to 10 min so every site
            # returns results. -to always wins when set explicitly.
            params.append("timeout=600")
        fmt = opts.get("format")
        if fmt and "," not in str(fmt):
            params.append(f"format={quote(fmt)}")
        if opts.get("seeders"):
            params.append(f"min_seeders={opts['seeders']}")
        if opts.get("fresh"):
            params.append("fresh=1")
        if opts.get("dedup"):
            params.append("dedup=1")
        include = opts.get("include")
        if include and "," not in str(include):
            params.append(f"include={quote(include)}")
        quality = opts.get("quality")
        if quality and "," not in str(quality):
            params.append(f"quality={quote(quality)}")
        language = opts.get("language")
        if language and "," not in str(language):
            params.append(f"language={quote(language)}")
        if opts.get("category"):
            params.append(f"category={quote(opts['category'])}")
        if opts.get("sort"):
            params.append(f"sort={quote(opts['sort'])}")
        if opts.get("order"):
            params.append(f"order={quote(opts['order'])}")
        if opts.get("size"):
            min_size, max_size = _size_bounds(opts["size"])
            if min_size:
                params.append(f"min_size={quote(min_size)}")
            if max_size:
                params.append(f"max_size={quote(max_size)}")
        if opts.get("max_size"):
            params.append(
                "max_size={}".format(quote(str(opts["max_size"]).replace(" ", "").lower()))
            )
    return ("&" + "&".join(params)) if params else ""


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_RES_RE = re.compile(r"\b(\d{3,4})p\b", re.I)
_LANG_PATTERNS = {
    "hindi": re.compile(r"(?<![a-z0-9])(?:hindi|hin)", re.I),
    "english": re.compile(r"(?<![a-z0-9])(?:english|eng)", re.I),
    "tamil": re.compile(r"(?<![a-z0-9])(?:tamil|tam)", re.I),
    "telugu": re.compile(r"(?<![a-z0-9])(?:telugu|tel)", re.I),
    "malayalam": re.compile(r"(?<![a-z0-9])(?:malayalam|mal)", re.I),
    "kannada": re.compile(r"(?<![a-z0-9])(?:kannada|kan)", re.I),
    "bengali": re.compile(r"(?<![a-z0-9])bengali", re.I),
    "punjabi": re.compile(r"(?<![a-z0-9])punjabi", re.I),
    "marathi": re.compile(r"(?<![a-z0-9])marathi", re.I),
    "gujarati": re.compile(r"(?<![a-z0-9])gujarati", re.I),
    "dubbed": re.compile(r"(?<![a-z0-9])(?:dubbed|dub)", re.I),
    "dual": re.compile(r"(?<![a-z0-9])dual", re.I),
    "multi": re.compile(r"(?<![a-z0-9])multi", re.I),
}


def _year_bounds(value):
    """Turn '-y 2023' / '-y 1977-2005' into (lo, hi) or None."""
    value = str(value or "").strip()
    if not value:
        return None
    if "-" in value:
        lo, _, hi = value.partition("-")
        if lo.isdigit() and hi.isdigit():
            return int(lo), int(hi)
        return None
    if value.isdigit():
        y = int(value)
        return y, y
    return None


def _result_year(item):
    """First 19xx/20xx year found in the result name or date."""
    m = _YEAR_RE.search(
        "{} {}".format(str(item.get("name") or ""), str(item.get("date") or ""))
    )
    return int(m.group(0)) if m else None


_EXACT_SPLIT = re.compile(r"[:|()\-–—]")


def _normalize_title(text):
    return " ".join(re.sub(r"[^\w]+", " ", str(text).lower()).split())


def _exact_matches(item, query):
    """Exact title match: main title (before : | - ( ) separators) must equal
    the query, so 'Ikigai for Teens'/'The Ikigai Journey' drop but
    'Ikigai: The Japanese Secret' stays."""
    title = str(item.get("name") or "")
    head = _normalize_title(_EXACT_SPLIT.split(title, 1)[0])
    qs = [_normalize_title(q) for q in str(query).split(",") if q.strip()]
    if not qs:
        return True
    return head in qs


def _season_episode_matches(item, season, episode):
    """Match a series result by season/episode (S05E03, S5E3, Season 5...)."""
    name = str(item.get("name") or "").lower()
    if season is not None:
        s = int(season)
        if episode is not None:
            e = int(episode)
            pat = re.compile(r"s0?{0}e0?{1}(?![a-z0-9])".format(s, e))
        else:
            pat = re.compile(
                r"(?:s0?{0}(?![a-z0-9])|season\s*0?{0}(?!\d))".format(s)
            )
    else:
        e = int(episode)
        pat = re.compile(
            r"(?:e0?{0}(?![a-z0-9])|episode\s*0?{0}(?!\d))".format(e)
        )
    return pat.search(name) is not None


def _quality_matches(item, quality):
    """Match a result by resolution (480/720/1080/4k) - mirrors t-api."""
    q = str(quality or "").lower().strip().replace("p", "")
    if not q:
        return True
    name = str(item.get("name") or "")
    res = set(int(m.group(1)) for m in _RES_RE.finditer(name))
    if re.search(r"\b(4k|uhd|2160p)\b", name, re.I):
        res.add(2160)
    if not res:
        return False
    if q in ("4k", "2160"):
        return max(res) >= 2160
    try:
        return int(q) in res
    except ValueError:
        return False


def _language_matches(item, language):
    """Match a result by language - mirrors t-api (_LANG_PATTERNS)."""
    lang = str(language or "").lower().strip()
    if not lang:
        return True
    text = (
        str(item.get("name") or "") + " " + str(item.get("category") or "")
        + " " + str(item.get("language") or "") + " "
        + str(item.get("languages") or "")
    ).lower()
    pattern = _LANG_PATTERNS.get(lang)
    if pattern is not None:
        return pattern.search(text) is not None
    return re.search(r"(?<![a-z0-9])" + re.escape(lang), text) is not None


def _format_matches(item, fmt):
    """Match a result by file format (mkv/pdf/epub...) - mirrors t-api."""
    f = str(fmt or "").lower().strip().lstrip(".")
    if not f:
        return True
    text = (
        str(item.get("name") or "") + " " + str(item.get("category") or "")
        + " " + str(item.get("extension") or "") + " "
        + str(item.get("torrent") or "") + " " + str(item.get("download") or "")
    ).lower()
    return (
        re.search(r"(?<![a-z0-9])\.?" + re.escape(f) + r"(?![a-z0-9])", text)
        is not None
    )


def _author_matches(item, author):
    """Match a book result by author (authors field + author + name fallback)."""
    a = str(author or "").lower().replace("_", " ").strip()
    if not a:
        return True
    text = " ".join(
        str(x) for x in (item.get("authors") or [])
    )
    if item.get("author"):
        text += " " + str(item["author"])
    text += " " + str(item.get("name") or "")
    return a in text.lower().replace("_", " ")


ADULT_KEYWORDS = (
    "porn", "xxx", "hentai", "onlyfans", "nsfw", "milf", "creampie",
    "blowjob", "sextape", "sex tape", "bukkake", "squirt", "dildo",
    "gangbang", "bdsm", "erotic", "erotica", "fuck", "horny", "manyvids",
    "tits", "boobs", "naked", "nude", "cumshot", "vagina", "penis",
    "orgasm", "pornstar", "squirting", "incest", "whore", "slut", "bitch",
    "stepmom", "stepsister",
)
# Whole-word matches only: "anal" never hits "analog", "sex" never hits
# "Essex"/"Middlesex", "cum" never hits "document", "cock" never hits
# "cocktail", "dick" never hits "Dickens", "pussy" never hits "Pussycat".
ADULT_WORD_KEYWORDS = ("anal", "sex", "cum", "cock", "dick", "pussy", "rape", "ass")


def _is_adult(name):
    low = str(name or "").lower()
    for kw in ADULT_WORD_KEYWORDS:
        if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", low):
            return True
    return any(kw in low for kw in ADULT_KEYWORDS)


# Video-release markers: quality, container, release-type words. -nv drops
# these, handy for course/book searches where video uploads should not
# appear. Distinctive tokens match as plain substrings so glued release
# names like "1080pAmazonWEB-DL" are still caught; short/ambiguous tokens
# ("avi", "aac") stay whole-word so "Aviator"/"Isaac" are never hit.
VIDEO_KEYWORDS = (
    "1080p", "2160p", "720p", "480p", "360p", "4k", "8k", "hdrip",
    "brrip", "webrip", "web-dl", "webdl", "bluray", "blu-ray", "remux",
    "hdtv", "dvdrip", "camrip", "h264", "h265", "x264", "x265", "hevc",
    "dd5.1", "imax", "mkv", "mp4", "m2ts", "movies", "xvid", "divx",
    "eztv",
)
VIDEO_WORD_KEYWORDS = ("avi", "aac", "hd")
# Dolby Digital tag written with spaces: "DD 5.1", "DD 5 1", "DD5.1",
# plus TV episode patterns: S03E01 / S03 E01 / S3E1
VIDEO_RE_PATTERNS = (
    r"dd\s*5\s*\.?\s*1",
    r"(?<![a-z0-9])s\d{1,2}\s*e\d{1,2}(?![a-z0-9])",
)
# Categories -ov never keeps even if they look like video: no anime and
# no adult content - user wants movies/webseries only.
ANIME_CATEGORIES = {"anime", "cartoon", "cartoons", "animation", "animated"}
OV_DROP_CATEGORIES = ANIME_CATEGORIES | {"xxx", "porn", "adult"}

# Result categories that mean video/TV content - -nv drops these too and
# -ov keeps them. Exact lowercased match, so "Video Training" (courses)
# is NOT treated as video.
VIDEO_CATEGORIES = {
    "movies", "movie", "films", "film",
    "tv", "tv-shows", "tv shows", "television", "series", "episodes",
    "episode", "shows", "show", "webseries", "web series", "web-series",
    "webisodes", "drama", "reality", "sitcom", "miniseries", "mini-series",
    "documentary", "documentaries", "docs",
    "sports", "concerts", "concert", "music videos", "music video",
    "live shows", "standup", "stand-up", "comedy",
    "wrestling", "short films", "short film", "shorts", "trailers",
    "video", "anime", "cartoon", "cartoons", "animation", "animated",
    "xxx", "porn",
}


def _is_video(name):
    low = str(name or "").lower()
    if any(kw in low for kw in VIDEO_KEYWORDS):
        return True
    if any(
        re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", low)
        for kw in VIDEO_WORD_KEYWORDS
    ):
        return True
    return any(re.search(pat, low) for pat in VIDEO_RE_PATTERNS)


def _dedup_rows(rows):
    """-du: drop rows that repeat the same release. Keyed by the infohash
    first, so the same torrent on multiple sites collapses into one row;
    rows without a hash fall back to the normalized name. Same-name rows
    with different hashes are different releases and stay separate."""
    seen = set()
    out = []
    for it in rows:
        h = str(it.get("hash") or "").strip().lower()
        if h:
            k = h
        else:
            k = re.sub(r"\s+", " ", str(it.get("name") or "")).strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _words(value, underscore=False):
    """Comma-separated filter words -> lowercased list (underscores -> spaces)."""
    return [
        (w.strip().lower().replace("_", " ") if underscore else w.strip().lower())
        for w in str(value or "").split(",")
        if w.strip()
    ]


def _apply_client_filters(results, opts, query=""):
    """Client-side filters for args t-api has no query param for (-y, -e, -n,
    -k) plus multi-value -q/-lng (t-api takes a single value per param).

    Applied after the API returns so the site adapters stay untouched.
    Results without a parseable year/date are kept (filter only drops what
    it can positively reject)."""
    out = results
    if opts.get("exact"):
        out = [r for r in out if _exact_matches(r, query)]
    include = opts.get("include")
    if include and "," in str(include):
        words = _words(include)
        if words:
            out = [r for r in out if any(w in str(r.get("name") or "").lower() for w in words)]
    quality = opts.get("quality")
    if quality and "," in str(quality):
        quals = _words(quality)
        if quals:
            out = [r for r in out if any(_quality_matches(r, q) for q in quals)]
    fmt = opts.get("format")
    if fmt and "," in str(fmt):
        formats = [w.lstrip(".") for w in _words(fmt)]
        if formats:
            out = [r for r in out if any(_format_matches(r, f) for f in formats)]
    author = opts.get("author")
    if author:
        words = _words(author, underscore=True)
        if words:
            out = [r for r in out if any(_author_matches(r, w) for w in words)]
    language = opts.get("language")
    if language and "," in str(language):
        langs = _words(language)
        if langs:
            out = [r for r in out if any(_language_matches(r, l) for l in langs)]
    keywords = opts.get("keywords")
    if keywords:
        words = _words(keywords, underscore=True)
        if words:
            out = [r for r in out if all(w in str(r.get("name") or "").lower() for w in words)]
    bounds = _year_bounds(opts.get("year"))
    if bounds:
        lo, hi = bounds
        out = [r for r in out if lo <= (_result_year(r) or -1) <= hi]
    season = opts.get("season")
    episode = opts.get("episode")
    if season is not None or episode is not None:
        out = [
            r for r in out
            if _season_episode_matches(r, season, episode)
        ]
    source = opts.get("source")
    if source:
        words = _words(source)
        if words:
            out = [r for r in out if any(w in str(r.get("name") or "").lower() for w in words)]
    exclude = opts.get("exclude")
    if exclude:
        words = _words(exclude)
        if words:
            out = [r for r in out if not any(w in str(r.get("name") or "").lower() for w in words)]
    if opts.get("adult"):
        out = [r for r in out if not _is_adult(str(r.get("name") or ""))]
    if opts.get("only_video") and not opts.get("no_video"):
        # -ov: sirf video/webseries/movie results rakho (-nv ka ulta) -
        # 480p/720p/1080p/4K/mkv/mp4/WEB-DL/HDTV/SxxEyy sab wahi words jo
        # -nv hatata hai. Anime/cartoon category hamesha drop (movies/
        # webseries chahiye, anime nahi).
        out = [
            r
            for r in out
            if str(r.get("category") or "").strip().lower() not in OV_DROP_CATEGORIES
            and (
                _is_video(str(r.get("name") or ""))
                or str(r.get("category") or "").strip().lower() in VIDEO_CATEGORIES
            )
        ]
    if opts.get("no_video"):
        out = [
            r
            for r in out
            if not _is_video(str(r.get("name") or ""))
            and str(r.get("category") or "").strip().lower() not in VIDEO_CATEGORIES
        ]
    return out


SEARCH_HELP_TEXT = (
    "<b>🔍 Torrent Search Args</b>\n\n"
    "Format: <code>/s &lt;key&gt; [args]</code> — args hamesha <b>key ke baad</b>\n\n"
    "• <code>-l &lt;n&gt;</code> → result limit\n"
    "• <code>-s &lt;n&gt;</code> → min seeders\n"
    "• <code>-p &lt;spec&gt;</code> → pages: <code>-p 1</code>, range <code>-p 1-4</code>, unlimited <code>-p 0</code>\n"
    "• <code>-to &lt;sec&gt;</code> → speed: slow sites skip (<code>-to 10</code> = max 10s)\n"
    "• <code>-f</code> → fresh (cache skip)\n"
    "• <code>-du</code> → duplicate protection ON\n"
    "• <code>-x &lt;word&gt;</code> → sirf us word wale results\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Multi: <code>-x 1080p,4k</code>\n"
    "• <code>-k &lt;words&gt;</code> → title me SAB words match (strict): <code>complete,course</code>\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Spaced: <code>-k \"machine learning\"</code> ya <code>machine_learning</code>\n"
    "• <code>-e &lt;words&gt;</code> → exclude words: <code>hindi,audible</code>\n"
    "• <code>-ad</code> → adult/porn results hatao\n"
    "• <code>-nv</code> → video results hatao (1080p/mkv/webrip) - courses ke liye useful\n"
    "• <code>-ov</code> → SIRF video results (movies/webseries/TV): 480p/720p/1080p/4K/mkv/WEB-DL/S01E02 sab - anime/cartoon nahi - <code>-nv</code> ka ulta\n"
    "• <code>-g &lt;site&gt;</code> → direct search, buttons skip\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Groups: <code>all</code> (17 sites), <code>books</code>, <code>courses</code>\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Multiple: <code>1337x,tgx,yts</code>\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Hide sites: <code>-hs 1337x,tgx</code> (ulta <code>-g</code>)\n"
    "• <code>-a</code> → SAB sites ek sath (31: general + books + courses)\n"
    "• <code>-q &lt;quality&gt;</code> → <code>480</code>, <code>720</code>, <code>1080</code>, <code>4k</code>\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Multi: <code>-q 1080p,4k</code>\n"
    "• <code>-lng &lt;lang&gt;</code> → <code>hindi</code>, <code>english</code>, <code>tamil</code>, <code>telugu</code>, <code>dual</code>\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Multi: <code>-lng hindi,english</code>\n"
    "• <code>-c &lt;cat&gt;</code> → <code>movies</code>, <code>tv</code>, <code>music</code>, <code>anime</code>, <code>audiobook</code>, <code>course</code>, <code>book</code>, <code>game</code>, <code>app</code>\n"
    "• <code>-z &lt;size&gt;</code> → <code>&lt;1GB</code>, <code>&gt;3GB</code>, <code>1GB-3GB</code>\n"
    "• <code>-mx &lt;size&gt;</code> → max size cap: <code>2GB</code>\n"
    "• <code>-w &lt;format&gt;</code> → file type: <code>mkv</code>, <code>mp4</code>, <code>pdf</code>\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Multi: <code>-w pdf,epub,mobi</code>\n"
    "• <code>-au &lt;author&gt;</code> → author filter (books): <code>-au james</code>, multi-word: <code>-au james_clear</code>\n"
    "• <code>-ex</code> → exact title match (books): <code>-ex</code>\n"
    "• <code>-S &lt;sort&gt;</code> → <code>seeders</code>, <code>size</code>, <code>date</code>\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;<code>quality</code> bhi: <code>-S quality</code>\n"
    "• <code>-o &lt;order&gt;</code> → <code>asc</code>, <code>desc</code>\n"
    "• <code>-y &lt;year&gt;</code> → saal filter: <code>2023</code> ya <code>1977-2005</code>\n"
    "• <code>-se &lt;n&gt;</code> → season filter: <code>-se 5</code> (S05)\n"
    "• <code>-ep &lt;n&gt;</code> → episode filter: <code>-ep 3</code> (S01E03 ke sath best)\n"
    "• <code>-auto</code> → best (top seeders) result DIRECT leech\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Leech args passthrough: <code>-sp 2GB -n Name -d</code>\n\n"
    "Presets (Hindi movies):\n"
    "• <code>-hd</code> → hindi + 1080p | <code>-4k</code> → 4K\n"
    "• <code>-480</code>/<code>-720</code>/<code>-1080</code> → resolution\n"
    "• <code>-bd</code> → bluray | <code>-wr</code> → webrip\n"
    "Books presets:\n"
    "• <code>-pdf</code>/<code>-epub</code>/<code>-mobi</code> → format\n"
    "• <code>-ab</code> → audiobook category\n\n"
    "Examples:\n"
    "<code>/s oppenheimer -g 1337x,tgx -l 10 -f</code>\n"
    "<code>/s ikigai -g books -lng hindi -x pdf</code>\n"
    "<code>/s python -c course -z 1GB-3GB</code>\n"
    "<code>/s oppenheimer -y 2023 -mx 4GB</code>\n"
    "<code>/s star wars -y 1977-2005 -e hindi -s 5</code>\n"
    "<code>/s oppenheimer -hs 1337x,tgx -q 1080p,4k</code>\n"
    "<code>/s python -k complete,course -lng hindi,english</code>\n"
    "<code>/s oppenheimer -hd -bd</code>\n"
    "<code>/s breaking bad -se 5 -hd</code>\n"
    "<code>/s oppenheimer,interstellar -4k</code>\n"
    "<code>/s oppenheimer -w mkv</code>\n"
    "<code>/s oppenheimer -auto -sp 2GB -n Oppy</code>\n"
    "<code>/s ikigai -g books -epub</code>\n"
    "<code>/s atomic habits -ab</code>\n"
    "<code>/s ikigai -au ikigai -w pdf,epub -g books</code>"
    "\n<code>/s ikigai -ex -g books</code>"
)


def _filter_label(value, kind):
    if not value or value == "all":
        return "All"
    if kind == "quality":
        return "4K" if value == "4k" else f"{value}p"
    return value.capitalize()


def _filter_summary(quality, language, format_):
    parts = []
    if quality and quality != "all":
        parts.append(f"Quality: {_filter_label(quality, 'quality')}")
    if language and language != "all":
        parts.append(f"Language: {_filter_label(language, 'language')}")
    if format_ and format_ != "all":
        parts.append(f"Format: {_filter_label(format_, 'format')}")
    return " | ".join(parts) if parts else "None"


def filter_buttons(user_id, site, category, quality, language, format_):
    buttons = ButtonMaker()
    for v in FILTER_QUALITY:
        name = _filter_label(v, "quality")
        if v == quality:
            name = f"✅ {name}"
        buttons.data_button(
            name,
            f"torser {user_id} fq {site} {category} {v} {language} {format_}",
        )
    for v in FILTER_LANGUAGE:
        name = _filter_label(v, "language")
        if v == language:
            name = f"✅ {name}"
        buttons.data_button(
            name,
            f"torser {user_id} fl {site} {category} {quality} {v} {format_}",
            position="header",
        )
    for v in FILTER_FORMAT:
        name = _filter_label(v, "format")
        if v == format_:
            name = f"✅ {name}"
        buttons.data_button(
            name,
            f"torser {user_id} ff {site} {category} {quality} {language} {v}",
            position="f_body",
        )
    buttons.data_button(
        "✅ Size & More",
        f"torser {user_id} fmore {site} {category} {quality} {language} {format_}",
        position="footer",
    )
    buttons.data_button(
        "◀ Back",
        f"torser {user_id} filtback {site} {category}",
        position="footer",
    )
    return buttons.build_menu(b_cols=5, h_cols=6, fb_cols=4, f_cols=2)


def filter_menu_text(key, site, category, quality, language, format_):
    return (
        f"<b>Filter results for <i>{key}</i></b>\n"
        f"Site:- <i>{_site_display_name(site)}</i>\n"
        f"Category:- <i>{category.capitalize()}</i>\n"
        f"Filters:- <i>{_filter_summary(quality, language, format_)}</i>"
    )


def _size_label(value):
    return {"all": "All", "small": "<1GB", "medium": "1-3GB", "large": ">3GB"}.get(
        value, value
    )


def _size_summary(size):
    parts = []
    if size and size != "all":
        parts.append(f"Size: {_size_label(size)}")
    return " | ".join(parts) if parts else "None"


def filter_size_buttons(user_id, size):
    buttons = ButtonMaker()
    for v in FILTER_SIZES:
        name = _size_label(v)
        if v == size:
            name = f"✅ {name}"
        buttons.data_button(name, f"torser {user_id} sz {v}")
    buttons.data_button("✅ Search", f"torser {user_id} fgs", position="footer")
    buttons.data_button("◀ Back", f"torser {user_id} fback1", position="footer")
    return buttons.build_menu(b_cols=4, f_cols=2)


def filter_size_text(key, size):
    return (
        f"<b>More filters for <i>{key}</i></b>\n"
        f"Filters:- <i>{_size_summary(size)}</i>"
    )


def filter_group_buttons(user_id, group, language, format_, size):
    """Group filter menu: Courses gets size only, Books gets language+format."""
    buttons = ButtonMaker()
    if group == "courses":
        for v in FILTER_SIZES:
            name = _size_label(v)
            if v == size:
                name = f"✅ {name}"
            buttons.data_button(name, f"torser {user_id} gsz {v}")
    else:
        for v in FILTER_LANGUAGE:
            name = _filter_label(v, "language")
            if v == language:
                name = f"✅ {name}"
            buttons.data_button(name, f"torser {user_id} gl {v} {format_}")
        for v in FILTER_FORMAT:
            name = _filter_label(v, "format")
            if v == format_:
                name = f"✅ {name}"
            buttons.data_button(name, f"torser {user_id} gf {language} {v}")
    buttons.data_button(
        "✅ Search",
        f"torser {user_id} ggo {language} {format_} {size}",
        position="footer",
    )
    buttons.data_button("◀ Back", f"torser {user_id} backcat", position="footer")
    return buttons.build_menu(b_cols=4, f_cols=2)


def filter_group_text(key, group, language, format_, size):
    parts = []
    if language and language != "all":
        parts.append(f"Language: {_filter_label(language, 'language')}")
    if format_ and format_ != "all":
        parts.append(f"Format: {_filter_label(format_, 'format')}")
    if size and size != "all":
        parts.append(f"Size: {_size_label(size)}")
    summary = " | ".join(parts) if parts else "None"
    return (
        f"<b>Filter results for <i>{key}</i></b>\n"
        f"Category:- <i>{_site_display_name(group)}</i>\n"
        f"Filters:- <i>{summary}</i>"
    )


# E-book sites get a format picker (pdf/epub/mobi) before searching;
# audiobook sites (hindiaudio/audiobookbay) and every other site search
# directly with one click - audio formats are not pdf/epub/mobi.
BOOK_SITES = {
    "hindibooks",
    "annasarchive",
    "libgen",
    "archivebooks",
    "gutenberg",
}

# Category-group buttons: Courses and Books search their whole group at
# once; every other site keeps its own button. Groups are built from the
# sites the API currently has enabled, so dead sites never appear.
COURSE_SITES = {
    "downarchive",
    "freecourseweb",
    "freecoursesites",
    "rutracker",
    "pimpmymind",
    "thedownloadly",
}
BOOK_GROUP_SITES = BOOK_SITES | {
    "hindiaudio",
    "oceanofpdf",
}
GROUP_SITES = {
    "courses": COURSE_SITES,
    "books": BOOK_GROUP_SITES,
}
GROUP_NAMES = {
    "all": "All",
    "courses": "Courses",
    "books": "Books",
}


def filter_format_buttons(user_id, site, format_):
    buttons = ButtonMaker()
    for v in FILTER_FORMAT:
        name = _filter_label(v, "format")
        if v == format_:
            name = f"✅ {name}"
        buttons.data_button(name, f"torser {user_id} bf {site} {v}")
    buttons.data_button(
        "✅ Search", f"torser {user_id} bfgo {site} {format_}", position="footer"
    )
    buttons.data_button("◀ Back", f"torser {user_id} backcat", position="footer")
    return buttons.build_menu(b_cols=4, f_cols=2)


def filter_format_text(key, site, format_):
    return (
        f"<b>Select format for <i>{key}</i></b>\n"
        f"Site:- <i>{_site_display_name(site)}</i>\n"
        f"Format:- <i>{_filter_label(format_, 'format')}</i>"
    )


def api_categories(user_id, site):
    buttons = ButtonMaker()
    for cat in SEARCH_CATEGORIES:
        name = "All" if cat == "all" else cat.capitalize()
        buttons.data_button(name, f"torser {user_id} catsel {site} {cat}")
    buttons.data_button("◀ Back", f"torser {user_id} backcat", position="footer")
    return buttons.build_menu(2)


async def api_buttons(user_id, method, page=1):
    await _refresh_sites()
    buttons = ButtonMaker()
    if not SITES:
        buttons.data_button("Cancel", f"torser {user_id} cancel")
        return buttons.build_menu(1)
    # Fixed group buttons on top, then the per-site list (minus the sites
    # that already live inside the Courses/Books groups).
    for group, name in [("courses", "🎓 Courses"), ("books", "📚 Books")]:
        buttons.data_button(name, f"torser {user_id} grp {group} {method}")
    grouped = COURSE_SITES | BOOK_GROUP_SITES
    sites = sorted(
        (
            (s, n)
            for s, n in SITES.items()
            if s == "all" or s not in grouped
        ),
        key=_site_sort_key,
    )
    total_pages = max(1, (len(sites) + API_PAGE_SIZE - 1) // API_PAGE_SIZE)
    page = max(1, min(int(page), total_pages))
    start = (page - 1) * API_PAGE_SIZE
    for data, name in sites[start : start + API_PAGE_SIZE]:
        buttons.data_button(_site_display_name(data), f"torser {user_id} {data} {method}")
    nav = []
    if page > 1:
        nav.append(("◀", f"torser {user_id} apipage {page - 1} {method}"))
    nav.append((f"{page}/{total_pages}", f"torser {user_id} apipage {page} {method}"))
    if page < total_pages:
        nav.append(("▶", f"torser {user_id} apipage {page + 1} {method}"))
    for text, callback in nav:
        buttons.data_button(text, callback, position="footer")
    buttons.data_button("Cancel", f"torser {user_id} cancel", position="footer")
    return buttons.build_menu(2)


async def plugin_buttons(user_id):
    buttons = ButtonMaker()
    if not PLUGINS:
        pl = await TorrentManager.qbittorrent.search.plugins()
        for i in pl:
            PLUGINS.append(i.name)
    for siteName in PLUGINS:
        buttons.data_button(
            siteName.capitalize(), f"torser {user_id} {siteName} plugin"
        )
    buttons.data_button("All", f"torser {user_id} all plugin")
    buttons.data_button("Cancel", f"torser {user_id} cancel")
    return buttons.build_menu(2)


@new_task
async def torrent_search(_, message):
    if Config.DISABLE_SEARCH:
        await send_message(
            message, "Torrent search is currently disabled by the Bot Owner."
        )
        return
    user_id = message.from_user.id
    buttons = ButtonMaker()
    key, opts = _parse_search_cmd(message.text)
    if opts.get("help"):
        await send_message(message, SEARCH_HELP_TEXT)
        return
    first_tok = (message.text or "").split()
    if len(first_tok) > 1 and first_tok[1].startswith("-"):
        await send_message(
            message,
            "❌ Args ko <b>key ke baad</b> rakho!\n\n"
            "Example: <code>/s oppenheimer -l 10 -f -du</code>\n"
            "<code>/s --help</code> se saare args dekho",
        )
        return
    if opts:
        SEARCH_OPTS[(user_id, message.id)] = opts
        SEARCH_OPTS[user_id] = opts
        SEARCH_OPTS[message.id] = opts
    else:
        SEARCH_OPTS.pop(user_id, None)
        SEARCH_OPTS.pop(message.id, None)
    direct_site = opts.get("site")
    if direct_site and not key:
        await send_message(
            message,
            "Send a search key along with command\n"
            "Usage: /s <key> -g <site>\n"
            "Example: /s oppenheimer -g 1337x,tgx",
        )
        return
    if direct_site:
        if SITES is None and Config.SEARCH_API_LINK:
            api_ready = await _refresh_sites()
        if SITES is None:
            await send_message(
                message, "Search API is unavailable right now. Try again in a bit."
            )
            return
        requested = [s.strip() for s in direct_site.split(",") if s.strip()]
        unknown = [s for s in requested if s not in GROUP_NAMES and s not in SITES]
        if unknown:
            await send_message(
                message,
                f"❌ Invalid site: <code>{escape(', '.join(unknown))}</code>\n\n"
                "Groups: <code>all, books, courses</code>\n"
                "Sites: buttons se site ka naam dekho (jaise <code>1337x, tgx, audiobookbay</code>)",
            )
            return
        searching = await send_message(
            message,
            f"<b>Searching for <i>{escape(key)}</i>\n"
            f"Torrent Site:- <i>{_site_display_name(direct_site)}</i></b>",
        )
        if searching is not None:
            # The direct-search message's reply_to_message is not reliable
            # (Pyrogram may drop it), so also key the options by this
            # message id - _search_opts falls back to src.id.
            SEARCH_OPTS[searching.id] = opts
            SEARCH_OPTS[user_id] = opts
            await search(key, direct_site, searching, "apisearch")
        return
    # Bare "/search" (or "/search -l 5" with no key) keeps the old
    # Trending/Recent menu; single-word keys stay normal search keys.
    key = key.split() if key else []
    api_ready = True
    if SITES is None and Config.SEARCH_API_LINK:
        # API was down at bot start: try to recover the site list now,
        # otherwise the bot would need a restart to ever show buttons.
        api_ready = await _refresh_sites()
    if SITES is None and not Config.SEARCH_PLUGINS:
        if Config.SEARCH_API_LINK and not api_ready:
            await send_message(
                message, "Search API is unavailable right now. Try again in a bit."
            )
        else:
            await send_message(
                message, "No API link or search PLUGINS added for this function"
            )
    elif not key and SITES is None:
        await send_message(message, "Send a search key along with command")
    elif not key:
        buttons.data_button("Trending", f"torser {user_id} apitrend")
        buttons.data_button("Recent", f"torser {user_id} apirecent")
        buttons.data_button("Cancel", f"torser {user_id} cancel")
        button = buttons.build_menu(2)
        await send_message(
            message,
            "Send a search key along with command\n"
            "Usage: /search <key> [-l <n>] [-s <n>] [-p <spec>] [-f] [-du] [-x <w>] [-g <site>] [-a] [-q <q>] [-lng <l>] [-c <c>] [-z <size>] [-S <sort>] [-o <order>]\n/s --help for all args",
            button,
        )
    elif SITES is not None and Config.SEARCH_PLUGINS:
        buttons.data_button("Api", f"torser {user_id} apisearch")
        buttons.data_button("Plugins", f"torser {user_id} plugin")
        buttons.data_button("Cancel", f"torser {user_id} cancel")
        button = buttons.build_menu(2)
        await send_message(message, "Choose tool to search:", button)
    elif SITES is not None:
        button = await api_buttons(user_id, "apisearch")
        await send_message(message, "Choose site to search | API:", button)
    else:
        button = await plugin_buttons(user_id)
        await send_message(message, "Choose site to search | Plugins:", button)


def _searching_msg(key, site, extra=""):
    """'Searching for...' status line used by every search entry point."""
    return f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{_site_display_name(site)}</i>{extra}</b>"


def _listing_msg(site, endpoint):
    """'Listing Trending/Recent items...' status line."""
    return f"<b>Listing {endpoint} Items...\nTorrent Site:- <i>{_site_display_name(site)}</i></b>"


def _parse_filters(data):
    """site/category/quality/language/format from a filter callback row."""
    return (
        data[3] if len(data) > 3 else "all",
        data[4] if len(data) > 4 else "all",
        data[5] if len(data) > 5 else "all",
        data[6] if len(data) > 6 else "all",
        data[7] if len(data) > 7 else "all",
    )


async def _need_state(message, user_id, site=None):
    """FILTER_STATE lookup; missing/wrong-group state falls back to the site
    picker so a stale callback never 500s."""
    state = FILTER_STATE.get(user_id)
    if state and site and state.get("site") != site:
        state = None
    if not state:
        button = await api_buttons(user_id, "apisearch")
        await edit_message(message, "Choose site:", button)
        return None
    return state


@new_task
async def torrent_search_update(_, query):
    user_id = query.from_user.id
    message = query.message
    key, _ = _parse_search_cmd(message.reply_to_message.text)
    key = key or None
    data = query.data.split()
    if user_id != int(data[1]):
        await query.answer("Not Yours!", show_alert=True)
    elif data[2] == "apipage":
        await query.answer()
        page = int(data[3]) if len(data) > 3 and data[3].isdigit() else 1
        method = data[4] if len(data) > 4 else "apisearch"
        button = await api_buttons(user_id, method, page)
        await edit_message(message, "Choose site:", button)
    elif data[2].startswith("api"):
        await query.answer()
        button = await api_buttons(user_id, data[2])
        await edit_message(message, "Choose site:", button)
    elif data[2] == "plugin":
        await query.answer()
        button = await plugin_buttons(user_id)
        await edit_message(message, "Choose site:", button)
    elif data[2] == "catsel":
        await query.answer()
        site = data[3] if len(data) > 3 else "all"
        category = data[4] if len(data) > 4 else "all"
        FILTER_STATE.pop(user_id, None)
        button = filter_buttons(user_id, site, category, "all", "all", "all")
        await edit_message(
            message,
            filter_menu_text(key, site, category, "all", "all", "all"),
            button,
        )
    elif data[2] in ("fq", "fl", "ff"):
        await query.answer()
        site, category, quality, language, format_ = _parse_filters(data)
        button = filter_buttons(user_id, site, category, quality, language, format_)
        await edit_message(message, filter_menu_text(key, site, category, quality, language, format_), button)
    elif data[2] == "fmore":
        await query.answer()
        site, category, quality, language, format_ = _parse_filters(data)
        prev = FILTER_STATE.get(user_id) or {}
        FILTER_STATE[user_id] = {
            "site": site,
            "category": category,
            "quality": quality,
            "language": language,
            "format": format_,
            "size": prev.get("size", "all"),
        }
        button = filter_size_buttons(user_id, FILTER_STATE[user_id]["size"])
        await edit_message(
            message,
            filter_size_text(key, FILTER_STATE[user_id]["size"]),
            button,
        )
    elif data[2] == "sz":
        await query.answer()
        state = await _need_state(message, user_id)
        if not state:
            return
        state["size"] = data[3] if len(data) > 3 else "all"
        button = filter_size_buttons(user_id, state["size"])
        await edit_message(message, filter_size_text(key, state["size"]), button)
    elif data[2] == "fback1":
        await query.answer()
        state = FILTER_STATE.get(user_id) or {}
        site = state.get("site", "all")
        category = state.get("category", "all")
        quality = state.get("quality", "all")
        language = state.get("language", "all")
        format_ = state.get("format", "all")
        button = filter_buttons(user_id, site, category, quality, language, format_)
        await edit_message(message, filter_menu_text(key, site, category, quality, language, format_), button)
    elif data[2] == "bf":
        await query.answer()
        site = data[3] if len(data) > 3 else "all"
        format_ = data[4] if len(data) > 4 else "all"
        button = filter_format_buttons(user_id, site, format_)
        await edit_message(message, filter_format_text(key, site, format_), button)
    elif data[2] == "bfgo":
        await query.answer()
        site = data[3] if len(data) > 3 else "all"
        format_ = data[4] if len(data) > 4 else "all"
        fmt = (
            f"\nFormat:- <i>{_filter_label(format_, 'format')}</i>"
            if format_ != "all" else ""
        )
        await edit_message(message, _searching_msg(key, site, fmt))
        await search(key, site, message, "apisearch", "all", "all", "all", format_)
    elif data[2] == "fgs":
        await query.answer()
        state = await _need_state(message, user_id)
        if not state:
            return
        site = state["site"]
        category = state["category"]
        quality = state["quality"]
        language = state["language"]
        format_ = state["format"]
        size = state["size"]
        summary = " | ".join(
            p
            for p in (
                _filter_summary(quality, language, format_),
                _size_summary(size),
            )
            if p and p != "None"
        ) or "None"
        await edit_message(
            message,
            _searching_msg(key, site, f"\nCategory:- <i>{category.capitalize()}</i>\nFilters:- <i>{summary}</i>"),
        )
        await search(key, site, message, "apisearch", category, quality, language, format_, size)
        FILTER_STATE.pop(user_id, None)
    elif data[2] == "filtback":
        await query.answer()
        site = data[3] if len(data) > 3 else "all"
        button = api_categories(user_id, site)
        await edit_message(message, "Choose site:", button)
    elif data[2] == "backcat":
        await query.answer()
        button = await api_buttons(user_id, "apisearch")
        await edit_message(message, "Choose site:", button)
    elif data[2] == "grp":
        await query.answer()
        group = data[3] if len(data) > 3 else "all"
        method = data[4] if len(data) > 4 else "apisearch"
        if method == "apisearch":
            FILTER_STATE[user_id] = {
                "site": group,
                "category": "all",
                "quality": "all",
                "language": "all",
                "format": "all",
                "size": "all",
            }
            button = filter_group_buttons(user_id, group, "all", "all", "all")
            await edit_message(
                message,
                filter_group_text(key, group, "all", "all", "all"),
                button,
            )
        else:
            endpoint = "Trending" if method == "apitrend" else "Recent"
            await edit_message(message, _listing_msg(group, endpoint))
            await search(key, group, message, method)
    elif data[2] == "gsz":
        await query.answer()
        state = await _need_state(message, user_id, "courses")
        if not state:
            return
        state["size"] = data[3] if len(data) > 3 else "all"
        button = filter_group_buttons(user_id, "courses", "all", "all", state["size"])
        await edit_message(
            message,
            filter_group_text(key, "courses", "all", "all", state["size"]),
            button,
        )
    elif data[2] == "gl":
        await query.answer()
        state = await _need_state(message, user_id, "books")
        if not state:
            return
        state["language"] = data[3] if len(data) > 3 else "all"
        format_ = data[4] if len(data) > 4 else state.get("format", "all")
        button = filter_group_buttons(
            user_id, "books", state["language"], format_, state.get("size", "all")
        )
        await edit_message(
            message,
            filter_group_text(
                key, "books", state["language"], format_, state.get("size", "all")
            ),
            button,
        )
    elif data[2] == "gf":
        await query.answer()
        state = await _need_state(message, user_id, "books")
        if not state:
            return
        language = data[3] if len(data) > 3 else state.get("language", "all")
        state["format"] = data[4] if len(data) > 4 else "all"
        button = filter_group_buttons(
            user_id, "books", language, state["format"], state.get("size", "all")
        )
        await edit_message(
            message,
            filter_group_text(
                key, "books", language, state["format"], state.get("size", "all")
            ),
            button,
        )
    elif data[2] == "ggo":
        await query.answer()
        state = await _need_state(message, user_id)
        if not state:
            return
        language = data[3] if len(data) > 3 else "all"
        format_ = data[4] if len(data) > 4 else "all"
        size = data[5] if len(data) > 5 else "all"
        site = state["site"]
        summary = " | ".join(
            p
            for p in (
                f"Language: {_filter_label(language, 'language')}" if language != "all" else "",
                f"Format: {_filter_label(format_, 'format')}" if format_ != "all" else "",
                f"Size: {_size_label(size)}" if size != "all" else "",
            )
            if p
        ) or "None"
        await edit_message(message, _searching_msg(key, site, f"\nFilters:- <i>{summary}</i>"))
        await search(key, site, message, "apisearch", "all", "all", language, format_, size)
        FILTER_STATE.pop(user_id, None)
    elif data[2] != "cancel":
        await query.answer()
        site = data[2]
        method = data[3]
        if method == "apisearch":
            if site == "all":
                button = api_categories(user_id, site)
                await edit_message(message, "Choose site:", button)
            elif site in BOOK_SITES:
                button = filter_format_buttons(user_id, site, "all")
                await edit_message(
                    message,
                    filter_format_text(key, site, "all"),
                    button,
                )
            else:
                await edit_message(message, _searching_msg(key, site))
                await search(key, site, message, "apisearch")
        elif method.startswith("api"):
            if key is None:
                if method == "apirecent":
                    endpoint = "Recent"
                elif method == "apitrend":
                    endpoint = "Trending"
                await edit_message(message, _listing_msg(site, endpoint))
                await search(key, site, message, method)
            else:
                await edit_message(message, _searching_msg(key, site))
                await search(key, site, message, method)
        else:
            await edit_message(
                message,
                f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{site.capitalize()}</i></b>",
            )
            await search(key, site, message, method)
    else:
        await query.answer()
        FILTER_STATE.pop(user_id, None)
        await edit_message(message, "Search has been canceled!")
