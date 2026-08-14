import re
from niquests import AsyncSession
from html import escape
from urllib.parse import quote
from pyrogram.enums import ButtonStyle

from .. import LOGGER
from ..core.config_manager import Config
from ..core.torrent_manager import TorrentManager
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import get_readable_file_size
from ..helper.ext_utils.telegraph_helper import telegraph
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import edit_message, send_message

def _dl_link(url, name, ext=""):
    """Build a download link for a result. Google Drive URLs are linked
    directly so WZML-X can resolve the Drive ID natively (its extractor
    fails on proxied drive URLs); everything else goes through the API
    proxy with a filename slug so browsers that ignore Content-Disposition
    still save the file with a real name, not "torrent_file.pdf"."""
    if "drive.usercontent.google.com" in url or "drive.google.com" in url:
        return url
    slug = re.sub(r"[^a-zA-Z0-9._-]", "_", str(name or "download"))[:80] or "download"
    slug = slug.strip("._-")
    if not re.search(r"\.[a-z0-9]{2,5}$", slug, re.I):
        if re.fullmatch(r"[a-z0-9]{2,5}", str(ext), re.I):
            slug = slug.rstrip("._-") + "." + str(ext).lower()
        else:
            m = re.search(
                r"(?<![a-z0-9])(pdf|epub|mobi|azw3|djvu|fb2|zip|rar|mp3|m4b|torrent)(?![a-z0-9])",
                slug,
                re.I,
            )
            slug = slug.rstrip("._-") + ("." + m.group(1).lower() if m else ".dl")
    return "{}/api/v1/torrent_file/{}?url={}&name={}".format(
        Config.SEARCH_API_LINK, quote(slug), quote(url), quote(str(name or ""))
    )


PLUGINS = []
SITES = None
SITE_STATUS = {}
TELEGRAPH_LIMIT = 9999999


async def _refresh_sites():
    """Fetch the enabled site list from the API so disabled sites disappear
    from the buttons without a bot restart."""
    global SITES, SITE_STATUS
    try:
        async with AsyncSession() as client:
            response = await client.get(f"{Config.SEARCH_API_LINK}/api/v1/sites")
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
                    f"{Config.SEARCH_API_LINK}/api/v1/sites/status"
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
                        s for s in SITES if s != "all"
                    )
                if group_sites:
                    api += f"&sites={quote(group_sites)}"
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
        api += _api_extra_params(opts, method)
        try:
            async with AsyncSession(timeout=60) as client:
                response = await client.get(api)
                search_results = response.json()
            if isinstance(search_results, dict):
                api_error = search_results.get("error") or search_results.get("detail")
                if api_error:
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
            msg = f"<b>Found {min(search_results['total'], TELEGRAPH_LIMIT)}</b>"
            if method == "apitrend":
                msg += f" <b>trending result(s)\nTorrent Site:- <i>{_site_display_name(site)}</i></b>"
            elif method == "apirecent":
                msg += (
                    f" <b>recent result(s)\nTorrent Site:- <i>{_site_display_name(site)}</i></b>"
                )
            else:
                msg += f" <b>result(s) for <i>{key}</i>\nTorrent Site:- <i>{_site_display_name(site)}</i></b>"
            if search_results.get("relaxed_filters"):
                msg += " <i>(filters relaxed)</i>"
            search_results = search_results["data"]
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
    buttons.url_button("🔎 VIEW", link, style=ButtonStyle.PRIMARY)
    button = buttons.build_menu(1)
    await edit_message(message, msg, button)


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
                            msg += "<a href='{}'>Direct Link</a>".format(
                                _dl_link(subres["torrent"], result.get("name") or "", subres.get("extension") or "")
                            )
                        if subres.get("torrent") and subres.get("magnet"):
                            msg += " | "
                        if subres.get("magnet"):
                            msg += "<b>Share Magnet to</b> "
                            msg += f"<a href='http://t.me/share/url?url={quote(subres['magnet'])}'>Telegram</a>"
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
                    if result.get("torrent"):
                        msg += "<a href='{}'>Direct Link</a>".format(
                            _dl_link(result["torrent"], result.get("name") or "", result.get("extension") or "")
                        )
                    if result.get("download"):
                        msg += " | <a href='{}'>Alt Link</a>".format(
                            _dl_link(result["download"], result.get("name") or "", result.get("extension") or "")
                        )
                    if result.get("torrent") and result.get("magnet"):
                        msg += " | "
                    if result.get("magnet"):
                        msg += "<b>Share Magnet to</b> "
                        msg += f"<a href='http://t.me/share/url?url={quote(result['magnet'])}'>Telegram</a>"
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


API_PAGE_SIZE = 18
SEARCH_CATEGORIES = ["all", "movies", "tv", "anime", "audiobook", "books", "courses", "apps", "games", "music"]

FILTER_QUALITY = ["all", "480", "720", "1080", "4k"]
FILTER_LANGUAGE = ["all", "hindi", "english", "tamil", "telugu", "dual"]
FILTER_FORMAT = ["all", "pdf", "epub", "mobi"]
FILTER_SIZES = ["all", "small", "medium", "large"]

# In-memory filter state (user_id -> dict) so the second filter page can use
# short callbacks (Telegram callback_data is limited to 64 bytes).
FILTER_STATE = {}

# Per-command search options set with /search -l <N> -s <S> -p <P> -f
# (keyed by (user_id, msg_id) because callbacks only carry the original
# command message id).
SEARCH_OPTS = {}


_DIGIT_FLAGS = {"-l": "limit", "-s": "seeders", "-p": "page"}
_WORD_FLAGS = {
    "-x": "include",
    "-g": "site",
    "-q": "quality",
    "-lng": "language",
    "-c": "category",
    "-z": "size",
    "-S": "sort",
    "-o": "order",
}
_FLAG_ONLY = {"-f": "fresh", "-du": "dedup", "--help": "help"}


def _parse_search_cmd(text):
    """Split '/search data science -l 15 -g 1337x,tgx -q 1080p -f -du' into
    ('data science', {'limit': 15, 'site': '1337x,tgx', 'quality': '1080p',
    'fresh': True, 'dedup': True}).

    Args are only recognised at the END, after the key - putting them
    before the key would be confusing."""
    parts = (text or "").split()
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
        break
    key = " ".join(parts[1 : i + 1]).strip()
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
    """Result limit: -l value if given, else SEARCH_LIMIT."""
    return _search_opts(message).get("limit") or Config.SEARCH_LIMIT


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


def _api_extra_params(opts, method):
    """Query-string params for the search API from command-line args."""
    params = []
    if opts.get("page"):
        params.append(f"page={opts['page']}")
    if method == "apisearch":
        if opts.get("seeders"):
            params.append(f"min_seeders={opts['seeders']}")
        if opts.get("fresh"):
            params.append("fresh=1")
        if opts.get("dedup"):
            params.append("dedup=1")
        if opts.get("include"):
            params.append(f"include={quote(opts['include'])}")
        if opts.get("quality"):
            params.append(f"quality={quote(opts['quality'])}")
        if opts.get("language"):
            params.append(f"language={quote(opts['language'])}")
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
    return ("&" + "&".join(params)) if params else ""


SEARCH_HELP_TEXT = (
    "<b>🔍 Torrent Search Args</b>\n\n"
    "Format: <code>/s &lt;key&gt; [args]</code> — args hamesha <b>key ke baad</b>\n\n"
    "• <code>-l &lt;n&gt;</code> → result limit\n"
    "• <code>-s &lt;n&gt;</code> → min seeders\n"
    "• <code>-p &lt;n&gt;</code> → page number\n"
    "• <code>-f</code> → fresh (cache skip)\n"
    "• <code>-du</code> → duplicate protection ON\n"
    "• <code>-x &lt;word&gt;</code> → sirf us word wale results\n"
    "• <code>-g &lt;site&gt;</code> → direct search, buttons skip\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Groups: <code>all</code> (17 sites), <code>books</code>, <code>courses</code>\n"
    "&nbsp;&nbsp;&nbsp;&nbsp;Multiple: <code>1337x,tgx,yts</code>\n"
    "• <code>-a</code> → SAB sites ek sath (29: general + books + courses)\n"
    "• <code>-q &lt;quality&gt;</code> → <code>480</code>, <code>720</code>, <code>1080</code>, <code>4k</code>\n"
    "• <code>-lng &lt;lang&gt;</code> → <code>hindi</code>, <code>english</code>, <code>tamil</code>, <code>telugu</code>, <code>dual</code>\n"
    "• <code>-c &lt;cat&gt;</code> → <code>movies</code>, <code>tv</code>, <code>music</code>, <code>anime</code>, <code>audiobook</code>, <code>course</code>, <code>book</code>, <code>game</code>, <code>app</code>\n"
    "• <code>-z &lt;size&gt;</code> → <code>&lt;1GB</code>, <code>&gt;3GB</code>, <code>1GB-3GB</code>\n"
    "• <code>-S &lt;sort&gt;</code> → <code>seeders</code>, <code>size</code>, <code>date</code>\n"
    "• <code>-o &lt;order&gt;</code> → <code>asc</code>, <code>desc</code>\n\n"
    "Examples:\n"
    "<code>/s oppenheimer -g 1337x,tgx -l 10 -f</code>\n"
    "<code>/s ikigai -g books -lng hindi -x pdf</code>\n"
    "<code>/s python -c course -z 1GB-3GB</code>"
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
            "Usage: /search <key> [-l <n>] [-s <n>] [-p <n>] [-f] [-du] [-x <w>] [-g <site>] [-a] [-q <q>] [-lng <l>] [-c <c>] [-z <size>] [-S <sort>] [-o <order>]\n/s --help for all args",
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
        site = data[3] if len(data) > 3 else "all"
        category = data[4] if len(data) > 4 else "all"
        quality = data[5] if len(data) > 5 else "all"
        language = data[6] if len(data) > 6 else "all"
        format_ = data[7] if len(data) > 7 else "all"
        button = filter_buttons(user_id, site, category, quality, language, format_)
        await edit_message(message, filter_menu_text(key, site, category, quality, language, format_), button)
    elif data[2] == "fmore":
        await query.answer()
        site = data[3] if len(data) > 3 else "all"
        category = data[4] if len(data) > 4 else "all"
        quality = data[5] if len(data) > 5 else "all"
        language = data[6] if len(data) > 6 else "all"
        format_ = data[7] if len(data) > 7 else "all"
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
        state = FILTER_STATE.get(user_id)
        if not state:
            button = await api_buttons(user_id, "apisearch")
            await edit_message(message, "Choose site:", button)
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
        await edit_message(
            message,
            f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{_site_display_name(site)}</i>{fmt}</b>",
        )
        await search(key, site, message, "apisearch", "all", "all", "all", format_)
    elif data[2] == "fgs":
        await query.answer()
        state = FILTER_STATE.get(user_id)
        if not state:
            button = await api_buttons(user_id, "apisearch")
            await edit_message(message, "Choose site:", button)
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
            f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{_site_display_name(site)}</i>\nCategory:- <i>{category.capitalize()}</i>\nFilters:- <i>{summary}</i></b>",
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
            await edit_message(
                message,
                f"<b>Listing {endpoint} Items...\nTorrent Site:- <i>{_site_display_name(group)}</i></b>",
            )
            await search(key, group, message, method)
    elif data[2] == "gsz":
        await query.answer()
        state = FILTER_STATE.get(user_id)
        if not state or state.get("site") != "courses":
            button = await api_buttons(user_id, "apisearch")
            await edit_message(message, "Choose site:", button)
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
        state = FILTER_STATE.get(user_id)
        if not state or state.get("site") != "books":
            button = await api_buttons(user_id, "apisearch")
            await edit_message(message, "Choose site:", button)
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
        state = FILTER_STATE.get(user_id)
        if not state or state.get("site") != "books":
            button = await api_buttons(user_id, "apisearch")
            await edit_message(message, "Choose site:", button)
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
        state = FILTER_STATE.get(user_id)
        if not state:
            button = await api_buttons(user_id, "apisearch")
            await edit_message(message, "Choose site:", button)
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
        await edit_message(
            message,
            f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{_site_display_name(site)}</i>\nFilters:- <i>{summary}</i></b>",
        )
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
                await edit_message(
                    message,
                    f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{_site_display_name(site)}</i></b>",
                )
                await search(key, site, message, "apisearch")
        elif method.startswith("api"):
            if key is None:
                if method == "apirecent":
                    endpoint = "Recent"
                elif method == "apitrend":
                    endpoint = "Trending"
                await edit_message(
                    message,
                    f"<b>Listing {endpoint} Items...\nTorrent Site:- <i>{_site_display_name(site)}</i></b>",
                )
                await search(key, site, message, method)
            else:
                await edit_message(
                    message,
                    f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{_site_display_name(site)}</i></b>",
                )
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
