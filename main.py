import discord
from discord import app_commands
from discord.ext import tasks
import os
import io
import asyncio
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote
import aiohttp
import asyncpg
from fpdf import FPDF
from dotenv import load_dotenv

load_dotenv()

TOKEN   = os.getenv('DISCORD_TOKEN')
MAL_ID  = os.getenv('MAL_CLIENT_ID')

DB_CONFIG = {
    'database': os.getenv('DB_NAME'),
    'user':     os.getenv('DB_USER'),
    'password': os.getenv('DB_PASS'),
    'host':     os.getenv('DB_HOST', 'localhost'),
    'port':     int(os.getenv('DB_PORT', '5432')),
}

MAX_TRACKED = 30

pool: asyncpg.Pool = None


async def init_db():
    global pool
    pool = await asyncpg.create_pool(**DB_CONFIG)
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tracking (
                user_id      BIGINT,
                manga_id     INTEGER,
                manga_title  TEXT,
                last_chapter INTEGER DEFAULT 0,
                last_updated TEXT    DEFAULT '',
                PRIMARY KEY (user_id, manga_id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                discord_id   BIGINT PRIMARY KEY,
                mal_username TEXT
            )
        ''')


def sanitize(text: str, max_len: int = 100) -> str:
    return text.strip().replace('@everyone', '').replace('@here', '')[:max_len]


MAL_SEARCH_LIMIT = 10
MAL_SEARCH_LIMIT_MEDIA = 30
MAL_USER_LIST_PAGE = 1000
MAL_USER_LIST_MAX_PAGES = 5

MAL_MANGA_SEARCH_FIELDS = (
    "id,title,alternative_titles,media_type,main_picture,synopsis,status,num_chapters,mean,"
    "authors{first_name,last_name}"
)
MAL_MANGA_TRACK_SEARCH_FIELDS = "id,title,alternative_titles,media_type,num_chapters,status,updated_at"
MAL_MANGA_DETAIL_FIELDS = (
    "id,title,alternative_titles,media_type,main_picture,synopsis,status,num_chapters,mean,"
    "authors{first_name,last_name},updated_at"
)

MAL_ANIME_SEARCH_FIELDS = (
    "id,title,alternative_titles,main_picture,synopsis,status,num_episodes,mean,studios"
)


def mal_headers() -> dict[str, str]:
    return {"X-MAL-CLIENT-ID": MAL_ID}


def _norm_title(s: str) -> str:
    s = unicodedata.normalize("NFKC", (s or "").strip())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    return " ".join(s.casefold().split())


def _titles_from_mal_node(node: dict) -> list[str]:
    out: list[str] = []
    t = node.get("title")
    if t:
        out.append(str(t))
    alt = node.get("alternative_titles")
    if isinstance(alt, dict):
        for key in ("en", "ja"):
            v = alt.get(key)
            if v:
                out.append(str(v))
        for syn in alt.get("synonyms") or []:
            if syn:
                out.append(str(syn))
    return out


def _title_similarity(query: str, title: str) -> float:
    nq, nt = _norm_title(query), _norm_title(title)
    if not nq or not nt:
        return 0.0
    if nq == nt:
        return 1.0
    if nq in nt or nt in nq:
        return 0.92
    return SequenceMatcher(None, nq, nt).ratio()


def pick_best_mal_node(nodes: list[dict], query: str) -> tuple[dict | None, bool, float]:
    """Pick the best-matching node for ``query``.

    Returns ``(node, fuzzy, score)`` where ``fuzzy`` is False on a normalized
    exact title/alt match and ``score`` is the best title similarity in ``[0, 1]``.
    """
    if not nodes:
        return None, False, 0.0
    qn = _norm_title(query)

    for node in nodes:
        for cand in _titles_from_mal_node(node):
            if _norm_title(cand) == qn:
                return node, False, 1.0

    ranked: list[tuple[float, int, dict]] = []
    for i, node in enumerate(nodes):
        score = max(
            (_title_similarity(query, c) for c in _titles_from_mal_node(node)),
            default=0.0,
        )
        ranked.append((score, -i, node))
    ranked.sort(reverse=True)
    best_score, _, best_node = ranked[0]
    return best_node, True, best_score


def _filter_nodes_media_type(nodes: list[dict], media: str | None) -> list[dict]:
    if not media or media == "any":
        return nodes
    want = media.lower()
    out = [n for n in nodes if str(n.get("media_type", "")).lower() == want]
    return out


def _pick_manga_with_media(
    nodes: list[dict], query: str, media: str | None
) -> tuple[dict | None, bool, float, bool]:
    """Like ``pick_best_mal_node`` but respects ``media`` (manhwa/manhua).

    Returns ``(node, fuzzy, score, used_fallback)`` where ``used_fallback`` is
    True when no rows matched the media filter and the pick used the unfiltered list.
    """
    filtered = _filter_nodes_media_type(nodes, media)
    if filtered:
        n, fz, sc = pick_best_mal_node(filtered, query)
        return n, fz, sc, False
    if media and media != "any":
        n, fz, sc = pick_best_mal_node(nodes, query)
        return n, fz, sc, True
    n, fz, sc = pick_best_mal_node(nodes, query)
    return n, fz, sc, False


async def mal_fetch_json(session: aiohttp.ClientSession, url: str) -> dict | None:
    async with session.get(url, headers=mal_headers()) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def mal_fetch_manga_detail(session: aiohttp.ClientSession, manga_id: int) -> dict | None:
    url = (
        f"https://api.myanimelist.net/v2/manga/{manga_id}"
        f"?fields={MAL_MANGA_DETAIL_FIELDS}"
    )
    return await mal_fetch_json(session, url)


MAL_ANIME_DETAIL_FIELDS = (
    "id,title,alternative_titles,main_picture,synopsis,status,num_episodes,mean,studios"
)


async def mal_fetch_anime_detail(session: aiohttp.ClientSession, anime_id: int) -> dict | None:
    url = f"https://api.myanimelist.net/v2/anime/{anime_id}?fields={MAL_ANIME_DETAIL_FIELDS}"
    return await mal_fetch_json(session, url)


async def mal_paginate_user_manga_nodes(session: aiohttp.ClientSession, username: str) -> list[dict]:
    user_seg = quote(username, safe="-_.~")
    field_sets = (
        "id,title,alternative_titles,media_type",
        "id,title,alternative_titles",
        "list_status",
    )
    for fields in field_sets:
        chunk: list[dict] = []
        url = (
            f"https://api.myanimelist.net/v2/users/{user_seg}/mangalist"
            f"?nsfw=true&limit={MAL_USER_LIST_PAGE}&offset=0&fields={fields}"
        )
        pages = 0
        ok = False
        while url and pages < MAL_USER_LIST_MAX_PAGES:
            data = await mal_fetch_json(session, url)
            if data is None:
                break
            ok = True
            for item in data.get("data") or []:
                n = item.get("node")
                if n and isinstance(n, dict):
                    chunk.append(n)
            url = (data.get("paging") or {}).get("next")
            pages += 1
            if url:
                await asyncio.sleep(0.2)
        if ok:
            return chunk
    return []


async def mal_paginate_user_anime_nodes(session: aiohttp.ClientSession, username: str) -> list[dict]:
    user_seg = quote(username, safe="-_.~")
    field_sets = (
        "id,title,alternative_titles",
        "list_status",
    )
    for fields in field_sets:
        chunk: list[dict] = []
        url = (
            f"https://api.myanimelist.net/v2/users/{user_seg}/animelist"
            f"?nsfw=true&limit={MAL_USER_LIST_PAGE}&offset=0&fields={fields}"
        )
        pages = 0
        ok = False
        while url and pages < MAL_USER_LIST_MAX_PAGES:
            data = await mal_fetch_json(session, url)
            if data is None:
                break
            ok = True
            for item in data.get("data") or []:
                n = item.get("node")
                if n and isinstance(n, dict):
                    chunk.append(n)
            url = (data.get("paging") or {}).get("next")
            pages += 1
            if url:
                await asyncio.sleep(0.2)
        if ok:
            return chunk
    return []


def resolve_manga_choice(
    list_node: dict | None,
    list_fuzzy: bool,
    list_score: float,
    search_node: dict | None,
    search_fuzzy: bool,
    search_score: float,
) -> tuple[dict | None, bool, str]:
    """Prefer MAL search exact match, then list exact, then stronger fuzzy.

    Returns ``(node, fuzzy, source)`` where ``source`` is ``search``, ``list``, or ``none``.
    """
    if search_node is not None and not search_fuzzy:
        return search_node, False, "search"
    if list_node is not None and not list_fuzzy:
        return list_node, False, "list"
    if list_node is not None and list_score > search_score + 0.05:
        return list_node, list_fuzzy, "list"
    if search_node is not None:
        return search_node, search_fuzzy, "search"
    if list_node is not None:
        return list_node, list_fuzzy, "list"
    return None, False, "none"


STATUS_MAP = {
    'currently_publishing': 'Ongoing',
    'finished':             'Finished',
    'not_yet_published':    'Not Yet Published',
    'on_hiatus':            'On Hiatus',
    'discontinued':         'Discontinued',
}

ANIME_STATUS_MAP = {
    'currently_airing':  'Airing',
    'finished_airing':   'Finished Airing',
    'not_yet_aired':     'Not Yet Aired',
}


class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await init_db()
        await self.tree.sync()
        self.check_for_updates.start()

    @tasks.loop(hours=6)
    async def check_for_updates(self):
        print("Checking for updates...")
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, manga_id, manga_title, last_chapter, last_updated FROM tracking"
            )

        async with aiohttp.ClientSession() as session:
            for row in rows:
                user_id      = row['user_id']
                m_id         = row['manga_id']
                m_title      = row['manga_title']
                last_ch      = row['last_chapter']
                last_updated = row['last_updated']

                url     = f'https://api.myanimelist.net/v2/manga/{m_id}?fields=num_chapters,status,updated_at'
                headers = {'X-MAL-CLIENT-ID': MAL_ID}

                try:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()

                    new_ch      = data.get('num_chapters', 0)
                    status      = data.get('status', '')
                    new_updated = data.get('updated_at', '')

                    notify_msg = None

                    if new_ch > 0 and new_ch > last_ch:
                        notify_msg = (
                            f"**Update!** **{m_title}** now has **{new_ch}** chapters listed on MAL."
                        )
                    elif status == 'currently_publishing' and new_updated and new_updated != last_updated:
                        notify_msg = (
                            f"**{m_title}** was updated on MyAnimeList! "
                            f"Check it out: https://myanimelist.net/manga/{m_id}"
                        )

                    if notify_msg:
                        try:
                            user = await self.fetch_user(user_id)
                            await user.send(notify_msg)
                        except Exception as e:
                            print(f"Could not DM user {user_id}: {e}")

                        async with pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE tracking SET last_chapter = $1, last_updated = $2 "
                                "WHERE user_id = $3 AND manga_id = $4",
                                new_ch, new_updated, user_id, m_id
                            )

                except Exception as e:
                    print(f"Error on manga {m_id}: {e}")

                await asyncio.sleep(1)

        print("Update check done.")

    @check_for_updates.before_loop
    async def before_check(self):
        await self.wait_until_ready()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")


bot = MyBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"Slow down. Try again in **{error.retry_after:.1f}s**.", ephemeral=True
        )
    else:
        try:
            await interaction.response.send_message("Something went wrong. Try again later.", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send("Something went wrong. Try again later.", ephemeral=True)
        print(f"Command error: {error}")


@bot.tree.command(name="help", description="List all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="MAL Tracker",
        description="Search manga & anime on MyAnimeList, track manga, and get DM'd when something updates.",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="/user <username>",
        value="Link your MAL username (used for PDF export + smarter manga/anime matching).",
        inline=False,
    )
    embed.add_field(
        name="/manga <title>",
        value="Search MAL; optional **media** (manhwa/manhua). Uses your manga list when linked.",
        inline=False,
    )
    embed.add_field(
        name="/anime <title>",
        value="Search MAL; uses your anime list when linked for pasted list titles.",
        inline=False,
    )
    embed.add_field(
        name="/track <title>",
        value=f"Track manga (max {MAX_TRACKED}); optional manhwa/manhua filter like `/manga`.",
        inline=False,
    )
    embed.add_field(name="/untrack <title>",  value="Stop tracking a manga (autocomplete).",         inline=False)
    embed.add_field(name="/list",             value="See your full tracking list.",                  inline=False)
    embed.add_field(name="/export",           value="Export your tracking list as a PDF.",           inline=False)
    embed.set_footer(text="Updates are checked every 6 hours.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="user", description="Link your MAL username for notifications")
@app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
async def set_user(interaction: discord.Interaction, username: str):
    username = sanitize(username, 50)
    if not username:
        await interaction.response.send_message("That username doesn't look right.", ephemeral=True)
        return

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (discord_id, mal_username) VALUES ($1, $2)
               ON CONFLICT (discord_id) DO UPDATE SET mal_username = EXCLUDED.mal_username""",
            interaction.user.id, username
        )
    await interaction.response.send_message(f"✅ MAL username set to **{username}**.")


@bot.tree.command(name="manga", description="Search for a manga on MAL")
@app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
@app_commands.describe(
    title="Title to search (paste from your MAL list works best if you use /user)",
    media="Optional: restrict to manhwa or manhua (MAL media_type)",
)
@app_commands.choices(media=[
    app_commands.Choice(name="Any", value="any"),
    app_commands.Choice(name="Manhwa", value="manhwa"),
    app_commands.Choice(name="Manhua", value="manhua"),
])
async def manga(interaction: discord.Interaction, title: str, media: str | None = None):
    title = sanitize(title)
    if not title:
        await interaction.response.send_message("Give me an actual title.", ephemeral=True)
        return

    await interaction.response.defer()

    kind = (media or "any").lower()
    lim  = MAL_SEARCH_LIMIT_MEDIA if kind != "any" else MAL_SEARCH_LIMIT
    search_url = (
        f"https://api.myanimelist.net/v2/manga?q={quote(title)}&limit={lim}"
        f"&fields={MAL_MANGA_SEARCH_FIELDS}"
    )

    mal_user: str | None = None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT mal_username FROM users WHERE discord_id = $1", interaction.user.id
        )
        if row and row["mal_username"]:
            mal_user = row["mal_username"]

    async with aiohttp.ClientSession() as session:
        search_task = asyncio.create_task(mal_fetch_json(session, search_url))
        list_task = (
            asyncio.create_task(mal_paginate_user_manga_nodes(session, mal_user))
            if mal_user
            else None
        )
        search_data = await search_task
        list_nodes = await list_task if list_task else []

        if search_data is None:
            await interaction.followup.send("MAL API is not responding right now. Try again later.")
            return

        raw = search_data.get("data") or []
        search_nodes = [item["node"] for item in raw if item.get("node")]

        ln, lf, ls, l_fb = _pick_manga_with_media(list_nodes, title, kind)
        sn, sf, ss, s_fb = _pick_manga_with_media(search_nodes, title, kind)

        node, _, src = resolve_manga_choice(ln, lf, ls, sn, sf, ss)
        if node is None:
            await interaction.followup.send("Couldn't find that manga on MAL.")
            return

        if src == "list" or not node.get("synopsis"):
            detail = await mal_fetch_manga_detail(session, int(node["id"]))
            if detail:
                node = detail

    status = STATUS_MAP.get(node.get("status", ""), "Unknown")
    chapters = node.get("num_chapters", 0)
    ch_text  = str(chapters) if chapters > 0 else "Ongoing"
    synopsis = node.get("synopsis", "No synopsis available.")
    score    = node.get("mean")
    score_text = f"{score}/10" if score else "N/A"

    authors = node.get("authors", [])
    author_names = []
    for a in authors:
        person = a.get("node", {})
        first  = person.get("first_name", "")
        last   = person.get("last_name", "")
        name   = f"{first} {last}".strip()
        if name:
            author_names.append(name)
    author_text = ", ".join(author_names[:3]) if author_names else "Unknown"

    mt = str(node.get("media_type", "") or "manga").replace("_", " ").title()
    embed = discord.Embed(
        title=node["title"],
        url=f"https://myanimelist.net/manga/{node['id']}",
        color=discord.Color.blue(),
    )
    if "main_picture" in node:
        embed.set_thumbnail(url=node["main_picture"]["large"])

    embed.add_field(
        name="Synopsis",
        value=synopsis[:300] + ("..." if len(synopsis) > 300 else ""),
        inline=False,
    )
    embed.add_field(name="Type",     value=mt,          inline=True)
    embed.add_field(name="Status",   value=status,      inline=True)
    embed.add_field(name="Chapters", value=ch_text,     inline=True)
    embed.add_field(name="MAL Score", value=score_text, inline=True)
    embed.add_field(name="Author",   value=author_text, inline=True)

    fuzzy = not any(_norm_title(c) == _norm_title(title) for c in _titles_from_mal_node(node))
    foot: list[str] = []
    if src == "list":
        foot.append("Matched using your linked MAL manga list (`/user`).")
    if kind != "any" and (
        str(node.get("media_type", "")).lower() != kind or s_fb or l_fb
    ):
        foot.append("Manhwa/manhua filter: no strict-only hit — showing closest title.")
    if fuzzy and raw and raw[0].get("node", {}).get("id") != node.get("id"):
        foot.append("Not MAL search #1 — picked closest title among results.")

    if foot:
        embed.set_footer(text=" ".join(foot))

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="anime", description="Search for an anime on MAL")
@app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
@app_commands.describe(
    title="Title to search (paste from your MAL list works best if you use /user)",
)
async def anime(interaction: discord.Interaction, title: str):
    title = sanitize(title)
    if not title:
        await interaction.response.send_message("Give me an actual title.", ephemeral=True)
        return

    await interaction.response.defer()

    search_url = (
        f"https://api.myanimelist.net/v2/anime?q={quote(title)}&limit={MAL_SEARCH_LIMIT}"
        f"&fields={MAL_ANIME_SEARCH_FIELDS}"
    )

    mal_user: str | None = None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT mal_username FROM users WHERE discord_id = $1", interaction.user.id
        )
        if row and row["mal_username"]:
            mal_user = row["mal_username"]

    async with aiohttp.ClientSession() as session:
        search_task = asyncio.create_task(mal_fetch_json(session, search_url))
        list_task = (
            asyncio.create_task(mal_paginate_user_anime_nodes(session, mal_user))
            if mal_user
            else None
        )
        search_data = await search_task
        list_nodes = await list_task if list_task else []

        if search_data is None:
            await interaction.followup.send("MAL API is not responding right now. Try again later.")
            return

        raw = search_data.get("data") or []
        search_nodes = [item["node"] for item in raw if item.get("node")]

        ln, lf, ls = pick_best_mal_node(list_nodes, title)
        sn, sf, ss = pick_best_mal_node(search_nodes, title)

        node, _, src = resolve_manga_choice(ln, lf, ls, sn, sf, ss)
        if node is None:
            await interaction.followup.send("Couldn't find that anime on MAL.")
            return

        if src == "list" or not node.get("synopsis"):
            detail = await mal_fetch_anime_detail(session, int(node["id"]))
            if detail:
                node = detail

    status   = ANIME_STATUS_MAP.get(node.get("status", ""), "Unknown")
    episodes = node.get("num_episodes", 0)
    ep_text  = str(episodes) if episodes > 0 else "Ongoing"
    synopsis = node.get("synopsis", "No synopsis available.")
    score    = node.get("mean")
    score_text = f"{score}/10" if score else "N/A"

    studios = node.get("studios", [])
    studio_names = [s.get("name", "") for s in studios if s.get("name")]
    studio_text  = ", ".join(studio_names[:3]) if studio_names else "Unknown"

    embed = discord.Embed(
        title=node["title"],
        url=f"https://myanimelist.net/anime/{node['id']}",
        color=discord.Color.red(),
    )
    if "main_picture" in node:
        embed.set_thumbnail(url=node["main_picture"]["large"])

    embed.add_field(
        name="Synopsis",
        value=synopsis[:300] + ("..." if len(synopsis) > 300 else ""),
        inline=False,
    )
    embed.add_field(name="Status",    value=status,      inline=True)
    embed.add_field(name="Episodes",  value=ep_text,     inline=True)
    embed.add_field(name="MAL Score", value=score_text,  inline=True)
    embed.add_field(name="Studio",    value=studio_text, inline=True)

    fuzzy = not any(_norm_title(c) == _norm_title(title) for c in _titles_from_mal_node(node))
    foot: list[str] = []
    if src == "list":
        foot.append("Matched using your linked MAL anime list (`/user`).")
    if fuzzy and raw and raw[0].get("node", {}).get("id") != node.get("id"):
        foot.append("Not MAL search #1 — picked closest title among results.")
    if foot:
        embed.set_footer(text=" ".join(foot))

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="track", description="Track a manga for chapter updates")
@app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
@app_commands.describe(
    title="Title to track (optional manhwa/manhua filter; uses your MAL list if linked)",
    media="Optional: restrict to manhwa or manhua (MAL media_type)",
)
@app_commands.choices(media=[
    app_commands.Choice(name="Any", value="any"),
    app_commands.Choice(name="Manhwa", value="manhwa"),
    app_commands.Choice(name="Manhua", value="manhua"),
])
async def track(interaction: discord.Interaction, title: str, media: str | None = None):
    title = sanitize(title)
    if not title:
        await interaction.response.send_message("Give me a title.", ephemeral=True)
        return

    await interaction.response.defer()

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM tracking WHERE user_id = $1", interaction.user.id
        )
        user_row = await conn.fetchrow(
            "SELECT mal_username FROM users WHERE discord_id = $1", interaction.user.id
        )

    if count >= MAX_TRACKED:
        await interaction.followup.send(
            f"You're already at the {MAX_TRACKED} manga limit. Use `/untrack` to free up a slot."
        )
        return

    kind = (media or "any").lower()
    lim  = MAL_SEARCH_LIMIT_MEDIA if kind != "any" else MAL_SEARCH_LIMIT
    search_url = (
        f"https://api.myanimelist.net/v2/manga?q={quote(title)}&limit={lim}"
        f"&fields={MAL_MANGA_TRACK_SEARCH_FIELDS}"
    )

    mal_user = user_row["mal_username"] if user_row and user_row["mal_username"] else None

    async with aiohttp.ClientSession() as session:
        search_task = asyncio.create_task(mal_fetch_json(session, search_url))
        list_task = (
            asyncio.create_task(mal_paginate_user_manga_nodes(session, mal_user))
            if mal_user
            else None
        )
        search_data = await search_task
        list_nodes = await list_task if list_task else []

        if search_data is None:
            await interaction.followup.send("MAL API is not responding right now.")
            return

        raw = search_data.get("data") or []
        search_nodes = [item["node"] for item in raw if item.get("node")]

        ln, lf, ls, _ = _pick_manga_with_media(list_nodes, title, kind)
        sn, sf, ss, _ = _pick_manga_with_media(search_nodes, title, kind)

        node, _, src = resolve_manga_choice(ln, lf, ls, sn, sf, ss)
        if node is None:
            await interaction.followup.send("Couldn't find that manga on MAL.")
            return

        if not node.get("updated_at") or node.get("num_chapters") is None:
            detail = await mal_fetch_manga_detail(session, int(node["id"]))
            if detail:
                node = detail

    async with pool.acquire() as conn:
        result = await conn.execute(
            """INSERT INTO tracking (user_id, manga_id, manga_title, last_chapter, last_updated)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id, manga_id) DO NOTHING""",
            interaction.user.id,
            node["id"],
            node["title"],
            node.get("num_chapters", 0),
            node.get("updated_at", ""),
        )

    fuzzy = not any(_norm_title(c) == _norm_title(title) for c in _titles_from_mal_node(node))
    hint_parts: list[str] = []
    if src == "list":
        hint_parts.append("Resolved via your linked MAL manga list (`/user`).")
    if fuzzy and raw and raw[0].get("node", {}).get("id") != node.get("id"):
        hint_parts.append("Closest match; MAL's first search hit was different.")
    hint = (" (" + " ".join(hint_parts) + ")") if hint_parts else ""

    if result == "INSERT 0 0":
        await interaction.followup.send(f"You're already tracking **{node['title']}**.{hint}")
    else:
        await interaction.followup.send(
            f"✅ Now tracking **{node['title']}**! I'll DM you when it updates.{hint}"
        )


async def untrack_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT manga_title FROM tracking WHERE user_id = $1 ORDER BY manga_title",
            interaction.user.id
        )
    choices = []
    for row in rows:
        t = row['manga_title']
        if current.lower() in t.lower():
            choices.append(app_commands.Choice(name=t[:100], value=t))
        if len(choices) >= 25:
            break
    return choices


@bot.tree.command(name="untrack", description="Stop tracking a manga")
@app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
@app_commands.autocomplete(title=untrack_autocomplete)
async def untrack(interaction: discord.Interaction, title: str):
    title = sanitize(title)
    if not title:
        await interaction.response.send_message("Give me a title.", ephemeral=True)
        return

    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM tracking WHERE user_id = $1 AND LOWER(manga_title) = LOWER($2)",
            interaction.user.id, title
        )

    if result == "DELETE 0":
        await interaction.response.send_message(
            f"Couldn't find **{title}** in your list. Use `/list` to see what you're tracking.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(f"✅ Stopped tracking **{title}**.")


@bot.tree.command(name="list", description="Show your tracked manga")
@app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
async def list_tracked(interaction: discord.Interaction):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT manga_title, last_chapter FROM tracking WHERE user_id = $1 ORDER BY manga_title",
            interaction.user.id
        )

    if not rows:
        await interaction.response.send_message(
            "You're not tracking anything yet. Use `/track` to get started."
        )
        return

    lines = []
    for r in rows:
        ch      = r['last_chapter']
        ch_text = f"{ch} ch" if ch > 0 else "Ongoing"
        lines.append(f"• **{r['manga_title']}** ({ch_text})")

    embed = discord.Embed(
        title="Your Tracked Manga",
        description="\n".join(lines),
        color=discord.Color.green()
    )
    embed.set_footer(text=f"{len(rows)}/{MAX_TRACKED} slots used")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="export", description="Export your tracked list as a PDF")
@app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
async def export_pdf(interaction: discord.Interaction):
    await interaction.response.defer()

    async with pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT mal_username FROM users WHERE discord_id = $1", interaction.user.id
        )
        if not user_row:
            await interaction.followup.send("Set your MAL username first with `/user`.")
            return

        rows = await conn.fetch(
            "SELECT manga_title, last_chapter FROM tracking WHERE user_id = $1 ORDER BY manga_title",
            interaction.user.id
        )

    if not rows:
        await interaction.followup.send("Nothing to export, your list is empty.")
        return

    mal_user = user_row['mal_username']

    try:
        pdf = FPDF()
        pdf.add_page()

        pdf.set_font("Arial", "B", 18)
        pdf.cell(0, 12, f"MAL Manga List: {mal_user}", ln=True)
        pdf.set_font("Arial", "", 10)
        pdf.cell(0, 8, f"Total tracked: {len(rows)}/{MAX_TRACKED}", ln=True)
        pdf.ln(4)

        pdf.set_font("Arial", "B", 12)
        pdf.set_fill_color(230, 230, 230)
        pdf.cell(130, 10, "Manga Title", border=1, fill=True)
        pdf.cell(50,  10, "Chapters",   border=1, fill=True)
        pdf.ln()

        pdf.set_font("Arial", "", 11)
        for t, chapter in rows:
            ch_text       = str(chapter) if chapter > 0 else "Ongoing"
            display_title = str(t)[:50] + ("..." if len(str(t)) > 50 else "")
            pdf.cell(130, 9, display_title, border=1)
            pdf.cell(50,  9, ch_text,       border=1)
            pdf.ln()

        buf = io.BytesIO(pdf.output())
        buf.seek(0)
        file = discord.File(fp=buf, filename=f"{mal_user}_manga_list.pdf")
        await interaction.followup.send("Here's your exported list!", file=file)

    except Exception as e:
        await interaction.followup.send("Something went wrong generating the PDF.")
        print(f"PDF error: {e}")


bot.run(TOKEN)