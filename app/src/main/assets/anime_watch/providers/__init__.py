import concurrent.futures
import logging
import re
from typing import Optional
from anime_watch.models import Site, SearchResult, Episode, StreamSource, TorrentResult
from anime_watch.core import (
    fetch_episodes_generic,
    scrape_page_for_video, extract_with_ytdlp
)
from .anidb import AniDBProvider
from .anikoto import AnikotoProvider
from .tryembed import TryEmbedProvider
from .anineko import AniNekoProvider
from .anizone import AniZoneProvider
from .bingr import BingrProvider
from .fmovies import FmoviesProvider
from .netmirror import NetMirrorProvider
from .moviebox import MovieBoxProvider
from .streamingunity import StreamingUnityProvider
from .tmdb_search import TMDbProvider
from .torrentprovider import TPBProvider, EZTVProvider, NyaaProvider
from .loader import discover_providers

logger = logging.getLogger(__name__)

ANIME_SITES = [
    Site(name="AniDB", slug="anidb", url="https://anidb.app", rank=1, category="anime"),
    Site(name="Anikoto", slug="anikoto", url="https://anikototv.to", rank=2, category="anime"),
    Site(name="TryEmbed", slug="tryembed", url="https://tryembed.us.cc", rank=3, category="anime"),
    Site(name="AniNeko", slug="anineko", url="https://anineko.to", rank=4, category="anime"),
    Site(name="AniZone", slug="anizone", url="https://anizone.to", rank=5, category="anime"),
]
ANIME_PROVIDERS = {
    "anidb": AniDBProvider(),
    "anikoto": AnikotoProvider(),
    "tryembed": TryEmbedProvider(),
    "anineko": AniNekoProvider(),
    "anizone": AniZoneProvider(),
}

MOVIE_SITES: list[Site] = [
    Site(name="Bingr", slug="bingr", url="https://bingr.one", rank=1, category="movies"),
    Site(name="Fmovies", slug="fmovies", url="https://www.fmovies.gd", rank=2, category="movies"),
    Site(name="StreamingUnity", slug="streamingunity", url="https://streamingunity.dog", rank=3, category="movies"),
    Site(name="MovieBox", slug="moviebox", url="https://moviebox.app", rank=4, category="movies"),
    Site(name="NetMirror", slug="netmirror", url="https://net77.cc", rank=5, category="movies"),
]
MOVIE_PROVIDERS: dict[str, "BaseProvider"] = {
    "bingr": BingrProvider(),
    "fmovies": FmoviesProvider(),
    "streamingunity": StreamingUnityProvider(),
    "moviebox": MovieBoxProvider(),
    "netmirror": NetMirrorProvider(),
    "tmdb": TMDbProvider(),
}

TORRENT_SITES: list[Site] = [
    Site(name="TPB", slug="tpb", url="https://thepiratebay.org", rank=1, category="torrent"),
    Site(name="EZTV", slug="eztv", url="https://eztvx.to", rank=2, category="torrent"),
    Site(name="Nyaa", slug="nyaa", url="https://nyaa.si", rank=3, category="torrent"),
]
TORRENT_PROVIDERS: dict[str, TPBProvider | EZTVProvider | NyaaProvider] = {
    "tpb": TPBProvider(),
    "eztv": EZTVProvider(),
    "nyaa": NyaaProvider(),
}

CONFIGURED_SITES = ANIME_SITES + MOVIE_SITES + TORRENT_SITES
CONFIGURED_PROVIDERS = {**ANIME_PROVIDERS, **MOVIE_PROVIDERS}

# Merge user-provided plugins
_plugins = discover_providers()
for _key, (_provider, _site) in _plugins.items():
    if _key in CONFIGURED_PROVIDERS:
        logger.info("Plugin %r overrides built-in provider, keeping built-in", _key)
        continue
    CONFIGURED_PROVIDERS[_key] = _provider
    CONFIGURED_SITES.append(_site)
    cat = _site.category
    if cat == "anime":
        ANIME_SITES.append(_site)
        ANIME_PROVIDERS[_key] = _provider
    elif cat == "movies":
        MOVIE_SITES.append(_site)
        MOVIE_PROVIDERS[_key] = _provider

_TARGET_PROVIDER: str = ""

def set_target_provider(slug: str):
    global _TARGET_PROVIDER
    _TARGET_PROVIDER = slug

def get_target_provider() -> str:
    return _TARGET_PROVIDER

def _search_torrents(providers: dict, query: str, on_progress=None) -> list:
    all_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        fmap = {}
        for slug, provider in providers.items():
            if on_progress:
                on_progress(slug.upper(), "searching")
            fmap[executor.submit(provider.search, query)] = slug
        done, not_done = concurrent.futures.wait(fmap, timeout=20)
        for f in done:
            slug = fmap[f]
            try:
                r = f.result()
                if r:
                    all_results.extend(r)
                if on_progress:
                    on_progress(slug.upper(), "done")
            except Exception:
                if on_progress:
                    on_progress(slug.upper(), "timeout")
        for f in not_done:
            slug = fmap[f]
            if on_progress:
                on_progress(slug.upper(), "timeout")
    seen = {}
    for r in all_results:
        if r.info_hash not in seen or r.seeders > seen[r.info_hash].seeders:
            seen[r.info_hash] = r
    return sorted(seen.values(), key=lambda r: r.seeders, reverse=True)


def search_configured(query: str, on_progress=None, category: str = "", provider: str = "") -> list:
    if category == "torrent-movies":
        return _search_torrents({"tpb": TORRENT_PROVIDERS["tpb"], "eztv": TORRENT_PROVIDERS["eztv"]}, query, on_progress)
    elif category == "torrent-anime":
        return _search_torrents({"nyaa": TORRENT_PROVIDERS["nyaa"]}, query, on_progress)
    elif category == "torrent":
        return _search_torrents(TORRENT_PROVIDERS, query, on_progress)

    if not provider:
        provider = _TARGET_PROVIDER
    if provider:
        prov = CONFIGURED_PROVIDERS.get(provider)
        if not prov:
            return []
        if on_progress:
            on_progress(provider, "searching")
        try:
            r = prov.search(query)
            if r:
                if on_progress:
                    on_progress(provider, "done")
                return r
        except Exception:
            if on_progress:
                on_progress(provider, "error")
        return []

    all_results = []
    sites = [s for s in CONFIGURED_SITES if not category or s.category == category]
    if not sites:
        return all_results
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        fmap = {}
        for site in sites:
            provider = CONFIGURED_PROVIDERS.get(site.slug)
            if provider:
                if on_progress:
                    on_progress(site.name, "searching")
                fmap[executor.submit(provider.search, query)] = site.name
        for f in concurrent.futures.as_completed(fmap, timeout=25):
            name = fmap[f]
            try:
                r = f.result(timeout=10)
                if r:
                    all_results.extend(r)
                if on_progress:
                    on_progress(name, "done")
            except concurrent.futures.TimeoutError:
                if on_progress:
                    on_progress(name, "timeout")
            except Exception as e:
                if on_progress:
                    on_progress(name, f"error")
    return all_results

PAGE_SIZE = 100


def _category_sort_key(c: str) -> tuple:
    m = re.match(r'Season (\d+)', c)
    if m:
        return (0, int(m.group(1)), _page_start(c))
    if c == "Specials":
        return (2, 0, 0)
    start = _page_start(c)
    if start:
        return (1, 0, start)
    return (1, c, 0)


def _page_start(c: str) -> int:
    m = re.search(r'\((\d+)-', c)
    if m:
        return int(m.group(1))
    return 0


def _season_label(season) -> str:
    s = season
    if isinstance(s, int):
        return f"Season {s}"
    s = str(s).strip()
    if s.lower().startswith("season "):
        return s
    return f"Season {s}"


def _categorize_episodes(episodes: list[Episode]) -> list[Episode]:
    for ep in episodes:
        if not ep.category:
            season = ep.data.get("season")
            if season is not None:
                ep.category = _season_label(season)
    return episodes


def _paginate_episodes(episodes: list[Episode], page_size: int = PAGE_SIZE) -> list[Episode]:
    cats = {}
    order = []
    for ep in episodes:
        c = ep.category or "All"
        if c not in cats:
            cats[c] = []
            order.append(c)
        cats[c].append(ep)
    order.sort(key=_category_sort_key)
    result = []
    for c in order:
        group = cats[c]
        if len(group) > page_size:
            for start in range(0, len(group), page_size):
                chunk = group[start:start + page_size]
                first_num = chunk[0].number
                last_num = chunk[-1].number
                page_cat = f"{c} ({first_num}-{last_num})"
                for ep in chunk:
                    ep.category = page_cat
                result.extend(chunk)
        else:
            result.extend(group)
    return result


def get_episodes(result: SearchResult) -> list[Episode]:
    key = result.site_name.lower().strip()
    provider = CONFIGURED_PROVIDERS.get(key)
    if provider:
        eps = provider.get_episodes(result)
    else:
        an = result.title.split(" (")[0].strip()
        em = re.search(r"/(?:ep(?:-|isode/))(\d+)", result.url)
        if em:
            eps = [Episode(title=f"{result.title} - Episode {em.group(1)}", url=result.url, number=em.group(1), site_name=result.site_name, anime_name=an)]
        else:
            eps = fetch_episodes_generic(result.url, result.site_name)
            if not eps:
                eps = [Episode(title=f"{result.title} - Play Now", url=result.url, number="1", site_name=result.site_name, anime_name=an)]
            for e in eps:
                e.anime_name = an
    eps = _categorize_episodes(eps)
    eps = _paginate_episodes(eps)
    return eps

def extract_stream(episode: Episode, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
    key = episode.site_name.lower().strip()
    provider = CONFIGURED_PROVIDERS.get(key)
    if provider:
        return provider.extract_stream(episode, audio_pref, quality_pref)
        
    stream = scrape_page_for_video(episode.url, episode.site_name)
    if not stream or not stream.url: stream = extract_with_ytdlp(episode.url)
    return stream


