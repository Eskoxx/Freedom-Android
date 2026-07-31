"""
Site Discovery Tool — Anime Watch Plugin Auto-Discovery
=========================================================
Analyzes a streaming site purely through code by trying ALL known
patterns from every configured provider. Discovers search, episode,
stream, subtitle, and server patterns automatically.

Usage:
    python -m anime_watch plugin discover https://mysite.com
    python -m anime_watch plugin discover https://mysite.com --query "naruto"
    python -m anime_watch plugin discover https://mysite.com --output config.json
    python -m anime_watch plugin discover https://mysite.com --test

To add new patterns (upgradeability):
    - Add a SearchPattern to SEARCH_PATTERNS
    - Add a CSS selector to SEARCH_RESULT_SELECTORS
    - Add a StreamStrategy to STREAM_STRATEGIES
    - Add an embed host to EMBED_HOSTS
    - Add a header combo to HEADER_TEMPLATES
    - Add a subtitle pattern to SUBTITLE_PATTERNS
    - Add a server pattern to SERVER_DETECTION_PATTERNS
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from bs4 import BeautifulSoup

from anime_watch.core import SESSION, SCRAPE_TIMEOUT, scrape_page_for_video, extract_with_ytdlp

# Transport — prefer curl_cffi for Cloudflare bypass, fallback to requests
try:
    from curl_cffi.requests import Session as _CurlSession
    HAS_CURL = True
except ImportError:
    HAS_CURL = False

if HAS_CURL:
    import requests as _requests
else:
    import requests as _requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SearchPattern:
    """A search URL template to probe."""
    path: str
    params: dict[str, str]
    method: str = "GET"
    json_response: bool = False
    json_body: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    body_template: dict[str, Any] | None = None
    description: str = ""

@dataclass
class StreamStrategy:
    """A stream extraction strategy to try."""
    name: str
    method: str  # "scrape", "iframe", "ytdlp", "m3u8_in_page", "data_attributes", "api_stream"
    description: str = ""
    config_template: dict[str, Any] = field(default_factory=dict)

@dataclass
class SubtitlePattern:
    """A subtitle discovery pattern."""
    name: str
    method: str  # "hls_tracks", "api_endpoint", "page_links", "resource_tracks", "embed_captions"
    description: str = ""

@dataclass
class ServerDetectionPattern:
    """A server/quality detection pattern."""
    name: str
    method: str  # "hls_audio_tracks", "quality_labels", "server_list_api", "dub_languages", "name_suffix"
    description: str = ""

@dataclass
class AuthDetectionPattern:
    """An authentication detection pattern."""
    name: str
    method: str  # "origin_referer", "cookie_file", "token_api", "hmac_signature", "cloudflare", "none"
    indicators: list[str] = field(default_factory=list)
    description: str = ""

@dataclass
class EpisodeAPIPattern:
    """An episode API endpoint pattern to probe."""
    path: str  # template with {id} placeholder
    method: str = "GET"
    ajax: bool = False  # needs X-Requested-With header
    response_type: str = "json"  # "json" or "json_html" (JSON whose result field contains HTML)
    source: str = "builtin"
    description: str = ""

@dataclass
class EmbedExtractPattern:
    """A known embed-host extraction method (e.g., megaclone, generic)."""
    name: str
    embed_signature: str  # URL substring that identifies this embed type (e.g. "/stream/")
    extract_type: str  # "api_from_id" (extract ID then call API), "regex_in_page" (find m3u8 via regex)
    id_regex: str = ""  # regex to extract a file/source ID from embed page HTML
    api_template: str = ""  # API endpoint template, {base} for embed base, {id} for extracted id
    response_json_path: str = ""  # dot-path to m3u8 in JSON response (e.g. "sources.file")
    url_regex: str = ""  # regex to find stream URL directly in embed page (for regex_in_page type)
    description: str = ""

@dataclass
class HLSSelectionPattern:
    """A method to pick the best HLS variant from a master playlist."""
    name: str
    method: str  # "resolution_pick", "bandwidth_pick"
    description: str = ""

@dataclass
class ProxyPattern:
    """A proxy/deobfuscation method for stream URLs."""
    name: str
    method: str  # "png_strip_proxy", "direct"
    cdn_domains: list[str] = field(default_factory=list)
    cdn_suffixes: list[str] = field(default_factory=list)
    description: str = ""

@dataclass
class PostSearchPattern:
    """A post-search enrichment pattern (e.g., season expansion, title cleanup)."""
    name: str
    method: str  # "season_expansion", "title_cleanup"
    trigger_text: str = ""  # text to find in detail page (for season_expansion)
    link_filter: str = ""  # URL pattern to match links within the trigger section
    cleanup_regexes: list[str] = field(default_factory=list)
    description: str = ""

@dataclass
class StreamLanguagePattern:
    """A language-embed resolution pattern (REST API returning languages → embed URLs)."""
    name: str
    api_endpoint: str  # template with {episode_id} placeholder
    method: str = "GET"
    ajax: bool = False
    lang_field: str = "languages"  # JSON field containing language array
    code_field: str = "code"  # JSON field for language code
    embed_field: str = "embed_url"  # JSON field for embed URL
    description: str = ""

@dataclass
class ServerSelectorPattern:
    """A CSS selector + data attribute combo for server entries."""
    name: str
    selector: str  # CSS selector (e.g., "li[data-link-id]")
    id_attr: str  # attribute to extract link/server ID (e.g., "data-link-id")
    type_selector: str = ""  # parent selector for audio type (e.g., ".type")
    type_attr: str = ""  # attribute on parent for audio type (e.g., "data-type")
    description: str = ""

@dataclass
class EncryptionPattern:
    """A custom encryption/decryption algorithm for stream sources."""
    name: str
    method: str  # "fnv1a_xor_cipher", "hmac_sha256"
    seed_endpoint: str = ""  # endpoint to fetch encryption seed
    cipher_params: list[str] = field(default_factory=list)  # algo parameters
    description: str = ""

# ---------------------------------------------------------------------------
# PATTERN REGISTRY — Add new patterns here to extend discovery
# ---------------------------------------------------------------------------

# Default user-agent
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# API subdomains to try (prepended to base URL for discovery)
API_SUBDOMAINS: list[str] = [
    "api",
    "cdn",
    "api2",
    "api3",
    "api4",
    "api5",
    "api6",
    "api-v2",
    "api-v3",
    "v2",
    "v3",
    "app",
    "api.app",
    "service",
    "services",
    "data",
    "rest",
    "restapi",
    "engine",
    "backend",
]

SEARCH_PATTERNS: list[SearchPattern] = [
    # --- Standard GET search patterns ---
    SearchPattern("/search", {"q": "{query}"}, method="GET",
                  description="Standard /search?q={query}"),
    SearchPattern("/search", {"s": "{query}"}, method="GET",
                  description="WordPress-style /search?s={query}"),
    SearchPattern("/", {"s": "{query}"}, method="GET",
                  description="Root with ?s={query} (WordPress)"),
    SearchPattern("/search.php", {"s": "{query}"}, method="GET",
                  description="PHP search endpoint (NetMirror-style)"),
    SearchPattern("/en/search", {"q": "{query}"}, method="GET",
                  description="Locale-prefixed search (StreamingUnity-style)"),
    SearchPattern("/filter", {"keyword": "{query}"}, method="GET",
                  description="Filter endpoint (Anikoto-style)"),
    SearchPattern("/search.html", {"keyword": "{query}"}, method="GET",
                  description="HTML search page"),
    SearchPattern("/", {"q": "{query}"}, method="GET",
                  description="Root with ?q={query}"),

    # --- JSON API patterns ---
    SearchPattern("/api/search", {"q": "{query}"}, method="GET", json_response=True,
                  description="Generic JSON API search (GET)"),
    SearchPattern("/api/search", {"q": "{query}", "type": "movie"}, method="GET",
                  json_response=True,
                  description="JSON API search with movie type (Bingr/TMDB-style)"),
    SearchPattern("/api/search", {"q": "{query}", "type": "tv"}, method="GET",
                  json_response=True,
                  description="JSON API search with tv type"),

    # --- AJAX patterns ---
    SearchPattern("/ajax/search", {"q": "{query}"}, method="GET",
                  headers={"X-Requested-With": "XMLHttpRequest"},
                  description="AJAX search endpoint"),
    SearchPattern("/ajax/episode/list", {"keyword": "{query}"}, method="GET",
                  headers={"X-Requested-With": "XMLHttpRequest", "Referer": "{base_url}/"},
                  description="AJAX episode list as search (Anikoto-style)"),

    # --- POST patterns ---
    SearchPattern("/api/search", {}, method="POST", json_body=True,
                  body_template={"keyword": "{query}", "page": 1},
                  description="POST JSON API search"),
    SearchPattern("/api/search/v2", {}, method="POST", json_body=True,
                  body_template={"keyword": "{query}", "page": 1, "perPage": 20},
                  description="POST JSON API search v2 (MovieBox-style)"),
    SearchPattern("/api/frontend/search", {}, method="POST", json_body=True,
                  body_template={"query": "{query}"},
                  description="Frontend API search (AniDB-style)"),

    # --- Suggest patterns ---
    SearchPattern("/search/suggestions", {"q": "{query}"}, method="GET",
                  description="Search suggestions endpoint"),
    SearchPattern("/autocomplete", {"q": "{query}"}, method="GET",
                  description="Autocomplete endpoint"),

    # --- Legacy patterns ---
    SearchPattern("/search", {"keyword": "{query}"}, method="GET",
                  description="Search with keyword param"),
    SearchPattern("/search", {"search": "{query}"}, method="GET",
                  description="Search with search param"),
    SearchPattern("/index.php", {"search": "{query}"}, method="GET",
                  description="PHP index with search param"),
    SearchPattern("/index.php", {"s": "{query}"}, method="GET",
                  description="PHP index with s param"),
    # --- Locale-prefixed search (StreamingUnity-style) ---
    SearchPattern("/en/titles", {"q": "{query}"}, method="GET",
                  description="StreamingUnity-style locale-prefixed titles search"),
    # --- DB search (Fmovies-style) ---
    SearchPattern("/3/search/movie", {"query": "{query}", "language": "en"}, method="GET",
                  json_response=True,
                  description="Fmovies-style DB search for movies"),
    SearchPattern("/3/search/tv", {"query": "{query}", "language": "en"}, method="GET",
                  json_response=True,
                  description="Fmovies-style DB search for TV shows"),
    # --- PHP search (NetMirror-style) ---
    SearchPattern("/search.php", {"s": "{query}"}, method="GET",
                  json_response=True,
                  description="NetMirror-style PHP search endpoint"),
    SearchPattern("/search.php", {"q": "{query}"}, method="GET",
                  description="PHP search with q param (anixx.fun-style search page)"),
    SearchPattern("/search.php", {"q": "{query}"}, method="GET",
                  json_response=True,
                  description="PHP search with q param returning JSON (JS-rendered live search)"),
    # --- PHP API search (JS-rendered sites) ---
    SearchPattern("/api/search.php", {"q": "{query}"}, method="GET",
                  json_response=True,
                  description="PHP API search returning JSON (generic)"),
    SearchPattern("/api/search_live.php", {"q": "{query}"}, method="GET",
                  json_response=True,
                  description="Live search PHP API returning JSON (anixx.fun-style)"),
    SearchPattern("/api/search_live", {"q": "{query}"}, method="GET",
                  json_response=True,
                  description="Live search API returning JSON"),
    # --- Mobile BFF search (MovieBox-style) ---
    SearchPattern("/wefeed-mobile-bff/subject-api/search/v2", {}, method="POST",
                  json_body=True,
                  body_template={"keyword": "{query}", "page": 1, "perPage": 20,
                                 "subjectType": "All", "tabId": "All"},
                  description="MovieBox-style POST JSON search"),
    SearchPattern("/wefeed-mobile-bff/tab-operating", {"page": "1", "tabId": "0"}, method="GET",
                  json_response=True,
                  description="MovieBox-style init endpoint (returns x-user token)"),
]

SEARCH_RESULT_SELECTORS: list[str] = [
    # --- Attribute-based selectors for links ---
    "a[href*='/anime/']",
    "a[href*='/mid/']",
    "a[href*='/title/']",
    "a[href*='/watch/']",
    "a[href*='watch?']",
    "a[href*='/movie/']",
    "a[href*='/tv/']",
    "a[href*='/detail/']",
    "a[href*='details?']",
    "a[href*='/view/']",
    "a[href*='/series/']",
    "a[href*='/show/']",
    "a[href*='/video/']",
    "a[href*='/play/']",
    "a[href*='/catalog/']",
    "a[href*='/vod/']",
    "a[href*='/stream/']",
    # --- Class-based selectors (containers) ---
    "div.card",
    ".main .item",
    "div.item",
    "div.poster",
    "div.result",
    "div.thumb",
    "div.movie",
    "div.anime",
    "div.media",
    "div.video",
    "div.entry",
    "div.box",
    "div.list-item",
    "article",
    "li.result",
    "li.movie",
    "li.media",
    "li.video",
    "li.item",
    "section",
    "figure",
    # --- General attribute-based selectors ---
    "[class*='result']",
    "[class*='card']",
    "[class*='item']",
    "[class*='movie']",
    "[class*='anime']",
    "[class*='poster']",
    "[class*='thumb']",
    "[class*='video']",
    "[class*='media']",
    "[class*='entry']",
    "[class*='list']",
    "[class*='grid']",
    "[class*='box']",
    "[class*='show']",
    "[class*='title']",
    "[class*='search']",
    "[class*='series']",
    "[class*='vod']",
    "[class*='stream']",
    # --- Table-based ---
    "table tr",
    "tbody tr",
    # --- ID-based ---
    "[id*='result']",
    "[id*='movie']",
    "[id*='anime']",
    "[id*='list']",
    "[id*='grid']",
    "[id*='content']",
]

SEARCH_TITLE_SELECTORS: list[str] = [
    "a[href*='/title/']",
    "a[href*='/anime/']",
    "a[href*='/mid/']",
    "a[href*='/watch/']",
    "a[href*='/movie/']",
    "a[href*='/tv/']",
    "a[href*='/detail/']",
    "a[href*='/series/']",
    "a",
    "p",
    "h2",
    "h3",
    "h4",
    "h5",
    ".title",
    "[class*='title']",
    ".name",
    "[class*='name']",
    "[class*='item'] p",
    ".poster p",
]

SEARCH_IMAGE_SELECTORS: list[str] = [
    "img",
    ".poster img",
    ".thumb img",
    "[class*='poster'] img",
    "[class*='thumb'] img",
    "img[src*='poster']",
    "img[src*='thumb']",
    "img[src*='cover']",
]

# Search data attributes that may hold media IDs or JSON props
SEARCH_DATA_ATTRS: list[str] = [
    "data-tip",
    "data-id",
    "data-media-id",
    "data-mid",
    "data-ids",
    "data-slug",
    "data-page",  # React hydration JSON props (StreamingUnity-style)
]

# Episode patterns
EPISODE_URL_PATTERNS: list[str] = [
    r"/episode/",
    r"/ep-",
    r"/ep/",
    r"/watch/",
    r"/ver/",
    r"/e/",
    r"/v/",
    r"episode=",
    r"ep=",
    r"/season-\d+",
    r"/s\d+",
    r"/season/\d+",
    r"/seasons/\d+",
    r"watch\.php\?id=",  # PHP watch page
    r"watch\?id=",       # Clean-URL watch page (anixx.fun-style)
    r"play\.php\?id=",   # PHP play page (NetMirror-style)
    r"view\.php\?id=",   # PHP view page
    r"episode\.php\?id=",# PHP episode page
    r"details\?id=",     # Details page (anixx.fun-style)
    r"category=",         # Category filter links
    r"\?filter=",         # Filter parameter links
]

EPISODE_SELECTORS: list[str] = [
    "a[href*='/episode/']",
    "a[href*='/ep-']",
    "a[href*='/ep/']",
    "a[href*='/watch/']",
    "a[href*='/ver/']",
    "a[href*='/e/']",
    "a[href*='/v/']",
    "[class*='episode'] a[href]",
    "[id*='episode'] a[href]",
    "[class*='ep'] a[href]",
    "[id*='ep'] a[href]",
    "ul.episodes li a",
    "ul.ep li a",
    "div.ep-list a",
    "div.episodes a",
    "select#episode option",
    "select.episode option",
    "[data-episode] a",
    "a[data-episode]",
    "[data-ep] a",
    "a[data-ep]",
    "[data-number] a",
    "a[data-number]",
    "a[href*='watch.php']",
    "a[href*='watch?']",
    "a[href*='play.php']",
    "a[href*='episode.php']",
    "a[href*='view.php']",
    "a[href*='details?']",
    ".season-select option",  # season selector (anixx.fun-style)
    ".ep-list a[href]",       # episode list (anixx.fun-style)
]

EPISODE_DATA_ATTRS: list[str] = [
    "data-episode",
    "data-ep",
    "data-number",
    "data-num",
    "data-seson",
    "data-season",
    "data-id",
    "data-ids",
    "data-mal",
    "data-slug",
    "data-timestamp",
    "data-link-id",
]

# Stream strategies
STREAM_STRATEGIES: list[StreamStrategy] = [
    StreamStrategy(
        "ajax_stream",
        method="ajax_stream",
        description="AJAX server/list → server?get → embed URL (Anikoto-style)",
        config_template={"type": "ajax_stream", "requires_xrw": True, "embed": True},
    ),
    StreamStrategy(
        "api_language_stream",
        method="api_language_stream",
        description="REST API episodes→languages→embed→m3u8 (AniDB-style)",
        config_template={"type": "api_language_stream"},
    ),
    StreamStrategy(
        "scrape",
        method="scrape",
        description="Run scrape_page_for_video() core helper on episode page",
        config_template={"type": "scrape"},
    ),
    StreamStrategy(
        "iframe+scrape",
        method="iframe",
        description="Find iframes, scrape each for video, check known embed hosts",
        config_template={"type": "iframe", "iframe_selector": "iframe[src*='embed']"},
    ),
    StreamStrategy(
        "iframe+ytdlp",
        method="iframe",
        description="Find iframes, run yt-dlp on iframe URL",
        config_template={"type": "iframe", "iframe_selector": "iframe[src*='embed']", "use_ytdlp": True},
    ),
    StreamStrategy(
        "ytdlp",
        method="ytdlp",
        description="Run yt-dlp --dump-json on episode URL directly",
        config_template={"type": "ytdlp"},
    ),
    StreamStrategy(
        "m3u8_in_page",
        method="m3u8_in_page",
        description="Regex search for .m3u8 URLs in page HTML and scripts",
        config_template={"type": "m3u8_in_page", "extract_m3u8": True},
    ),
    StreamStrategy(
        "m3u8+mp4",
        method="m3u8_in_page",
        description="Regex search for both .m3u8 and .mp4 URLs in page",
        config_template={"type": "m3u8_in_page", "extract_m3u8": True, "extract_mp4": True},
    ),
    StreamStrategy(
        "data_attributes",
        method="data_attributes",
        description="Look for data-src/data-url/data-video/data-source attributes",
        config_template={"type": "scrape"},
    ),
]

# Known iframe embed hosts (for iframe scraping)
EMBED_HOSTS: list[str] = [
    "mp4upload", "streamtape", "vidstream", "gogoanime",
    "yourupload", "doodstream", "dood", "embedsito",
    "mcloud", "mixdrop", "vidsrc", "netu", "gdriveplayer",
    "vixcloud.co", "vixcloud", "megaplay.buzz", "megaclube",
    "pahe.nekostream.site", "nekostream.site", "kotocdn.site",
    "speedracelight.com", "fmovies.gd",
    "videasy.net", "player.videasy.net",  # Next.js embed player (anixx.fun-style)
    "vidsrc.vip", "dl.vidsrc.vip",       # Direct stream host
    "peachify.top", "dl.peachify.top",   # Direct stream host alt
    "tryembed.us.cc",                    # TryEmbed token-based HLS player (anixx.fun-style)
]

# Subtitle discovery patterns
SUBTITLE_PATTERNS: list[SubtitlePattern] = [
    SubtitlePattern(
        "hls_captions",
        method="hls_tracks",
        description="Parse #EXT-X-MEDIA:TYPE=SUBTITLES from HLS playlists",
    ),
    SubtitlePattern(
        "hls_audio_tracks",
        method="hls_tracks",
        description="Parse #EXT-X-MEDIA:TYPE=AUDIO from HLS playlists (language selection)",
    ),
    SubtitlePattern(
        "api_subtitles",
        method="api_endpoint",
        description="Try /api/subtitles/{media_type}/{tmdb_id}?season=&ep= (Bingr-style)",
    ),
    SubtitlePattern(
        "api_ext_captions",
        method="api_endpoint",
        description="Try /api/get-ext-captions?subjectId=&resourceId= (MovieBox-style)",
    ),
    SubtitlePattern(
        "page_links_vtt",
        method="page_links",
        description="Find .vtt subtitle files linked in page HTML",
    ),
    SubtitlePattern(
        "page_links_srt",
        method="page_links",
        description="Find .srt subtitle files linked in page HTML",
    ),
    SubtitlePattern(
        "embed_captions",
        method="resource_tracks",
        description="Parse embed page for track/caption objects with kind=captions",
    ),
    SubtitlePattern(
        "playlist_subtitles",
        method="api_endpoint",
        description="Try playlist.php?id={id}&t=Movie for subtitle tracks (NetMirror-style)",
    ),
    SubtitlePattern(
        "stream_response_subtitles",
        method="resource_tracks",
        description="Parse stream API response for subtitles array",
    ),
]

# Server/quality detection patterns
SERVER_DETECTION_PATTERNS: list[ServerDetectionPattern] = [
    ServerDetectionPattern(
        "hls_audio_tracks",
        method="hls_audio_tracks",
        description="Parse HLS master for #EXT-X-MEDIA:TYPE=AUDIO → server per language",
    ),
    ServerDetectionPattern(
        "quality_labels",
        method="quality_labels",
        description="Parse HLS variants for RESOLUTION labels",
    ),
    ServerDetectionPattern(
        "name_suffix",
        method="name_suffix",
        description="Detect (Sub)/(Dub)/(HSub) suffixes in server/episode titles",
    ),
    ServerDetectionPattern(
        "server_list_api",
        method="server_list_api",
        description="Try /ajax/server/list?servers={ids} for server data (Anikoto-style)",
    ),
    ServerDetectionPattern(
        "dub_languages",
        method="dub_languages",
        description="Look for dub language alternatives in episode data (MovieBox-style)",
    ),
    ServerDetectionPattern(
        "server_names",
        method="server_names",
        description="Use provider-defined SERVER_NAMES dict for predefined server pools",
    ),
    ServerDetectionPattern(
        "neko_mapper_api",
        method="neko_mapper_api",
        description="Probe mapper.nekostream.site/api/mal/{mal_id}/{slug}/{ts} for extra servers",
    ),
]

# Episode API patterns (endpoints that return episode lists)
EPISODE_API_PATTERNS: list[EpisodeAPIPattern] = [
    EpisodeAPIPattern("/ajax/episode/list/{id}", ajax=True, response_type="json_html",
                      description="Anikoto-style AJAX episode list"),
    EpisodeAPIPattern("/api/frontend/anime/{id}/episodes", response_type="json",
                      description="AniDB-style REST episodes API"),
    EpisodeAPIPattern("/api/episodes/{id}", response_type="json",
                      description="Generic episodes API"),
    EpisodeAPIPattern("/ajax/episode/list/{id}", ajax=True, response_type="json_html",
                      source="data_scan",
                      description="AJAX episode list (discovered from page data attrs)"),
    EpisodeAPIPattern("/api/v1/episodes/{id}", response_type="json",
                      description="REST API v1 episodes"),
    EpisodeAPIPattern("/api/frontend/episode/{id}/languages", response_type="json",
                      description="AniDB-style episode language/embed API"),
    EpisodeAPIPattern("/ajax/server/list", response_type="json",
                      description="Anikoto-style AJAX server list"),
    EpisodeAPIPattern("/ajax/server", response_type="json",
                      description="Anikoto-style AJAX server detail (embed URL)"),
    EpisodeAPIPattern("/api/stream", method="POST", response_type="json",
                      description="Bingr-style POST stream API (JSON body with srv/t/id/query)"),
    EpisodeAPIPattern("/api/details/{media_type}/{id}", response_type="json",
                      description="Bingr-style TMDB details API (returns seasons for TV)"),
    EpisodeAPIPattern("/api/episodes/{id}/{season}", response_type="json",
                      description="Bingr-style TMDB episode list API"),
    EpisodeAPIPattern("/en/titles/{id}-{slug}", response_type="json",
                      description="StreamingUnity-style title page with data-page JSON props"),
    EpisodeAPIPattern("/en/titles/{id}-{slug}/season-{season}", response_type="json",
                      description="StreamingUnity-style season page with episode list"),
    EpisodeAPIPattern("/en/iframe/{id}", response_type="json",
                      description="StreamingUnity-style iframe page (scrape for vixcloud embed)"),
    EpisodeAPIPattern("/3/tv/{id}", response_type="json",
                      description="Fmovies-style TV details API"),
    EpisodeAPIPattern("/3/tv/{id}/season/{season}", response_type="json",
                      description="Fmovies-style season episodes API"),
    EpisodeAPIPattern("/seed", method="GET", response_type="json",
                      description="Fmovies-style seed API (returns seed for stream decryption)"),
    # NetMirror-style
    EpisodeAPIPattern("/post.php", method="GET", response_type="json",
                      description="NetMirror-style episode/post detail API (t=ts&id=mid)"),
    EpisodeAPIPattern("/episodes.php", method="GET", response_type="json",
                      description="NetMirror-style season episodes API (t=ts&s=sid&series=mid)"),
    EpisodeAPIPattern("/play.php", method="POST", response_type="json",
                      description="NetMirror-style play token API (POST form: id=ep_id)"),
    EpisodeAPIPattern("/playlist.php", method="GET", response_type="json",
                      description="NetMirror-style playlist API (id=ep_id&t=Movie&tm=ts&h=hash)"),
    EpisodeAPIPattern("/hls/{id}.m3u8", method="GET", response_type="json",
                      description="NetMirror-style direct HLS endpoint (?in=token)"),
    # PHP API episode endpoints (JS-rendered sites)
    EpisodeAPIPattern("/api/episode/{id}", response_type="json",
                      description="Generic PHP API episode detail (?id=)"),
    EpisodeAPIPattern("/api/episodes.php", method="GET", response_type="json",
                      description="PHP episodes API (?id=&season=)"),
    EpisodeAPIPattern("/api/play.php", method="GET", response_type="json",
                      description="PHP play API (?id=)"),
    EpisodeAPIPattern("/api/watch.php", method="GET", response_type="json",
                      description="PHP watch API (?id=)"),
    EpisodeAPIPattern("/api/server.php", method="GET", response_type="json",
                      description="PHP server API (?id=)"),
    # MovieBox-style
    EpisodeAPIPattern("/wefeed-mobile-bff/subject-api/get", method="GET", response_type="json",
                      description="MovieBox-style subject detail API (?subjectId=id)"),
    EpisodeAPIPattern("/wefeed-mobile-bff/subject-api/resource", method="GET", response_type="json",
                      description="MovieBox-style resource list API (?subjectId=id&se=season&ep=ep)"),
    EpisodeAPIPattern("/wefeed-mobile-bff/subject-api/get-ext-captions", method="GET", response_type="json",
                      description="MovieBox-style external captions API (?subjectId=id&resourceId=rid)"),
]

# Authentication detection patterns
AUTH_DETECTION_PATTERNS: list[AuthDetectionPattern] = [
    AuthDetectionPattern(
        "cloudflare",
        method="cloudflare",
        indicators=["just a moment", "cf-browser-verification", "cloudflare", "__cfduid"],
        description="Detect Cloudflare protection",
    ),
    AuthDetectionPattern(
        "cookie_required",
        method="cookie_file",
        indicators=["login", "sign in", "cookie", "set-cookie"],
        description="Site requires cookies or login",
    ),
    AuthDetectionPattern(
        "token_api",
        method="token_api",
        indicators=["token", "x-user", "access-token"],
        description="API returns tokens on init request",
    ),
    AuthDetectionPattern(
        "origin_referer",
        method="origin_referer",
        indicators=[],
        description="API requires Origin/Referer headers",
    ),
    AuthDetectionPattern(
        "hmac_signature",
        method="hmac_signature",
        indicators=["signature", "x-tr-signature", "x-client-token", "hmac"],
        description="Request signing via HMAC or custom cipher",
    ),
    AuthDetectionPattern(
        "spoofed_android",
        method="spoofed_android",
        indicators=["x-client-info", "x-client-status", "x-forwarded-for", "x-play-mode"],
        description="MovieBox-style: spoofed Android device info, IP, and client metadata via headers",
    ),
    AuthDetectionPattern(
        "timestamp_nonce",
        method="timestamp_nonce",
        indicators=["t=", "&tm=", "&h="],
        description="NetMirror-style: timestamp+nonce in query params for request validation",
    ),
]

# Header templates to try for authentication
HEADER_TEMPLATES: list[dict[str, str]] = [
    {"User-Agent": DEFAULT_UA},
    {"User-Agent": DEFAULT_UA, "Referer": "{base_url}/"},
    {"User-Agent": DEFAULT_UA, "Origin": "{base_url}", "Referer": "{base_url}/"},
    {"User-Agent": DEFAULT_UA, "X-Requested-With": "XMLHttpRequest"},
    {"User-Agent": DEFAULT_UA, "X-Requested-With": "XMLHttpRequest",
     "Referer": "{base_url}/"},
]

# Stream embed extraction patterns (e.g., megaclone, generic m3u8/mp4)
EMBED_EXTRACT_PATTERNS: list[EmbedExtractPattern] = [
    EmbedExtractPattern(
        "megaclone", embed_signature="/stream/", extract_type="api_from_id",
        id_regex=r"File\s+(\d+)\s*-",
        api_template="{base}/stream/getSources?id={id}",
        response_json_path="sources.file",
        description="Megaclone embed: extract File {id}, call /stream/getSources for m3u8",
    ),
    EmbedExtractPattern(
        "generic_m3u8", embed_signature="", extract_type="regex_in_page",
        url_regex=r'(https?://[^"\'<> ]+\.m3u8[^"\'<> ]*)',
        description="Generic embed: find .m3u8 URL via regex in HTML",
    ),
    EmbedExtractPattern(
        "generic_mp4", embed_signature="", extract_type="regex_in_page",
        url_regex=r'(https?://[^"\'<> ]+\.mp4[^"\'<> ]*)',
        description="Generic embed: find .mp4 URL via regex in HTML",
    ),
    EmbedExtractPattern(
        "megaclone_dub", embed_signature="/stream/", extract_type="api_from_id",
        id_regex=r"File\s+(\d+)\s*-",
        api_template="{base}/stream/getSources?id={id}&type=dub",
        response_json_path="sources.file",
        description="Megaclone embed with dub type suffix",
    ),
    EmbedExtractPattern(
        "vixcloud_hls", embed_signature="vixcloud.co", extract_type="regex_in_page",
        id_regex=r"'token':\s*'([^']+)'",
        url_regex=r"window\.streams\s*=\s*(\[.+?\])\s*;",
        description="Vixcloud embed: extract token/expires/streams array, build HLS URL (StreamingUnity-style)",
    ),
    EmbedExtractPattern(
        "fmovies_encrypted", embed_signature="", extract_type="api_from_id",
        id_regex=r"",
        api_template="{base}/seed?mediaId={id}",
        response_json_path="sources",
        description="Fmovies-style: GET seed → decrypt sources-with-title response (fnv1a+XOR cipher)",
    ),
    EmbedExtractPattern(
        "bingr_stream_api", embed_signature="", extract_type="api_from_id",
        id_regex=r"",
        api_template="{base}/api/stream",
        response_json_path="sources",
        description="Bingr-style: POST /api/stream with JSON body {srv, t, id, query} → sources[].url",
    ),
    EmbedExtractPattern(
        "dl_vidsrc", embed_signature="vidsrc.vip", extract_type="api_from_id",
        id_regex=r"",
        api_template="{base}/api/source",
        response_json_path="",
        description="Vidsrc.vip download/stream URL (anixx.fun-style: dl.vidsrc.vip/{type}/{id}/{s}/{e})",
    ),
    EmbedExtractPattern(
        "dl_peachify", embed_signature="peachify.top", extract_type="api_from_id",
        id_regex=r"",
        api_template="{base}/api/source",
        response_json_path="",
        description="Peachify.top download/stream URL (anixx.fun-style: dl.peachify.top/{type}/{id}/{s}/{e})",
    ),
    EmbedExtractPattern(
        "videasy_player", embed_signature="player.videasy.net", extract_type="regex_in_page",
        url_regex=r'(https?://[^"\'<> ]+\.m3u8[^"\'<> ]*)',
        description="Videasy Next.js player: extract m3u8 from page HTML or JS (anixx.fun-style)",
    ),
    EmbedExtractPattern(
        "tryembed", embed_signature="tryembed.us.cc", extract_type="api_from_id",
        description=(
            "TryEmbed token-based player. Embed page has RAW_PAYLOAD (base64) with "
            "{anilist_id, episode, audio} and EMBED_NONCE. "
            "Call /api/stream_data?id={id}&episode={ep}&audio={audio}&nonce={nonce} "
            "with X-Embed-Nonce header. Response has qualities[].token → "
            "build m3u8 at {HOST}/s/{token}.m3u8. "
            "Requires curl_cffi browser impersonation (Cloudflare anti-bot)."
        ),
    ),
]

# HLS variant selection patterns (from master playlist)
HLS_SELECTION_PATTERNS: list[HLSSelectionPattern] = [
    HLSSelectionPattern("resolution_pick", method="resolution_pick",
                        description="Parse #EXT-X-STREAM-INF:RESOLUTION= and pick by height"),
    HLSSelectionPattern("bandwidth_pick", method="bandwidth_pick",
                        description="Parse #EXT-X-STREAM-INF:BANDWIDTH= and pick by bitrate"),
    HLSSelectionPattern("field_quality_pick", method="field_quality_pick",
                        description="Bingr-style: pick by closest match from sources[].quality field (e.g. '1080p')"),
    HLSSelectionPattern("field_height_pick", method="field_height_pick",
                        description="Fmovies-style: pick by QUALITY_HEIGHTS dict mapping from sources[].quality field"),
]

# Quality/height lookup tables (for field-based quality picking)
QUALITY_HEIGHTS: dict[str, int] = {
    "2160p": 2160,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "best": 99999,
}

# Image CDN URL templates for poster/thumbnail construction
IMAGE_URL_TEMPLATES: list[dict[str, str]] = [
    {"pattern": "https://image.tmdb.org/t/p/w185{poster_path}", "source": "tmdb"},
    {"pattern": "{cdn}/images/{filename}", "source": "streamingunity_cdn"},
    {"pattern": "https://image.tmdb.org/t/p/w342{path}", "source": "tmdb_w342"},
]

# Proxy/deobfuscation patterns for stream URLs
PROXY_PATTERNS: list[ProxyPattern] = [
    ProxyPattern(
        "png_wrap_proxy", method="png_strip_proxy",
        cdn_domains=["mt.nekostream.site", "9hjkrt.nekostream.site",
                      "vidtub.kotocdn.site", "megap.kotocdn.site"],
        cdn_suffixes=[".nekostream.site"],
        description="PNG-wrapped HLS segments: strip IEND chunk from .ts data",
    ),
    ProxyPattern("direct", method="direct",
                 description="No proxy needed, direct stream access"),
    ProxyPattern("local_playlist_proxy", method="playlist_rewrite",
                 description="Fmovies/Anikoto-style: download master playlist, rewrite segment paths → local HTTP server"),
    ProxyPattern("netmirror_hls_proxy", method="hls_audio_select",
                 description="NetMirror-style: rewrite master playlist with audio language selection, sub-playlist rewriting, local HTTP proxy"),
]

# Post-search enrichment patterns
POST_SEARCH_PATTERNS: list[PostSearchPattern] = [
    PostSearchPattern(
        "anidb_seasons", method="season_expansion",
        trigger_text="Seasons",
        link_filter="/anime/",
        cleanup_regexes=[r"^\d+", r"\d{4}$", r"^Now"],
        description="AniDB-style: find div with 'Seasons' text, extract season links",
    ),
]

# Stream language resolution patterns (languages API → embed URL)
STREAM_LANGUAGE_PATTERNS: list[StreamLanguagePattern] = [
    StreamLanguagePattern(
        "anidb_languages",
        api_endpoint="/api/frontend/episode/{episode_id}/languages",
        lang_field="languages", code_field="code", embed_field="embed_url",
        description="AniDB-style: GET languages → pick by code → extract embed_url",
    ),
]

# Server CSS selector patterns (extract server list entries from HTML)
SERVER_SELECTOR_PATTERNS: list[ServerSelectorPattern] = [
    ServerSelectorPattern(
        "anikoto_server_li", selector="li[data-link-id]", id_attr="data-link-id",
        type_selector=".type", type_attr="data-type",
        description="Anikoto-style: li[data-link-id] in .type parent for audio detection",
    ),
    ServerSelectorPattern(
        "generic_server_list", selector="[data-link-id]", id_attr="data-link-id",
        description="Generic: any element with data-link-id"),
    ServerSelectorPattern(
        "generic_server_options", selector="option[value]", id_attr="value",
        description="Generic: <option> elements with server IDs"),
]

# Title cleanup regexes (post-extraction normalization)
TITLE_CLEANUP_REGEXES: list[str] = [
    r"^\d+",       # leading digits (season numbers)
    r"\d{4}$",     # trailing year
    r"^Now",       # prefix "Now"
    r"\s+\(\w+\)$",  # trailing parenthetical like "(TV)", "(Dub)"
    r"\s*\(.*\)\s*", # any parenthetical
]

# Trailing-ID extraction regexes (extract numeric ID from slug-style URLs)
ID_EXTRACT_PATTERNS: list[str] = [
    r"-(\d+)$",            # trailing dash+digits (anidb-style)
    r"/mid/(\d+)",         # /mid/{id} (anikoto-style)
    r"[-/](\d+)(?:/|$|[?#])",  # any path segment digits
]

# Audio language mappings (preference codes → provider language codes)
AUDIO_LANG_MAP: dict[str, str] = {
    "sub": "eng",
    "eng": "eng",
    "english": "eng",
    "hin": "hin",
    "hindi": "hin",
    "jpn": "jpn",
    "japanese": "jpn",
}

# Host pool patterns (providers with multiple API endpoints for failover)
HOST_POOL_PATTERNS: list[list[str]] = [
    ["api6.aoneroom.com", "api5.aoneroom.com", "api4.aoneroom.com",
     "api4sg.aoneroom.com", "api3.aoneroom.com", "api6sg.aoneroom.com",
     "api.inmoviebox.com"],
]

# Episode code patterns (encoding season+episode into a single number)
EPISODE_CODE_PATTERNS: list[dict[str, int | str]] = [
    {"name": "moviebox_epcode", "multiplier": 100,
     "formula": "season * 100 + episode",
     "description": "MovieBox-style: episode code = season * 100 + episode (e.g. S01E01=101)"},
]

# Token parsing patterns (extract auth/play info from opaque tokens)
TOKEN_PARSE_PATTERNS: list[dict[str, str]] = [
    {"name": "netmirror_token", "prefix": "in=",
     "delimiter": "::", "fields": "in_token,hash,timestamp,is_premium,user_token",
     "description": "NetMirror-style: in={token}::{hash}::{ts}::{is_premium}::{user_token}"},
]

# Encryption/decryption patterns for protected stream sources
ENCRYPTION_PATTERNS: list[EncryptionPattern] = [
    EncryptionPattern(
        "fmovies_fnv1a_xor", method="fnv1a_xor_cipher",
        seed_endpoint="/seed?mediaId={id}",
        cipher_params=["fnv1a", "fmix", "init_state", "generate_xor_key", "MVM1 magic"],
        description="Fmovies-style: GET seed → GET sources-with-title (encrypted) → decrypt with fnv1a+XOR (MurmurHash3-derived)",
    ),
]

# ---------------------------------------------------------------------------
# Discovery Engine
# ---------------------------------------------------------------------------

class DiscoveryResult:
    """Holds the full discovery output for one site."""
    def __init__(self, url: str):
        self.url = url
        self.search_config: Optional[dict] = None
        self.search_found_methods: list[str] = []
        self.search_failed_attempts: list[dict] = []
        self.episodes_config: Optional[dict] = None
        self.episode_urls_found: list[str] = []
        self.episode_count: int = 0
        self.episode_failed_attempts: list[dict] = []
        self.stream_config: Optional[dict] = None
        self.stream_found_methods: list[dict] = []
        self.stream_failed_attempts: list[dict] = []
        self.subtitle_methods_found: list[str] = []
        self.server_methods_found: list[str] = []
        self.auth_type: Optional[str] = None
        self.auth_detail: str = ""
        self.sample_search_url: Optional[str] = None
        self.sample_episode_url: Optional[str] = None
        self.detected_json: bool = False
        self.detected_selectors: list[str] = []
        self.pages_fetched: int = 0
        self.elapsed: float = 0.0

    def _adopt(self, other: DiscoveryResult):
        for k, v in vars(other).items():
            if v is not None:
                setattr(self, k, v)

    @property
    def search_confidence(self) -> float:
        if not self.search_found_methods:
            return 0.0
        base = 0.5
        if self.search_config:
            base += 0.3
        return min(base + len(self.detected_selectors) * 0.05, 1.0)

    @property
    def episodes_confidence(self) -> float:
        if self.episode_count == 0:
            return 0.0
        return min(0.5 + self.episode_count * 0.05, 1.0)

    @property
    def stream_confidence(self) -> float:
        if not self.stream_found_methods:
            return 0.0
        return 0.7 + len(self.stream_found_methods) * 0.1

    def to_config(self) -> dict:
        cfg: dict = {}
        if self.search_config:
            cfg["search"] = self.search_config
        if self.episodes_config:
            cfg["episodes"] = self.episodes_config
        if self.stream_config:
            cfg["stream"] = self.stream_config
        return cfg

    def to_site_config(self, name: str = "", slug: str = "") -> dict:
        if not name:
            netloc = urllib.parse.urlparse(self.url).netloc
            name = netloc.split(".")[-2].title() if netloc.count(".") > 1 else "Site"
        if not slug:
            netloc = urllib.parse.urlparse(self.url).netloc
            slug = netloc.split(".")[0]
        return {
            "name": name,
            "slug": slug,
            "url": self.url.rstrip("/"),
            "category": "movies",
            **self.to_config(),
        }

    def summary(self) -> str:
        lines = []
        lines.append(f"Site: {self.url}")
        lines.append(f"Time: {self.elapsed:.1f}s | Pages fetched: {self.pages_fetched}")
        lines.append("")
        lines.append("── Search ──")
        if self.search_config:
            lines.append(f"  Method: {'JSON API' if self.detected_json else 'HTML scraping'}")
            lines.append(f"  Path: {self.search_config.get('url', '?')}")
            lines.append(f"  Confidence: {self.search_confidence:.0%}")
            for sel in self.detected_selectors:
                lines.append(f"  Selector: {sel}")
        else:
            lines.append("  NOT FOUND")
            for a in self.search_failed_attempts[:5]:
                lines.append(f"  ✗ {a.get('path', '?')} → {a.get('status', '?')}")

        lines.append("")
        lines.append("── Episodes ──")
        if self.episodes_config and self.episode_count > 0:
            lines.append(f"  Mode: {self.episodes_config.get('use_generic') and 'generic' or 'custom selectors'}")
            lines.append(f"  Episodes found: {self.episode_count}")
            lines.append(f"  Sample: {self.sample_episode_url or 'N/A'}")
            lines.append(f"  Confidence: {self.episodes_confidence:.0%}")
        else:
            lines.append("  NOT FOUND")

        lines.append("")
        lines.append("── Stream ──")
        if self.stream_found_methods:
            for m in self.stream_found_methods:
                lines.append(f"  ✓ {m.get('name', '?')} — {m.get('url', '')[:80]}")
        else:
            lines.append("  NOT FOUND")
        lines.append(f"  Confidence: {self.stream_confidence:.0%}")

        lines.append("")
        lines.append("── Subtitles ──")
        if self.subtitle_methods_found:
            for s in self.subtitle_methods_found:
                lines.append(f"  ✓ {s}")
        else:
            lines.append("  None detected")

        lines.append("")
        lines.append("── Auth ──")
        lines.append(f"  {self.auth_type or 'None detected'}")
        if self.auth_detail:
            lines.append(f"  Detail: {self.auth_detail}")

        lines.append("")
        lines.append("── Servers ──")
        if self.server_methods_found:
            for s in self.server_methods_found:
                lines.append(f"  ✓ {s}")
        else:
            lines.append("  None detected (single-server)")

        return "\n".join(lines)


class DiscoveryEngine:
    """Main discovery engine. Walks through search → episodes → stream → subtitles."""

    def __init__(self, base_url: str, test_query: str = "naruto", timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.test_query = test_query
        self.timeout = min(timeout, 6)  # cap at 6s per request
        self.parsed = urllib.parse.urlparse(self.base_url)
        if HAS_CURL:
            from curl_cffi.requests import Session as _CurlSession
            self.session = _CurlSession(impersonate="chrome124")
        else:
            self.session = _requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_UA})
        self.session.headers.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                      "Accept-Language": "en-US,en;q=0.5"})
        self.result = DiscoveryResult(base_url)
        self._pages = 0
        self._cache: dict[str, Optional[requests.Response]] = {}
        self._warmed_up = False
        self._referer: Optional[str] = None
        self._media_ids: list[str] = []
        self._episode_data_ids: list[str] = []
        self._episode_mapper_data: list[dict] = []

    # ------------------------------------------------------------------
    # TRANSPORT: cache, warmup, referer
    # ------------------------------------------------------------------

    def _warmup(self):
        """Visit the homepage to set cookies / Cloudflare clearance."""
        if self._warmed_up:
            return
        self._log("WARMUP", self.base_url, "BUSY")
        r = self._fetch(f"{self.base_url}/")
        if r:
            self._warmed_up = True
            self._log("WARMUP", self.base_url, str(r.status_code))
        else:
            self._log("WARMUP", self.base_url, "no_response")

    def _cache_key(self, url: str, params: dict | None = None) -> str:
        if params:
            return url + "?" + urllib.parse.urlencode(sorted(params.items()))
        return url

    def _fetch(self, url: str, headers: Optional[dict] = None,
               timeout: Optional[int] = None) -> Optional[requests.Response]:
        ck = self._cache_key(url)
        if ck in self._cache:
            return self._cache[ck]
        try:
            h = dict(self.session.headers)
            if headers:
                h.update(headers)
            if self._referer and "Referer" not in h:
                h["Referer"] = self._referer
            r = self.session.get(url, headers=h, timeout=timeout or self.timeout)
            self._pages += 1
            # Update referer for next request
            if r.status_code in (200, 201):
                self._referer = url
            self._cache[ck] = r
            return r
        except Exception:
            self._cache[ck] = None
            return None

    def _fetch_post(self, url: str, json_body: dict,
                    headers: Optional[dict] = None) -> Optional[requests.Response]:
        ck = self._cache_key(url) + "#POST"
        if ck in self._cache:
            return self._cache[ck]
        try:
            h = dict(self.session.headers)
            if headers:
                h.update(headers)
            if self._referer and "Referer" not in h:
                h["Referer"] = self._referer
            r = self.session.post(url, json=json_body, headers=h, timeout=self.timeout)
            self._pages += 1
            if r.status_code in (200, 201):
                self._referer = url
            self._cache[ck] = r
            return r
        except Exception:
            self._cache[ck] = None
            return None

    def _head(self, url: str) -> Optional[requests.Response]:
        """Quick HEAD to check if a host/path is reachable."""
        try:
            r = self.session.head(url, timeout=3)
            self._pages += 1
            return r
        except Exception:
            return None

    def _log(self, phase: str, url: str, status: str, detail: str = ""):
        """Print an incremental discovery log line."""
        icon_map = {"OK": "✓", "PARTIAL": "~", "BUSY": "·", "DETECTED": "!", "FAIL": "✗",
                    "ERR": "✗", "CLOUDFLARE": "!"}
        icon = icon_map.get(status, "·")
        if status in ("no_response",) or (status.isdigit() and int(status) >= 400):
            icon = "✗"
        t = time.strftime("%H:%M:%S")
        parts = [f"[{t}]", f"[{phase:8s}]", f"{icon}"]
        # Truncate URL for display
        display_url = url if len(url) < 90 else url[:80] + "..."
        parts.append(display_url)
        parts.append(f"→ {status}")
        if detail:
            parts.append(f"({detail})")
        print("  " + " ".join(parts), flush=True)

    def _is_valid_response(self, r: Optional[requests.Response]) -> bool:
        if r is None:
            return False
        if r.status_code not in (200, 201):
            return False
        if not r.text or len(r.text.strip()) < 20:
            return False
        return True

    def _is_json_response(self, r: requests.Response) -> bool:
        ct = (r.headers.get("Content-Type", "") or "").lower()
        if "json" in ct:
            return True
        try:
            json.loads(r.text[:1000])
            return True
        except (json.JSONDecodeError, ValueError):
            return False

    def _is_cloudflare(self, r: requests.Response) -> bool:
        text_lower = r.text.lower()
        indicators = ["just a moment", "cf-browser-verification", "cloudflare"]
        return any(i in text_lower for i in indicators) and len(r.text) < 2000

    # ------------------------------------------------------------------
    # AJAX ENDPOINT SCANNING
    # ------------------------------------------------------------------

    def _scan_ajax_endpoints(self, page_url: str) -> list[dict]:
        """Fetch a page and scan HTML/scripts for AJAX API endpoint patterns.
        Returns a list of dicts with {'endpoint', 'method', 'params', 'headers'}."""
        found: list[dict] = []
        r = self._fetch(page_url)
        if not r or not self._is_valid_response(r):
            return found

        soup = BeautifulSoup(r.text, "lxml")
        page_text = r.text

        # Pattern 1: Script tags with AJAX URL strings
        ajax_url_patterns = [
            r"['\"]/ajax/([^'\"]+)['\"]",
            r"['\"]([^'\"]*ajax[^'\"]*)['\"]",
            r"url:\s*['\"]/ajax/([^'\"]+)['\"]",
            r"['\"/](api/v[12]/(?:episode|server|stream|link)[^'\"]*)['\"]",
        ]
        for pattern in ajax_url_patterns:
            for m in re.finditer(pattern, page_text, re.IGNORECASE):
                ep = m.group(1).strip()
                if ep and len(ep) > 3 and ep not in [e["endpoint"] for e in found]:
                    # Determine likely method from endpoint name
                    method = "GET"
                    if any(kw in ep.lower() for kw in ("list", "get", "info")):
                        method = "GET"
                    elif any(kw in ep.lower() for kw in ("create", "add", "update", "delete")):
                        method = "POST"
                    found.append({
                        "endpoint": ep,
                        "method": method,
                        "headers": {"X-Requested-With": "XMLHttpRequest"},
                        "source": "script_tag",
                    })

        # Pattern 2: data attributes with AJAX URLs
        for attr in ("data-url", "data-api", "data-endpoint", "data-link", "data-href"):
            for el in soup.select(f"[{attr}]"):
                val = el.get(attr, "")
                if "ajax" in val.lower() or "/api/" in val.lower():
                    if val not in [e["endpoint"] for e in found]:
                        found.append({
                            "endpoint": val,
                            "method": "GET",
                            "headers": {"X-Requested-With": "XMLHttpRequest"},
                            "source": f"data_attr.{attr}",
                        })

        # Pattern 3: Links/buttons with data-* attributes suggesting AJAX
        for el in soup.select("[data-link-id], [data-ids], [data-media-id], [data-id]"):
            for attr in ("data-link-id", "data-ids", "data-media-id", "data-id"):
                val = el.get(attr, "")
                if val and val not in [e.get("endpoint", "") for e in found]:
                    # Suggest server list / episode list endpoints
                    found.append({
                        "endpoint": f"/ajax/server/list?servers={val}",
                        "method": "GET",
                        "headers": {"X-Requested-With": "XMLHttpRequest"},
                        "source": f"data_attr.{attr}",
                    })
                    found.append({
                        "endpoint": f"/ajax/episode/list/{val}",
                        "method": "GET",
                        "headers": {"X-Requested-With": "XMLHttpRequest"},
                        "source": f"data_attr.{attr}",
                    })

        return found

    def _try_api_language_stream(self, episode_url: str) -> Optional[dict]:
        """Try REST API language→embed stream (AniDB-style).
        Chain: episodes API → pick episode id → languages API → embed url → m3u8."""
        anime_id = None
        m = re.search(r"-(\d+)$", episode_url.rstrip("/"))
        if m:
            anime_id = m.group(1)
        if not anime_id:
            return None

        # Step 1: get episodes
        eps_url = f"{self.base_url}/api/frontend/anime/{anime_id}/episodes"
        r = self._fetch(eps_url)
        if not r or r.status_code != 200:
            return None
        try:
            data = r.json()
            episodes = data.get("episodes", data.get("data", []))
            if not episodes:
                return None
            first_ep = episodes[0]
            ep_id = first_ep.get("id", first_ep.get("episodeId", ""))
            if not ep_id:
                return None

            # Step 2: get languages
            lang_url = f"{self.base_url}/api/frontend/episode/{ep_id}/languages"
            r2 = self._fetch(lang_url)
            if not r2 or r2.status_code != 200:
                return None
            data2 = r2.json()
            langs = data2.get("languages", data2.get("data", []))
            if not langs:
                return None
            embed = langs[0].get("embed_url", "")
            if not embed:
                return None

            # Step 3: fetch embed, extract m3u8
            m3u8_url = ""
            embed_page = self._fetch(embed)
            if embed_page and embed_page.status_code == 200:
                for pat in [
                    r'(https?://[^"\'<> ]+\.m3u8[^"\'<> ]*)',
                    r"'(/[^']+\.m3u8[^']*)'",
                ]:
                    m = re.search(pat, embed_page.text, re.IGNORECASE)
                    if m:
                        m3u8_url = m.group(1)
                        if m3u8_url.startswith("/"):
                            m3u8_url = urllib.parse.urljoin(embed, m3u8_url)
                        break

            return {
                "name": "api_language_stream",
                "method": "api_language_stream",
                "url": (m3u8_url or embed)[:120],
                "is_direct": bool(m3u8_url),
                "detail": f"AniDB chain: episodes→languages→embed→{'m3u8' if m3u8_url else 'embed'}",
                "embed_url": embed,
                "m3u8_url": m3u8_url,
            }
        except (json.JSONDecodeError, ValueError, AttributeError, KeyError):
            return None

    def _try_mapper_api(self) -> Optional[dict]:
        """Try neko mapper API (mapper.nekostream.site) for additional servers.
        Uses data-mal, data-slug, data-timestamp collected from episode links."""
        for md in self._episode_mapper_data[:3]:
            mapper_url = f"https://mapper.nekostream.site/api/mal/{md['mal']}/{md['slug']}/{md['ts']}"
            r = self._fetch(mapper_url)
            if r and r.status_code == 200:
                try:
                    data = r.json()
                    servers = [(k, v) for k, v in data.items() if k != "status" and isinstance(v, dict)]
                    if servers:
                        return {
                            "name": "mapper_api",
                            "method": "mapper_api",
                            "url": mapper_url,
                            "is_direct": False,
                            "detail": f"neko mapper API: {len(servers)} servers found",
                            "server_count": len(servers),
                        }
                except (json.JSONDecodeError, ValueError):
                    pass
        return None

    def _try_ajax_stream_api(self, episode_url: str) -> Optional[dict]:
        """Try to discover stream via AJAX server/list → server?get pattern (Anikoto-style)."""
        # Try data-ids collected from AJAX episode API first
        for data_ids in self._episode_data_ids[:3]:
            result = self._try_data_ids_stream(data_ids, episode_url)
            if result:
                return result

        page = self._fetch(episode_url)
        if not page or not self._is_valid_response(page):
            return None
        soup = BeautifulSoup(page.text, "lxml")

        # Look for data-link-id / data-ids in the page (Anikoto puts these on episode links)
        for el in soup.select("[data-ids], [data-link-id]"):
            data_ids = el.get("data-ids", "") or el.get("data-link-id", "") or ""
            if data_ids:
                server_url = f"{self.base_url}/ajax/server/list?servers={data_ids}"
                r = self._fetch(server_url, headers={"X-Requested-With": "XMLHttpRequest"})
                if r and r.status_code == 200:
                    try:
                        body = r.json()
                        if body.get("status") == 200:
                            server_soup = BeautifulSoup(body.get("result", ""), "lxml")
                            link_els = server_soup.select("[data-link-id]")
                            if link_els:
                                link_id = link_els[0].get("data-link-id", "")
                                if link_id:
                                    stream_url = f"{self.base_url}/ajax/server?get={link_id}"
                                    r2 = self._fetch(stream_url,
                                                     headers={"X-Requested-With": "XMLHttpRequest"})
                                    if r2 and r2.status_code == 200:
                                        try:
                                            body2 = r2.json()
                                            embed = body2.get("result", {}).get("url", "")
                                            if embed:
                                                m3u8_url, subtitles = self._extract_m3u8_from_embed(embed)
                                                return {
                                                    "name": "ajax_server_stream",
                                                    "method": "ajax_stream",
                                                    "url": (m3u8_url or embed)[:120],
                                                    "is_direct": bool(m3u8_url),
                                                    "detail": "Anikoto-style AJAX server→embed→m3u8" if m3u8_url else "Anikoto-style AJAX embed",
                                                    "embed_url": embed,
                                                    "m3u8_url": m3u8_url,
                                                    "subtitles": subtitles,
                                                }
                                        except (json.JSONDecodeError, ValueError, AttributeError):
                                            pass
                    except (json.JSONDecodeError, ValueError, AttributeError):
                        pass

        # Try AJAX endpoint scanning
        ajax_endpoints = self._scan_ajax_endpoints(episode_url)
        for ep in ajax_endpoints:
            if "server" in ep["endpoint"] and "get" in ep["endpoint"]:
                url = f"{self.base_url}{ep['endpoint']}"
                r = self._fetch(url, headers=ep["headers"])
                if r and r.status_code == 200:
                    try:
                        body = r.json()
                        embed = body.get("result", {}).get("url", "")
                        if embed:
                            return {
                                "name": "ajax_stream",
                                "method": "ajax_stream",
                                "url": embed[:120],
                                "is_direct": False,
                                "detail": f"discovered AJAX stream endpoint: {ep['endpoint']}",
                                "embed_url": embed,
                            }
                    except (json.JSONDecodeError, ValueError, AttributeError):
                        pass

        return None

    def _extract_m3u8_from_embed(self, embed: str) -> tuple[str, list[dict]]:
        """Extract m3u8 URL and subtitles from an embed page.
        Tries megaclone chain (/stream/ → File {id} → /stream/getSources) first,
        then falls back to generic m3u8 regex."""
        m3u8_url = ""
        subtitles: list[dict] = []
        embed_page = self._fetch(embed)
        if not embed_page or embed_page.status_code != 200:
            return m3u8_url, subtitles
        # Try megaclone chain first
        if "/stream/" in embed:
            file_id_m = re.search(r"File\s+(\d+)\s*-", embed_page.text)
            if file_id_m:
                file_id = file_id_m.group(1)
                api_base = f"{urllib.parse.urlparse(embed).scheme}://{urllib.parse.urlparse(embed).netloc}"
                api_url = f"{api_base}/stream/getSources?id={file_id}"
                sources_r = self._fetch(api_url, headers={"X-Requested-With": "XMLHttpRequest", "Referer": embed})
                if sources_r and sources_r.status_code == 200:
                    try:
                        sd = sources_r.json()
                        sources = sd.get("sources", {})
                        m3u8_url = sources.get("file", "") if isinstance(sources, dict) else ""
                        for t in sd.get("tracks", []):
                            if t.get("kind") == "captions" and t.get("file"):
                                subtitles.append({"url": t["file"], "label": t.get("label", "")})
                    except (json.JSONDecodeError, ValueError, AttributeError):
                        pass
        if not m3u8_url:
            for pat in [
                r'(https?://[^"\'<> ]+\.m3u8[^"\'<> ]*)',
                r"'(/[^']+\.m3u8[^']*)'",
            ]:
                m = re.search(pat, embed_page.text, re.IGNORECASE)
                if m:
                    m3u8_url = m.group(1)
                    if m3u8_url.startswith("/"):
                        m3u8_url = urllib.parse.urljoin(embed, m3u8_url)
                    break
        return m3u8_url, subtitles

    def _try_data_ids_stream(self, data_ids: str, referer: str) -> Optional[dict]:
        """Given data-ids from an episode, walk the AJAX server→stream chain."""
        # Step 1: server/list → get link_id
        server_url = f"{self.base_url}/ajax/server/list?servers={data_ids}"
        r = self._fetch(server_url, headers={"X-Requested-With": "XMLHttpRequest", "Referer": referer})
        if not r or r.status_code != 200:
            return None
        try:
            body = r.json()
            if body.get("status") != 200:
                return None
            server_soup = BeautifulSoup(body.get("result", ""), "lxml")
            link_ids = [el.get("data-link-id", "") for el in server_soup.select("[data-link-id]") if el.get("data-link-id")]
            if not link_ids:
                return None

            # Step 2: server?get= → get embed URL
            for link_id in link_ids[:3]:
                stream_url = f"{self.base_url}/ajax/server?get={link_id}"
                r2 = self._fetch(stream_url, headers={"X-Requested-With": "XMLHttpRequest", "Referer": referer})
                if not r2 or r2.status_code != 200:
                    continue
                try:
                    body2 = r2.json()
                    embed = body2.get("result", {}).get("url", "")
                    if not embed:
                        continue

                    # Step 3: extract m3u8 from embed (megaclone chain + generic fallback)
                    m3u8_url, subtitles = self._extract_m3u8_from_embed(embed)
                    return {
                        "name": "ajax_server_stream",
                        "method": "ajax_stream",
                        "url": (m3u8_url or embed)[:120],
                        "is_direct": bool(m3u8_url),
                        "detail": f"Anikoto full chain: data_ids→server→embed→{'m3u8' if m3u8_url else 'embed'}",
                        "embed_url": embed,
                        "m3u8_url": m3u8_url,
                        "subtitles": subtitles,
                    }
                except (json.JSONDecodeError, ValueError, AttributeError):
                    continue
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass
        return None

    def _try_ajax_episode_api(self, sample_url: str) -> Optional[dict]:
        """Try AJAX episode API endpoints discovered from the page or by pattern."""
        # 1. Scan the sample URL for AJAX endpoint hints
        ajax_endpoints = self._scan_ajax_endpoints(sample_url)

        # 2. Try discovered endpoints
        for ep in ajax_endpoints:
            if "episode" in ep["endpoint"].lower():
                full_url = f"{self.base_url}{ep['endpoint']}"
                r = self._fetch(full_url, headers=ep["headers"])
                if r and r.status_code == 200:
                    try:
                        data = r.json()
                        if isinstance(data, dict) and data.get("status") == 200:
                            eps = _find_json_array(data)
                            if eps and len(eps) >= 1:
                                self.result.episode_count = len(eps)
                                self.result.sample_episode_url = sample_url
                                return {
                                    "use_generic": False,
                                    "api_endpoint": ep["endpoint"],
                                    "ajax": True,
                                    "requires_xrw": True,
                                }
                    except (json.JSONDecodeError, ValueError):
                        pass

        # 3. Try known AJAX episode patterns with media IDs from search results
        html_ajax_patterns = [p for p in EPISODE_API_PATTERNS if p.response_type == "json_html"]
        for mid in self._media_ids[:5]:
            for pat in html_ajax_patterns:
                tmpl = pat.path
                url = f"{self.base_url}{tmpl.replace('{id}', mid)}"
                r = self._fetch(url, headers={"X-Requested-With": "XMLHttpRequest",
                                              "Referer": sample_url})
                if r and r.status_code == 200:
                    try:
                        data = r.json()
                        if isinstance(data, dict) and data.get("status") == 200:
                            soup = BeautifulSoup(data.get("result", ""), "lxml")
                            links = soup.select("a[data-ids], a[data-num]")
                            if links and len(links) >= 1:
                                self.result.episode_count = len(links)
                                self.result.episode_urls_found = [
                                    l.get("href", "") or sample_url for l in links[:5]
                                ]
                                self.result.sample_episode_url = sample_url
                                # Collect data-ids for stream/server discovery
                                for l in links[:5]:
                                    did = l.get("data-ids", "")
                                    if did:
                                        self._episode_data_ids.append(did)
                                    mal = l.get("data-mal", "")
                                    slug = l.get("data-slug", "")
                                    ts = l.get("data-timestamp", "")
                                    if mal and slug and ts:
                                        self._episode_mapper_data.append({"mal": mal, "slug": slug, "ts": ts})
                                return {
                                    "use_generic": False,
                                    "api_endpoint": tmpl,
                                    "ajax": True,
                                    "requires_xrw": True,
                                }
                    except (json.JSONDecodeError, ValueError):
                        pass

        # 4. Try known AJAX episode patterns with extracted IDs from URL
        for id_candidate in _extract_ids(sample_url)[:3]:
            for pat in html_ajax_patterns:
                tmpl = pat.path
                url = f"{self.base_url}{tmpl.replace('{id}', id_candidate)}"
                r = self._fetch(url, headers={"X-Requested-With": "XMLHttpRequest",
                                              "Referer": sample_url})
                if r and r.status_code == 200:
                    try:
                        data = r.json()
                        if isinstance(data, dict) and data.get("status") == 200:
                            soup = BeautifulSoup(data.get("result", ""), "lxml")
                            links = soup.select("a[data-ids], a[data-num]")
                            if links and len(links) >= 1:
                                self.result.episode_count = len(links)
                                self.result.episode_urls_found = [
                                    l.get("data-ids", "") for l in links[:5]
                                ]
                                self.result.sample_episode_url = sample_url
                                for l in links[:5]:
                                    did = l.get("data-ids", "")
                                    if did:
                                        self._episode_data_ids.append(did)
                                    mal = l.get("data-mal", "")
                                    slug = l.get("data-slug", "")
                                    ts = l.get("data-timestamp", "")
                                    if mal and slug and ts:
                                        self._episode_mapper_data.append({"mal": mal, "slug": slug, "ts": ts})
                                return {
                                    "use_generic": False,
                                    "api_endpoint": tmpl,
                                    "ajax": True,
                                    "requires_xrw": True,
                                }
                    except (json.JSONDecodeError, ValueError):
                        pass

        return None

    # ------------------------------------------------------------------
    # PARALLEL SEARCH
    # ------------------------------------------------------------------

    def _try_search_url(self, pat: SearchPattern, url: str) -> Optional[tuple[dict, float, bool, str]]:
        """Try a single search URL, return (config, score, is_json, debug_label) or None."""
        params = {k: v.replace("{query}", self.test_query) for k, v in pat.params.items()}
        headers = _resolve_headers(pat.headers, url)

        params_str = "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
        label = f"{pat.path}?{params_str}" if params_str else pat.path

        start = time.time()
        r: Optional[requests.Response] = None
        try:
            if pat.method == "POST":
                body = _resolve_body(pat.body_template, self.test_query) if pat.body_template else {"keyword": self.test_query}
                r = self._fetch_post(url, json_body=body, headers=headers)
            else:
                if params:
                    url_with_params = f"{url}?{urllib.parse.urlencode(params)}"
                else:
                    url_with_params = url
                r = self._fetch(url_with_params, headers=headers)
        except Exception as e:
            self.result.search_failed_attempts.append({
                "path": url, "method": pat.method,
                "error": str(e), "time": time.time() - start,
            })
            return None

        if not self._is_valid_response(r):
            st = str(r.status_code) if r else "no_response"
            self.result.search_failed_attempts.append({
                "path": url, "method": pat.method,
                "status": st, "time": time.time() - start,
            })
            return None

        if self._is_cloudflare(r):
            self.result.auth_type = "cloudflare"
            self.result.auth_detail = "Cloudflare protection detected"
            return None

        if pat.json_response or self._is_json_response(r):
            cfg = self._analyze_json_search(r, pat)
            if cfg:
                count = cfg.get("_item_count", 0)
                score = self._score_search_config(cfg, found_items=count, path=pat.path)
                return (cfg, score, True, label)

        cfg = self._analyze_html_search(r, pat)
        if cfg:
            count = cfg.get("_item_count", 0)
            score = self._score_search_config(cfg, found_items=count, path=pat.path)
            return (cfg, score, False, label)

        return None

    # ------------------------------------------------------------------ 
    # SEARCH DISCOVERY
    # ------------------------------------------------------------------ 

    def _build_urls(self, pat: SearchPattern) -> list[str]:
        """Build candidate URLs for a search pattern, including API subdomains."""
        urls = []

        # Primary: base URL + path
        urls.append(f"{self.base_url}{pat.path}")

        # For API paths, try on common API subdomains
        if "/api/" in pat.path or pat.json_response or pat.method == "POST":
            domain_parts = self.parsed.netloc.split(".")
            if len(domain_parts) >= 2:
                base_domain = ".".join(domain_parts[-2:]) if domain_parts[-1] in (
                    "com", "org", "net", "io", "tv", "co", "cc", "app", "to"
                ) else ".".join(domain_parts[-3:])
                # First HEAD-check the base domain to avoid DNS dead ends
                live_subdomains = [""]
                test_url = f"https://{base_domain}/"
                hr = self._head(test_url)
                if hr:
                    for sub in API_SUBDOMAINS[:10]:
                        api_url = f"https://{sub}.{base_domain}{pat.path}"
                        # Quick check if subdomain resolves
                        shr = self._head(f"https://{sub}.{base_domain}/")
                        if shr:
                            live_subdomains.append(sub)
                            urls.append(api_url)
                        else:
                            pass  # subdomain doesn't resolve, skip
        return urls

    def discover_search(self, max_attempts: int = 40) -> Optional[dict]:
        best: Optional[dict] = None
        best_score = 0
        done = False

        # Build all (pat, url) pairs
        all_tasks: list[tuple[SearchPattern, str]] = []
        for pat in SEARCH_PATTERNS:
            if done:
                break
            candidate_urls = self._build_urls(pat)
            for url in candidate_urls:
                if len(all_tasks) >= max_attempts:
                    break
                all_tasks.append((pat, url))

        if not all_tasks:
            self._log("SEARCH", "✗ NOT FOUND", "FAIL", "no URLs to try")
            return None

        self._log("SEARCH", f"Trying {len(all_tasks)} URLs in parallel...", "BUSY")

        # Try in parallel — 8 workers
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures: dict[concurrent.futures.Future, tuple[SearchPattern, str]] = {}
            for pat, url in all_tasks:
                f = pool.submit(self._try_search_url, pat, url)
                futures[f] = (pat, url)

            for f in concurrent.futures.as_completed(futures):
                pat, url = futures[f]
                try:
                    result = f.result(timeout=15)
                except Exception as e:
                    if not done:
                        self._log("SEARCH", url, "ERR", str(e)[:50])
                    continue

                if result is None:
                    continue

                cfg, score, is_json, label = result
                method_label = "JSON" if is_json else "HTML"
                count = cfg.get("_item_count", 0)
                sel = cfg.get("result_selector", "")

                if not done:
                    if is_json:
                        self._log("SEARCH", label, "JSON", f"{count} items, score={score:.1f}")
                    else:
                        self._log("SEARCH", label, "HTML", f"selector={sel}, {count} items, score={score:.1f}")

                # Only replace best if score is higher (or equal with non-root path)
                is_root = pat.path == "/"
                if score > best_score or (score == best_score and not is_root and best_score > 0):
                    best = cfg
                    best_score = score
                    self.result.search_found_methods.append(
                        f"{method_label}: {label} (score={score})"
                    )

                # Don't early-exit on root-path hits — they're often false positives
                if score >= 0.8 and not is_root:
                    done = True
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

        if best:
            self.result.search_config = {k: v for k, v in best.items() if not k.startswith("_")}
            method = "JSON API" if self.result.detected_json else "HTML scraping"
            self._log("SEARCH", "✓ FOUND", "OK", method)
        else:
            self._log("SEARCH", "✗ NOT FOUND", "FAIL", "no working search pattern")
        return self.result.search_config

    def _analyze_json_search(self, r: requests.Response,
                             pat: SearchPattern) -> Optional[dict]:
        try:
            data = r.json()
        except (json.JSONDecodeError, ValueError):
            return None

        items = _find_json_array(data)
        if not items:
            return None

        # Build config from successful pattern
        cfg: dict = {
            "url": pat.path,
            "params": dict(pat.params),
            "method": pat.method,
            "_item_count": len(items),
        }

        # Try to determine if movie/tv type param is needed
        if "{type}" in str(pat.params) or any(k == "type" for k in pat.params):
            pass  # keep as-is

        # For POST, check if body template was used
        if pat.method == "POST" and pat.body_template:
            cfg["body"] = pat.body_template

        return cfg

    def _analyze_html_search(self, r: requests.Response,
                             pat: SearchPattern) -> Optional[dict]:
        soup = BeautifulSoup(r.text, "lxml")
        best_sel = None
        best_count = 0
        best_sample = ""

        for sel in SEARCH_RESULT_SELECTORS:
            elements = soup.select(sel)
            if not elements:
                continue
            count = len(elements)
            if count < 2:
                continue
            # Score: more elements is better, but not too many (could be nav links)
            score = count
            if count > 50:
                score = count // 2  # penalize over-broad selectors

            # Check if elements contain links and text (likely content items)
            link_count = sum(1 for el in elements[:10] if el.find("a"))
            text_items = [el.get_text(strip=True) for el in elements[:10]]
            avg_len = sum(len(t) for t in text_items) / max(len(text_items), 1)
            if link_count < 2 and avg_len < 5:
                continue  # not content items

            # Prefer selectors that match info pages (mid, anime, series) over watch pages
            path_bonus = 0
            if any(kw in sel for kw in ("/mid/", "/anime/", "/series/", "/title/")):
                path_bonus = 3
            elif any(kw in sel for kw in ("/watch/", "/episode/", "/ep-", "/e/")):
                path_bonus = 1

            if score + path_bonus > best_count:
                best_sel = sel
                best_count = score + path_bonus
                best_sample = elements[0].get_text(strip=True)[:80] if elements else ""

        if not best_sel:
            return None

        # Determine title and link extraction
        title_sel = self._find_title_selector(soup, best_sel)
        link_attr = self._detect_link_attribute(soup, best_sel)
        image_sel = self._find_image_selector(soup, best_sel)

        cfg = {
            "url": pat.path,
            "params": dict(pat.params),
            "result_selector": best_sel,
            "title_from": "text",
            "link_attr": link_attr or "href",
            "_item_count": best_count,
            "_sample": best_sample,
        }
        if title_sel and title_sel != best_sel:
            cfg["title_selector"] = title_sel
        if image_sel:
            cfg["image_attr"] = "src"

        self.result.detected_selectors.append(best_sel)
        return cfg

    def _find_title_selector(self, soup: BeautifulSoup,
                             container_sel: str) -> Optional[str]:
        containers = soup.select(container_sel)
        if not containers:
            return None
        for sel in SEARCH_TITLE_SELECTORS:
            matches = sum(1 for c in containers[:5] if c.select_one(sel))
            if matches >= 3:
                return sel
        return "a"

    def _detect_link_attribute(self, soup: BeautifulSoup,
                               container_sel: str) -> str:
        containers = soup.select(container_sel)
        if not containers:
            return "href"
        for c in containers[:5]:
            a = c.find("a")
            if a:
                for attr in ("href", "data-url", "data-link", "data-src"):
                    if a.get(attr):
                        return attr
        return "href"

    def _find_image_selector(self, soup: BeautifulSoup,
                             container_sel: str) -> Optional[str]:
        containers = soup.select(container_sel)
        if not containers:
            return None
        for c in containers[:5]:
            for sel in SEARCH_IMAGE_SELECTORS:
                img = c.select_one(sel)
                if img and (img.get("src") or img.get("data-src")):
                    return sel
        return None

    def _score_search_config(self, cfg: Optional[dict],
                             found_items: int = 0, path: str = "") -> float:
        if cfg is None:
            return 0.0
        score = 0.5
        if found_items >= 5:
            score += 0.3
        if found_items >= 20:
            score += 0.2
        if cfg.get("result_selector"):
            score += 0.1
        if cfg.get("title_selector"):
            score += 0.05
        # Use provided path or fall back to cfg
        p = path or cfg.get("url", "")
        if p != "/":
            score += 0.05
        if "filter" in p or "search" in p or "ajax" in p:
            score += 0.05
        # Penalize root path slightly so specific paths win tiebreaks
        if p == "/":
            score -= 0.02
        return min(score, 1.0)

    # ------------------------------------------------------------------ 
    # EPISODE DISCOVERY
    # ------------------------------------------------------------------ 

    def discover_episodes(self) -> Optional[dict]:
        if not self.result.search_config:
            self._log("EPISODES", "SKIP", "SKIP", "no search config")
            return None

        # Get a sample search result URL
        self._log("EPISODES", "Fetching sample result...", "BUSY")
        sample_url = self._find_sample_result_url()
        if not sample_url:
            self._log("EPISODES", "✗ NOT FOUND", "FAIL", "could not get sample search URL")
            return None

        self.result.sample_search_url = sample_url
        self._log("EPISODES", sample_url, "SAMPLE")

        # Try AJAX episode API first (Anikoto-style)
        self._log("EPISODES", "AJAX episode API...", "BUSY")
        eps_cfg = self._try_ajax_episode_api(sample_url)
        if eps_cfg:
            self.result.episodes_config = eps_cfg
            api = eps_cfg.get("api_endpoint", "?")
            self._log("EPISODES", f"✓ AJAX API: {api}", "OK", f"{self.result.episode_count} episodes")
            return eps_cfg
        self._log("EPISODES", "AJAX API: no matches", "FAIL")

        # Try generic episode extraction
        self._log("EPISODES", "Generic extractor...", "BUSY")
        from anime_watch.core import fetch_episodes_generic
        eps = fetch_episodes_generic(sample_url, "DiscoveryTest")
        if eps:
            self.result.episode_count = len(eps)
            self.result.episode_urls_found = [e.url for e in eps[:5]]
            self.result.sample_episode_url = eps[0].url if eps else None
            self.result.episodes_config = {"use_generic": True}
            self._log("EPISODES", f"✓ {len(eps)} episodes", "OK", f"sample: {eps[0].url[:60]}")
            return self.result.episodes_config
        self._log("EPISODES", "Generic: no matches", "FAIL")

        # Try scraping the page for episode links manually
        self._log("EPISODES", "CSS selectors + URL patterns...", "BUSY")
        resp = self._fetch(sample_url)
        if resp and self._is_valid_response(resp):
            soup = BeautifulSoup(resp.text, "lxml")
            found_links = set()

            # Try CSS selectors
            for sel in EPISODE_SELECTORS:
                for el in soup.select(sel):
                    href = el.get("href", "") or el.get("data-href", "")
                    if href and href not in found_links:
                        found_links.add(href)
            if found_links:
                self._log("EPISODES", f"CSS selectors: {len(found_links)} links", "PARTIAL")

            # Try data attributes
            for attr in EPISODE_DATA_ATTRS:
                for el in soup.select(f"[{attr}]"):
                    val = el.get(attr, "")
                    if val and val not in found_links:
                        found_links.add(val)
            if found_links:
                self._log("EPISODES", f"+ data attrs: {len(found_links)} total", "PARTIAL")

            # Try URL pattern matching (any link matching episode patterns)
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if any(re.search(p, href) for p in EPISODE_URL_PATTERNS):
                    if href not in found_links:
                        found_links.add(href)

            if found_links:
                self.result.episode_count = len(found_links)
                self.result.episode_urls_found = list(found_links)[:5]
                self.result.sample_episode_url = list(found_links)[0]
                full_url = urllib.parse.urljoin(self.base_url, list(found_links)[0])
                self.result.sample_episode_url = full_url

                # Build selector from found patterns
                best_sel = self._find_best_episode_selector(soup)
                self.result.episodes_config = {
                    "use_generic": False,
                    "result_selector": best_sel or "a[href*='/episode/']",
                }
                self._log("EPISODES", f"✓ {len(found_links)} episodes", "OK",
                          f"selector={best_sel}, sample={full_url[:60]}")
                return self.result.episodes_config
        self._log("EPISODES", "CSS/URL patterns: no matches", "FAIL")

        # Try legacy API-based episode discovery
        self._log("EPISODES", "Legacy API endpoints...", "BUSY")
        eps_cfg = self._try_episode_api(sample_url)
        if eps_cfg:
            self.result.episodes_config = eps_cfg
            api = eps_cfg.get("api_endpoint", "?")
            self._log("EPISODES", f"✓ API: {api}", "OK", f"{self.result.episode_count} episodes")
            return eps_cfg
        self._log("EPISODES", "Legacy API: no matches", "FAIL")

        # Failed — try common episode page URL patterns
        self._log("EPISODES", "Brute-force episode URL patterns...", "BUSY")
        for tmpl in ["/episode/1", "/ep-1", "/watch/1", "/ep/1"]:
            url = f"{self.base_url}{tmpl}"
            r = self._fetch(url)
            if r and r.status_code == 200 and len(r.text) > 200:
                self.result.episode_urls_found.append(url)
                self.result.sample_episode_url = url
                self.result.episodes_config = {"use_generic": True}
                self._log("EPISODES", f"✓ {url}", "OK", "brute-force match")
                return self.result.episodes_config
            self._log("EPISODES", f"  {url}", str(r.status_code) if r else "no_response")

        self._log("EPISODES", "✗ NOT FOUND", "FAIL", "all methods exhausted")
        return None

    def _find_sample_result_url(self) -> Optional[str]:
        if not self.result.search_config:
            return None
        cfg = self.result.search_config
        url = f"{self.base_url}{cfg.get('url', '/search')}"
        params = {k: v.replace("{query}", self.test_query) for k, v in cfg.get("params", {}).items()}

        if cfg.get("method") == "POST":
            body = cfg.get("body", {"keyword": self.test_query})
            r = self._fetch_post(url, json_body=body)
        else:
            url_with_params = f"{url}?{urllib.parse.urlencode(params)}" if params else url
            r = self._fetch(url_with_params)

        if not r or not self._is_valid_response(r):
            return None

        # Collect media IDs from results page (for AJAX episode/server API calls)
        soup = BeautifulSoup(r.text, "lxml")
        for attr in SEARCH_DATA_ATTRS:
            for el in soup.select(f"[{attr}]"):
                val = el.get(attr, "")
                if val and val.strip():
                    self._media_ids.append(val.strip())

        # Try JSON first
        try:
            data = r.json()
            items = _find_json_array(data)
            if items:
                first = items[0]
                for key in ("url", "link", "href", "slug", "id", "permalink", "path"):
                    val = first.get(key)
                    if val:
                        if val.startswith("http"):
                            return val
                        return urllib.parse.urljoin(self.base_url, val)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try HTML
        sel = cfg.get("result_selector", "a")
        containers = soup.select(sel)

        # If the broad selector found too many items (nav + results), try specific sub-selectors
        if len(containers) > 30:
            for narrow_sel in [".main .item", ".poster a", ".thumb a", "article", ".result a"]:
                narrowed = soup.select(narrow_sel)
                if 2 <= len(narrowed) <= 30:
                    containers = narrowed
                    break

        if containers:
            best_url = None
            for c in containers[:10]:
                text = c.get_text(strip=True)[:20]
                # Skip nav/menu items
                if text.lower() in ("home", "movies", "anime", "news", "contact", "about", "login", "register", "sign in", "sign up", ""):
                    continue
                # Try to find a direct link first
                a = c.find("a") if c.name != "a" else c
                if a:
                    href = a.get("href") or a.get("data-url") or a.get("data-link") or ""
                    if href and href not in ("/", "/home", "#"):
                        full = urllib.parse.urljoin(self.base_url, href)
                        # Prefer mid/anime/series URLs over watch/episode URLs
                        if "/mid/" in full or "/anime/" in full or "/series/" in full:
                            return full
                        if not best_url:
                            best_url = full

            if best_url:
                return best_url

            # No anchor found — try constructing URL from data-tip (Anikoto-style)
            for c in containers[:10]:
                poster = c.select_one(".poster, [data-tip]")
                if poster:
                    media_id = poster.get("data-tip", "") or poster.get("data-id", "")
                    if media_id:
                        return f"{self.base_url}/mid/{media_id}"
        return None

    def _find_best_episode_selector(self, soup: BeautifulSoup) -> str:
        best_sel = EPISODE_SELECTORS[0]
        best_count = 0
        for sel in EPISODE_SELECTORS:
            count = len(soup.select(sel))
            if count > best_count:
                best_count = count
                best_sel = sel
        return best_sel

    def _try_episode_api(self, sample_url: str) -> Optional[dict]:
        """Try common episode API endpoint patterns from EPISODE_API_PATTERNS."""
        rest_patterns = [p for p in EPISODE_API_PATTERNS if p.response_type == "json" and p.source == "builtin"]
        for id_candidate in _extract_ids(sample_url):
            for pat in rest_patterns:
                tmpl = pat.path
                url = f"{self.base_url}{tmpl.replace('{id}', id_candidate)}"
                headers = {"X-Requested-With": "XMLHttpRequest"} if pat.ajax else None
                r = self._fetch(url, headers=headers)
                if r and r.status_code == 200:
                    try:
                        data = r.json()
                        if isinstance(data, dict):
                            eps = _find_json_array(data)
                            if eps and len(eps) >= 2:
                                self.result.episode_count = len(eps)
                                self.result.episode_urls_found = [
                                    str(e.get("url", e.get("id", ""))) for e in eps[:5]
                                ]
                                self.result.sample_episode_url = sample_url
                                return {"use_generic": False, "api_endpoint": tmpl}
                    except (json.JSONDecodeError, ValueError):
                        pass
        return None

    # ------------------------------------------------------------------ 
    # STREAM DISCOVERY
    # ------------------------------------------------------------------ 

    def discover_stream(self) -> Optional[dict]:
        episode_url = self.result.sample_episode_url
        if not episode_url:
            self._log("STREAM", "SKIP", "SKIP", "no episode URL")
            return None

        self._log("STREAM", f"Episode: {episode_url}", "BUSY")

        strategies_to_try = STREAM_STRATEGIES[:]
        found: list[dict] = []

        for strategy in strategies_to_try:
            self._log("STREAM", f"Trying {strategy.name}...", "BUSY")
            result = self._try_stream_strategy(strategy, episode_url)
            if result:
                u = result.get("url", "")
                detail = result.get("detail", result.get("note", ""))
                self._log("STREAM", f"✓ {strategy.name}", "OK",
                          f"{u[:60]}{' | '+detail if detail else ''}")
                found.append(result)
                break
            else:
                self._log("STREAM", f"✗ {strategy.name}", "FAIL")

        if found:
            self.result.stream_found_methods = found

            # Pick best strategy, preserving actual method name
            best_strat_name = found[0].get("name", "")
            actual_method = found[0].get("method", "scrape")
            for s in STREAM_STRATEGIES:
                if s.name == best_strat_name:
                    self.result.stream_config = dict(s.config_template)
                    self.result.stream_config["type"] = actual_method
                    if s.method == "iframe":
                        iframe_sel = self._detect_iframe_selector(episode_url)
                        if iframe_sel:
                            self.result.stream_config["iframe_selector"] = iframe_sel
                    break
            method = self.result.stream_found_methods[0].get("method", "?")
            self._log("STREAM", f"✓ FOUND", "OK", f"method={method}")
            return self.result.stream_config

        self._log("STREAM", "✗ NOT FOUND", "FAIL", "all strategies exhausted")
        return None

    def _try_stream_strategy(self, strategy: StreamStrategy,
                             url: str) -> Optional[dict]:
        try:
            if strategy.method == "ajax_stream":
                result = self._try_ajax_stream_api(url)
                if result:
                    result["name"] = strategy.name
                    return result

            elif strategy.method == "api_language_stream":
                result = self._try_api_language_stream(url)
                if result:
                    result["name"] = strategy.name
                    return result

            elif strategy.method == "scrape":
                s = scrape_page_for_video(url, "DiscoveryTest")
                if s and s.url:
                    return {
                        "name": strategy.name,
                        "method": "scrape",
                        "url": s.url[:120],
                        "is_direct": s.is_direct,
                    }

            elif strategy.method == "ytdlp":
                s = extract_with_ytdlp(url)
                if s and s.url:
                    return {
                        "name": strategy.name,
                        "method": "ytdlp",
                        "url": s.url[:120],
                        "is_direct": s.is_direct,
                    }

            elif strategy.method == "iframe":
                return self._try_iframe_extraction(url, strategy)

            elif strategy.method == "m3u8_in_page":
                return self._try_m3u8_extraction(url, strategy)

            elif strategy.method == "data_attributes":
                return self._try_data_attribute_extraction(url, strategy)

        except Exception as e:
            self.result.stream_failed_attempts.append({
                "name": strategy.name, "error": str(e),
            })
        return None

    def _try_iframe_extraction(self, url: str,
                               strategy: StreamStrategy) -> Optional[dict]:
        r = self._fetch(url)
        if not r or not self._is_valid_response(r):
            return None
        soup = BeautifulSoup(r.text, "lxml")

        # Try all iframe selectors
        for sel in ["iframe[src]", "iframe", "frame[src]"]:
            iframes = soup.select(sel)
            for ifr in iframes:
                src = ifr.get("src", "")
                if not src:
                    continue
                if not src.startswith("http"):
                    src = urllib.parse.urljoin(url, src)

                # Check known embed hosts
                host = urllib.parse.urlparse(src).netloc.lower()
                is_known = any(h in host for h in EMBED_HOSTS)
                detail = f"known_host={host}" if is_known else f"unknown_host={host}"

                # Try to get stream from iframe
                s = scrape_page_for_video(src, "DiscoveryTest")
                if s and s.url:
                    return {
                        "name": strategy.name,
                        "method": "iframe",
                        "url": s.url[:120],
                        "is_direct": s.is_direct,
                        "iframe_src": src[:80],
                        "detail": detail,
                    }

                # If scrape failed, try yt-dlp
                s2 = extract_with_ytdlp(src)
                if s2 and s2.url:
                    return {
                        "name": "iframe+ytdlp",
                        "method": "iframe",
                        "url": s2.url[:120],
                        "is_direct": s2.is_direct,
                        "iframe_src": src[:80],
                        "detail": f"{detail} + ytdlp",
                    }

        return None

    def _try_m3u8_extraction(self, url: str,
                             strategy: StreamStrategy) -> Optional[dict]:
        r = self._fetch(url)
        if not r or not self._is_valid_response(r):
            return None

        found_urls = []
        for pattern in [
            r'(https?://[^"\'<> ]+\.m3u8[^"\'<> ]*)',
            r'(https?://[^"\'<> ]+\.mp4[^"\'<> ]*)',
            r"'(/[^']+\.m3u8[^']*)'",
        ]:
            for match in re.finditer(pattern, r.text, re.IGNORECASE):
                u = match.group(1)
                if u.startswith("/"):
                    u = urllib.parse.urljoin(url, u)
                if u not in found_urls:
                    found_urls.append(u)
                if len(found_urls) >= 3:
                    break

        if found_urls:
            # Try first found URL
            for u in found_urls:
                s = extract_with_ytdlp(u)
                if s and s.url:
                    return {
                        "name": strategy.name,
                        "method": "m3u8_in_page",
                        "url": s.url[:120],
                        "is_direct": True,
                        "found_urls": found_urls,
                    }
            # Even if yt-dlp fails, report that m3u8 URLs were found
            return {
                "name": strategy.name,
                "method": "m3u8_in_page",
                "url": found_urls[0][:120],
                "is_direct": True,
                "note": "URL found in page, yt-dlp did not confirm",
            }
        return None

    def _try_data_attribute_extraction(self, url: str,
                                        strategy: StreamStrategy) -> Optional[dict]:
        r = self._fetch(url)
        if not r or not self._is_valid_response(r):
            return None
        soup = BeautifulSoup(r.text, "lxml")

        for attr in ("data-src", "data-url", "data-video", "data-source", "data-href", "data-link"):
            for el in soup.select(f"[{attr}]"):
                val = el.get(attr, "")
                if not val:
                    continue
                if val.startswith("http") and val.endswith((".mp4", ".m3u8", ".ts")):
                    return {
                        "name": strategy.name,
                        "method": "data_attributes",
                        "url": val[:120],
                        "is_direct": True,
                        "attr": attr,
                    }
                # Also check if it looks like a stream URL (not the same as page URL)
                if val.startswith("http") and val != url and url not in val:
                    if any(kw in val.lower() for kw in ("/stream/", "/video/", "/hls/", "/play/", ".m3u8", ".mp4", "manifest")):
                        if not val.startswith("http"):
                            val = urllib.parse.urljoin(url, val)
                        return {
                            "name": strategy.name,
                            "method": "data_attributes",
                            "url": val[:120],
                            "is_direct": True,
                            "attr": attr,
                        }
        return None

    def _detect_iframe_selector(self, url: str) -> Optional[str]:
        r = self._fetch(url)
        if not r or not self._is_valid_response(r):
            return None
        soup = BeautifulSoup(r.text, "lxml")

        # Check if there's a specific pattern for embed iframes
        for sel in ["iframe[src*='embed']", "iframe[src*='player']",
                     "iframe[src*='video']", "iframe[src*='watch']"]:
            if soup.select(sel):
                return sel
        if soup.select("iframe[src]"):
            return "iframe[src]"
        return None

    # ------------------------------------------------------------------ 
    # SUBTITLE DISCOVERY
    # ------------------------------------------------------------------ 

    def discover_subtitles(self):
        """Try to discover subtitle endpoints and patterns."""
        episode_url = self.result.sample_episode_url
        if not episode_url:
            self._log("SUBS", "SKIP", "SKIP", "no episode URL")
            return

        self._log("SUBS", episode_url, "BUSY")
        found_methods = []

        # 1. Check page for subtitle file links
        r = self._fetch(episode_url)
        if r and self._is_valid_response(r):
            for ext in (".vtt", ".srt", ".ass", ".ssa", ".sub"):
                for match in re.finditer(
                        rf'(https?://[^"\'<> ]+{re.escape(ext)}[^"\'<> ]*)',
                        r.text, re.IGNORECASE):
                    found_methods.append(f"page_link_{ext}: {match.group(1)[:80]}")
                    break
                if found_methods:
                    break

            if found_methods:
                self._log("SUBS", f"✓ page links: {found_methods[0][:80]}", "OK")
            else:
                self._log("SUBS", "No subtitle file links on page", "FAIL")

            # 2. Check for subtitle API calls in script tags
            soup = BeautifulSoup(r.text, "lxml")
            for sc in soup.select("script"):
                t = sc.string or ""
                for match in re.finditer(r'(/api/(?:subtitle|sub|caption)[^"\' ]*)', t):
                    found_methods.append(f"script_api: {match.group(1)}")
                    break
            api_matches = [m for m in found_methods if m.startswith("script_api")]
            if api_matches:
                self._log("SUBS", f"✓ script API: {api_matches[0]}", "OK")
            else:
                self._log("SUBS", "No subtitle API in scripts", "PARTIAL")

        # 3. Try known subtitle API endpoints
        base = self.base_url
        endpoints_to_try = [
            f"{base}/api/subtitles/",
            f"{base}/api/captions/",
            f"{base}/api/v1/subtitles/",
            f"{base}/subtitles/",
        ]
        for ep in endpoints_to_try:
            r = self._fetch(ep)
            status = r.status_code if r else "no_response"
            if r and status in (200, 201, 403):
                found_methods.append(f"api_endpoint: {ep}")
                self._log("SUBS", f"✓ endpoint: {ep}", "OK", f"HTTP {status}")
                break
            else:
                self._log("SUBS", f"  {ep}", str(status))
        else:
            self._log("SUBS", "No common API endpoints found", "FAIL")

        # 4. If stream was discovered via iframe, check embed for captions
        for m in self.result.stream_found_methods:
            iframe_src = m.get("iframe_src", "")
            if iframe_src:
                self._log("SUBS", f"Embed captions: {iframe_src[:90]}", "BUSY")
                r = self._fetch(iframe_src)
                if r and self._is_valid_response(r):
                    try:
                        data = json.loads(r.text)
                        tracks = []
                        if isinstance(data, dict):
                            tracks = data.get("tracks", data.get("captions",
                                                    data.get("subtitles", [])))
                        if tracks:
                            found_methods.append(f"embed_captions: {len(tracks)} tracks found")
                            self._log("SUBS", f"✓ {len(tracks)} caption tracks", "OK")
                    except (json.JSONDecodeError, ValueError):
                        if "#EXT-X-MEDIA:TYPE=SUBTITLES" in r.text:
                            found_methods.append("hls_subtitle_tracks")
                            self._log("SUBS", "✓ HLS subtitle tracks", "OK")
                        if "#EXT-X-MEDIA:TYPE=AUDIO" in r.text:
                            found_methods.append("hls_audio_tracks")
                            self._log("SUBS", "✓ HLS audio tracks", "OK")

        self.result.subtitle_methods_found = found_methods
        if found_methods:
            self._log("SUBS", f"✓ DONE: {len(found_methods)} method(s)", "OK")
        else:
            self._log("SUBS", "✗ No subtitle discovery", "FAIL")

    # ------------------------------------------------------------------ 
    # SERVER DISCOVERY
    # ------------------------------------------------------------------ 

    def discover_servers(self):
        """Try to detect if the site has multiple servers or quality options."""
        found_methods = []
        episode_url = self.result.sample_episode_url
        if not episode_url:
            self._log("SERVERS", "SKIP", "SKIP", "no episode URL")
            return

        self._log("SERVERS", episode_url, "BUSY")
        r = self._fetch(episode_url)
        if not r or not self._is_valid_response(r):
            st = str(r.status_code) if r else "no_response"
            self._log("SERVERS", st, "FAIL")
            return

        soup = BeautifulSoup(r.text, "lxml")

        # 1. Check for (Sub)/(Dub)/(HSub) in labels
        for el in soup.select("a, button, span, li, option"):
            text = el.get_text(strip=True)
            if re.search(r'\((?:Sub|Dub|HSub|SUB|DUB|HSUB)\)', text, re.IGNORECASE):
                found_methods.append("name_suffix: (Sub)/(Dub) detected in labels")
                self._log("SERVERS", "✓ (Sub)/(Dub) labels detected", "OK")
                break
        else:
            self._log("SERVERS", "No (Sub)/(Dub) labels", "PARTIAL")

        # 2. Check for quality/server selectors
        for sel in ["select", "[class*='quality']", "[class*='server']",
                     "[class*='audio']", "[id*='quality']", "[id*='server']"]:
            els = soup.select(sel)
            for el in els:
                options = el.select("option") if el.name == "select" else [el]
                if len(options) >= 2:
                    labels = [o.get_text(strip=True) for o in options[:5]]
                    labels_str = ", ".join(labels)[:100]
                    found_methods.append(f"quality_server_select: {el.name}.{el.get('class', '')} → [{labels_str}]")
                    self._log("SERVERS", f"✓ Select: [{labels_str}]", "OK")
                    break
            if found_methods:
                break
        else:
            self._log("SERVERS", "No quality/server selectors", "PARTIAL")

        # 3. Check for HLS media tracks
        if "^#EXT-X-MEDIA:" in (r.text or ""):
            for match in re.finditer(r'#EXT-X-MEDIA:TYPE=([^,\n]+)', r.text):
                found_methods.append(f"hls_media_track: {match.group(1)}")
                self._log("SERVERS", f"✓ HLS track: {match.group(1)}", "OK")

        self.result.server_methods_found = found_methods
        if found_methods:
            self._log("SERVERS", f"✓ DONE: {len(found_methods)} method(s)", "OK")
        else:
            self._log("SERVERS", "✗ No server detection", "FAIL")

    # ------------------------------------------------------------------ 
    # AUTH DISCOVERY
    # ------------------------------------------------------------------ 

    def discover_auth(self):
        """Detect authentication requirements."""
        # Already checked for Cloudflare during search
        if self.result.auth_type == "cloudflare":
            self._log("AUTH", "Cloudflare already detected", "OK")
            return

        self._log("AUTH", "Probing auth requirements...", "BUSY")

        # Test root with different header combos
        for i, headers in enumerate(HEADER_TEMPLATES):
            resolved = _resolve_headers(headers, self.base_url)
            r = self._fetch(f"{self.base_url}/", headers=resolved)
            status = r.status_code if r else "no_response"
            label = f"headers[{i}]"[:20]
            if r and status == 200:
                self._log("AUTH", f"{label} → 200 OK", "OK", "no auth required")
                self.result.auth_type = "none"
                self.result.auth_detail = "Site accessible without special headers"
                return
            if r and status in (403, 401, 407):
                www_auth = (r.headers.get("WWW-Authenticate", "") or "")
                if www_auth:
                    self._log("AUTH", f"HTTP auth: {www_auth[:60]}", "DETECTED")
                    self.result.auth_type = "http_auth"
                    self.result.auth_detail = f"WWW-Authenticate: {www_auth}"
                    return
                self._log("AUTH", f"{label} → {status}", "BUSY")
            else:
                self._log("AUTH", f"{label} → {status}", "BUSY")

        # Check if origin/referer headers help
        self._log("AUTH", "Trying origin/referer headers...", "BUSY")
        for headers in HEADER_TEMPLATES[1:]:
            resolved = _resolve_headers(headers, self.base_url)
            r = self._fetch(f"{self.base_url}/", headers=resolved)
            if r and r.status_code == 200:
                self._log("AUTH", "origin/referer OK", "OK")
                self.result.auth_type = "origin_referer"
                self.result.auth_detail = f"Needs: {resolved}"
                return

        # Check for token-based auth (look for x-user or token in headers)
        self._log("AUTH", "Checking for token-based auth...", "BUSY")
        r = self._fetch(f"{self.base_url}/")
        if r:
            req_headers = {k.lower(): v for k, v in r.request.headers.items()}
            for hdr_name in ("x-user", "x-client-token", "x-tr-signature", "authorization"):
                if hdr_name in req_headers:
                    self._log("AUTH", f"Token header: {hdr_name}", "DETECTED")
                    self.result.auth_type = "token_api"
                    self.result.auth_detail = f"Request header: {hdr_name}"
                    return

        self.result.auth_type = "unknown"
        self.result.auth_detail = "Could not determine auth mechanism"
        self._log("AUTH", "✗ Unknown auth mechanism", "FAIL")

    # ------------------------------------------------------------------ 
    # MAIN ENTRY
    # ------------------------------------------------------------------ 

    def discover_all(self) -> DiscoveryResult:
        start = time.time()
        print(f"\n{'='*70}")
        print(f"  Discovery: {self.base_url}")
        print(f"{'='*70}\n")
        try:
            self.result.auth_type = "none"
            self._warmup()
            print()
            self.discover_search()
            print()
            self.discover_episodes()
            print()
            self.discover_stream()
            print()
            self.discover_subtitles()
            print()
            self.discover_servers()
            print()
            self.discover_auth()
        except Exception as e:
            self.result.auth_detail = f"Discovery error: {e}"
        finally:
            self.result.elapsed = time.time() - start
            self.result.pages_fetched = self._pages
            print(f"\n{'='*70}")
            print(f"  Done — {self.result.pages_fetched} pages fetched in {self.result.elapsed:.1f}s")
            print(f"{'='*70}\n")
        return self.result


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _resolve_headers(headers: dict[str, str], base_url: str) -> dict[str, str]:
    return {k: v.replace("{base_url}", base_url) for k, v in headers.items()}


def _resolve_body(template: Optional[dict], query: str) -> dict:
    if template is None:
        return {"keyword": query}
    result = {}
    for k, v in template.items():
        if isinstance(v, str):
            result[k] = v.replace("{query}", query)
        else:
            result[k] = v
    return result


def _find_json_array(data: Any) -> list:
    """Find the first substantial array in JSON data."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Look for common result container keys
        for key in ("results", "data", "items", "hits", "entries", "list",
                     "documents", "records", "response", "anime", "movies",
                     "tv", "series", "shows", "videos", "media"):
            val = data.get(key)
            if isinstance(val, list) and len(val) >= 2:
                return val
        # Recursive search
        for val in data.values():
            if isinstance(val, (list, dict)):
                found = _find_json_array(val)
                if found:
                    return found
    return []


def _extract_ids(url: str) -> list[str]:
    """Extract numeric and alphanumeric IDs from a URL."""
    ids = []
    # Numeric IDs
    for m in re.finditer(r'/(\d+)(?:/|$|[?#])', url):
        ids.append(m.group(1))
    # Slug-style IDs (last path segment)
    path = urllib.parse.urlparse(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s and not s.isdigit()]
    if segments:
        ids.append(segments[-1])
    # TMDB-style IDs
    for m in re.finditer(r'[-/](\d+)(?:-|$)', url):
        ids.append(m.group(1))
    return list(set(ids))


def load_config(config_path: str) -> dict:
    """Load a JSON config file, or extract CONFIG dict from a .py file."""
    if config_path.endswith(".py"):
        import importlib.util
        mod_name = "_discover_config_" + os.path.splitext(os.path.basename(config_path))[0]
        spec = importlib.util.spec_from_file_location(mod_name, config_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "CONFIG"):
                return mod.CONFIG
        return {}
    with open(config_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run_discover(args):
    """Run the discovery tool with parsed CLI arguments."""
    url = args.url
    query = args.query or "naruto"
    output = args.output
    run_test = args.test

    print(f"Analyzing: {url}")
    print(f"Query:     {query}")
    print()

    engine = DiscoveryEngine(url, test_query=query)
    result = engine.discover_all()

    # Print summary
    print(result.summary())

    # Build site config
    name = args.name or ""
    slug = args.slug or ""
    site_config = result.to_site_config(name=name, slug=slug)

    # If discovery confidence is very low, show raw config
    confidence = min(
        result.search_confidence,
        result.episodes_confidence or 1.0,
        result.stream_confidence or 1.0,
    )
    has_essential = result.search_config is not None

    print()
    print("=" * 60)
    print("GENERATED CONFIG")
    print("=" * 60)
    print(json.dumps(site_config, indent=2))
    print()

    if not has_essential:
        print("⚠  WARNING: Search discovery failed — the generated config")
        print("   will not work without manual fixes.")
        print()

    if output:
        with open(output, "w") as f:
            json.dump(site_config, f, indent=2)
        print(f"Config saved to: {output}")
        print()
        print(f"Next: python -m anime_watch plugin generate {output}")
    else:
        print("Save this config to a file, then generate with:")
        print(f"  python -m anime_watch plugin discover {url} --output config.json")
        print(f"  python -m anime_watch plugin generate config.json")
    print()

    if run_test and has_essential:
        _run_discovered_plugin_test(result, output)

    return 0 if has_essential else 1


def _run_discovered_plugin_test(result: DiscoveryResult, config_path: Optional[str]):
    """If output config was saved, try to generate + test the plugin."""
    if not config_path:
        return
    try:
        from .generate import save_plugin
        from .validate import validate_source
        from .test import load_plugin, run_test

        out = save_plugin(config_path)
        print()
        print(f"Generated plugin: {out}")

        vr = validate_source(out)
        print(str(vr))

        if vr.passed:
            provider = load_plugin(out)
            if provider:
                print()
                print("Running automated test cycle...")
                ok = run_test(provider, query=result.test_query)
                if ok:
                    print()
                    print("✓ Plugin works! Install with:")
                    print(f"  python -m anime_watch plugin install {out}")
    except Exception as e:
        print(f"Plugin generation test skipped: {e}")


def add_arguments(parser):
    """Add discover subcommand arguments to an argparse subparser."""
    parser.add_argument("url", help="Base URL of the site to analyze (e.g. https://mysite.com)")
    parser.add_argument("--query", default="naruto",
                        help="Test query to use for search discovery (default: naruto)")
    parser.add_argument("--name", default="",
                        help="Display name for the site (auto-generated from URL if not set)")
    parser.add_argument("--slug", default="",
                        help="Slug for the provider (auto-generated from URL if not set)")
    parser.add_argument("-o", "--output", help="Save generated config to this file")
    parser.add_argument("--test", action="store_true",
                        help="After discovery, generate and test the plugin automatically")
