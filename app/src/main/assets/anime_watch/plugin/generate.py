"""Config-driven plugin generator.

Usage by AI agent:
  1. Explore the target site, discover CSS selectors & URL patterns
  2. Write a JSON config file describing the site structure
  3. Run: python -m anime_watch plugin generate config.json
  4. Validate & test the generated plugin
  5. Install it
"""

from __future__ import annotations
import json
import os
import re
from typing import Optional

_PLUGIN_TEMPLATE = '''\
"""
{name} — Auto-generated Anime Watch Provider Plugin
=====================================================
Generated from config: {config_desc}
"""

from __future__ import annotations
import json
import re
from typing import Optional
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from anime_watch.providers.base import BaseProvider
from anime_watch.models import SearchResult, Episode, StreamSource
from anime_watch.core import SESSION, SCRAPE_TIMEOUT


class {class_name}(BaseProvider):
    name = "{name}"
    slug = "{slug}"
    url = "{url}"
    category = "{category}"

{search_code}

{episodes_code}

{stream_code}

    def get_supported_qualities(self) -> list[str]:
        return ["best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub"]
'''


def _I(code: str | list[str], level: int) -> str:
    """Indent code line(s) by `level` (each level = 4 spaces).

    * str  → single line
    * list → joined by newline, each line indented (empty lines skipped)
    """
    pad = "    " * level
    if isinstance(code, str):
        return pad + code if code.strip() else code
    return "\n".join(
        pad + line if line.strip() else line
        for line in code
    )


def _escape(s: str) -> str:
    """Escape a string for safe inclusion in Python source."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _build_search(params: dict) -> str:
    """Generate the search() method body from config params."""
    url = params.get("url", "/search")
    param_def = params.get("params", {"q": "{query}"})
    method = params.get("method", "GET").upper()
    result_sel = params.get("result_selector", 'a[href*="/anime/"]')
    title_from = params.get("title_from", "text")
    link_attr = params.get("link_attr", "href")
    image_sel = params.get("image_selector", "")
    image_attr = params.get("image_attr", "src")
    headers_extra = params.get("headers", {})
    has_image = bool(image_sel)

    param_items = []
    for k, v in param_def.items():
        if "{query}" in v:
            param_items.append(f'"{k}": query')
        else:
            param_items.append(f'"{k}": "{_escape(v)}"')
    param_str = "{" + ", ".join(param_items) + "}" if param_items else "None"

    lines = []
    def L(text, level):
        lines.append(_I(text, level))

    L('def search(self, query: str) -> list[SearchResult]:', 1)
    L('results: list[SearchResult] = []', 2)
    L('', 0)
    L('try:', 2)
    L('ql = query.lower()', 3)
    L(f'resp = SESSION.{method.lower()}(', 3)
    L(f'urljoin(self.url, "{_escape(url)}"),', 4)
    if param_str != "None":
        L(f'params={param_str},', 4)
    if method == "POST":
        L(f'data={param_str},', 4)
    if headers_extra:
        h = ", ".join(f'"{k}": "{_escape(v)}"' for k, v in headers_extra.items())
        L(f'headers={{{h}}},', 4)
    L('timeout=SCRAPE_TIMEOUT,', 4)
    L(')', 4)
    L('if resp.status_code != 200:', 3)
    L('return results', 4)
    L('soup = BeautifulSoup(resp.text, "lxml")', 3)

    L(f'for item in soup.select("{_escape(result_sel)}"):', 3)
    if title_from == "text":
        L('title = item.get_text(strip=True) or item.get("title", "")', 4)
    else:
        L(f'title = item.get("{_escape(title_from)}", "")', 4)

    L('if not title or len(title) <= 2 or ql not in title.lower():', 4)
    L('continue', 5)

    L(f'href = item.get("{_escape(link_attr)}", "")', 4)
    L('if not href:', 4)
    L('continue', 5)
    L('full_url = href if href.startswith("http") else urljoin(self.url, href)', 4)

    if has_image:
        L(f'img_el = item.select_one("{_escape(image_sel)}")', 4)
        L(f'thumb = img_el.get("{_escape(image_attr)}", "") if img_el else ""', 4)
    else:
        L('thumb = ""', 4)

    L('results.append(SearchResult(', 4)
    L('title=title,', 5)
    L('url=full_url,', 5)
    L('site_name=self.name,', 5)
    L('image=thumb,', 5)
    L('))', 5)
    L('except requests.RequestException:', 2)
    L('pass', 3)
    L('return results', 2)

    return "\n".join(lines)


def _build_episodes(params: dict) -> str:
    """Generate the get_episodes() method body."""
    use_generic = params.get("use_generic", True)

    lines = []
    def L(text, level):
        lines.append(_I(text, level))

    L('def get_episodes(self, result: SearchResult) -> list[Episode]:', 1)
    L('episodes: list[Episode] = []', 2)
    L('an = result.title.split(" (")[0].strip()', 2)
    L('', 0)

    if not use_generic:
        result_sel = params.get("result_selector", 'a[href*="/episode/"]')
        title_attr = params.get("title_attr", "text")
        link_attr = params.get("link_attr", "href")
        number_regex = params.get("number_regex", r"(\d+)")
        number_from = params.get("number_from", "text")
        number_attr = params.get("number_attr", "")

        L("# Custom episode extraction", 2)
        L("try:", 2)
        L("resp = SESSION.get(result.url, timeout=SCRAPE_TIMEOUT)", 3)
        L("if resp.status_code != 200:", 3)
        L("return episodes", 4)
        L('soup = BeautifulSoup(resp.text, "lxml")', 3)
        L(f'for item in soup.select("{_escape(result_sel)}"):', 3)
        if title_attr == "text":
            L('title = item.get_text(strip=True) or "Episode"', 4)
        else:
            L(f'title = item.get("{_escape(title_attr)}", "") or "Episode"', 4)
        L(f'href = item.get("{_escape(link_attr)}", "")', 4)
        L("if not href:", 4)
        L("continue", 5)
        L("full_url = href if href.startswith(\"http\") else urljoin(result.url, href)", 4)
        if number_from == "attr" and number_attr:
            L(f'num = item.get("{_escape(number_attr)}", "")', 4)
        elif number_from == "regex":
            L(f'm = re.search(r"{_escape(number_regex)}", href)', 4)
            L("num = m.group(1) if m else str(len(episodes) + 1)", 4)
        else:
            L(f'm = re.search(r"{_escape(number_regex)}", title)', 4)
            L("num = m.group(1) if m else str(len(episodes) + 1)", 4)
        L("episodes.append(Episode(", 4)
        L("title=title,", 5)
        L("url=full_url,", 5)
        L("number=str(num),", 5)
        L("site_name=self.name,", 5)
        L("anime_name=an,", 5)
        L("))", 5)
        L("except requests.RequestException:", 2)
        L("pass", 3)
    else:
        L("# Generic fallback: look for episode links on the page", 2)
        L("try:", 2)
        L("resp = SESSION.get(result.url, timeout=SCRAPE_TIMEOUT)", 3)
        L("if resp.status_code != 200:", 3)
        L("return episodes", 4)
        L('soup = BeautifulSoup(resp.text, "lxml")', 3)
        L("seen = set()", 3)
        L("for link in soup.select(\"a[href*='/episode/'], a[href*='/ep-'], a[href*='/watch/']\"):", 3)
        L('href = link.get("href", "")', 4)
        L("if href in seen:", 4)
        L("continue", 5)
        L("seen.add(href)", 4)
        L('full_url = href if href.startswith("http") else urljoin(result.url, href)', 4)
        L('title = link.get_text(strip=True) or f"Episode {len(episodes) + 1}"', 4)
        L('m = re.search(r"(\\d+)", title)', 4)
        L("epn = m.group(1) if m else str(len(episodes) + 1)", 4)
        L("episodes.append(Episode(", 4)
        L("title=title,", 5)
        L("url=full_url,", 5)
        L("number=epn,", 5)
        L("site_name=self.name,", 5)
        L("anime_name=an,", 5)
        L("))", 5)
        L("except requests.RequestException:", 2)
        L("pass", 3)

    L("return episodes", 2)
    return "\n".join(lines)


def _build_stream(params: dict) -> str:
    """Generate the extract_stream() method body."""
    stream_type = params.get("type", "scrape")
    iframe_sel = params.get("iframe_selector", "iframe[src*='embed']")
    use_ytdlp = params.get("use_ytdlp", True)
    referer = params.get("referer", "")
    extract_mp4 = params.get("extract_mp4", True)
    extract_m3u8 = params.get("extract_m3u8", True)

    lines = []
    def L(text, level):
        lines.append(_I(text, level))

    L('def extract_stream(', 1)
    L('self,', 2)
    L('episode: Episode,', 2)
    L('audio_pref: str = "sub",', 2)
    L('quality_pref: str = "best",', 2)
    L(') -> Optional[StreamSource]:', 2)

    if stream_type == "iframe":
        L("try:", 2)
        L("resp = SESSION.get(episode.url, timeout=SCRAPE_TIMEOUT)", 3)
        L("if resp.status_code != 200:", 3)
        L("return None", 4)
        L('soup = BeautifulSoup(resp.text, "lxml")', 3)
        L(f'iframe = soup.select_one("{_escape(iframe_sel)}")', 3)
        L("if not iframe:", 3)
        L("return None", 4)
        L('embed_url = iframe.get("src", "")', 3)
        L("if not embed_url:", 3)
        L("return None", 4)

        if use_ytdlp:
            L("# Try yt-dlp first for the embed URL", 3)
            L("from anime_watch.core import extract_with_ytdlp", 3)
            L("stream = extract_with_ytdlp(embed_url)", 3)
            L("if stream and stream.url:", 3)
            L("return stream", 4)
            L("", 0)
            L("# Fallback: scrape the embed page directly", 3)

        L("headers = {}", 3)
        if referer:
            L(f'headers["Referer"] = "{_escape(referer)}"', 3)
        L('if "Referer" not in headers:', 3)
        L('headers["Referer"] = episode.url', 4)
        L("", 0)
        L("embed_resp = SESSION.get(embed_url, headers=headers, timeout=SCRAPE_TIMEOUT)", 3)
        L("if embed_resp.status_code != 200:", 3)
        L("return None", 4)

        if extract_m3u8:
            L("# Look for HLS playlist", 3)
            L("m3u8_match = re.search(r'https?://[^\"\\'<> ]+?\\.m3u8[^\"\\'<> ]*', embed_resp.text)", 3)
            L("if m3u8_match:", 3)
            L("return StreamSource(", 4)
            L("url=m3u8_match.group(0),", 5)
            L("site_name=self.name,", 5)
            L("quality=quality_pref,", 5)
            L("is_direct=True,", 5)
            L("headers=headers,", 5)
            L(")", 5)

        if extract_mp4:
            L("# Look for direct MP4", 3)
            L("mp4_match = re.search(r'https?://[^\"\\'<> ]+?\\.mp4[^\"\\'<> ]*', embed_resp.text)", 3)
            L("if mp4_match:", 3)
            L("return StreamSource(", 4)
            L("url=mp4_match.group(0),", 5)
            L("site_name=self.name,", 5)
            L("quality=quality_pref,", 5)
            L("is_direct=True,", 5)
            L(")", 5)

        L("return None", 3)
        L("except requests.RequestException:", 2)
        L("return None", 3)

    elif stream_type == "ytdlp":
        L("try:", 2)
        L("from anime_watch.core import extract_with_ytdlp", 3)
        L("stream = extract_with_ytdlp(episode.url)", 3)
        L("if stream and stream.url:", 3)
        L("return stream", 4)
        L("return None", 3)
        L("except Exception:", 2)
        L("return None", 3)

    elif stream_type == "m3u8_in_page":
        L("try:", 2)
        L("resp = SESSION.get(episode.url, timeout=SCRAPE_TIMEOUT)", 3)
        L("if resp.status_code != 200:", 3)
        L("return None", 4)
        L("m3u8_match = re.search(r'https?://[^\"\\'<> ]+?\\.m3u8[^\"\\'<> ]*', resp.text)", 3)
        L("if m3u8_match:", 3)
        L("return StreamSource(", 4)
        L("url=m3u8_match.group(0),", 5)
        L("site_name=self.name,", 5)
        L("quality=quality_pref,", 5)
        L("is_direct=True,", 5)
        L(")", 5)
        L("return None", 3)
        L("except requests.RequestException:", 2)
        L("return None", 3)

    else:
        # Default: generic scrape
        L("try:", 2)
        L("from anime_watch.core import scrape_page_for_video, extract_with_ytdlp", 3)
        L("stream = scrape_page_for_video(episode.url, self.name)", 3)
        L("if not stream or not stream.url:", 3)
        L("stream = extract_with_ytdlp(episode.url)", 4)
        L("return stream", 3)
        L("except Exception:", 2)
        L("return None", 3)

    return "\n".join(lines)


def generate_from_config(config: dict) -> str:
    """Generate a complete plugin source from a config dict.

    The config describes the site's HTML structure using CSS selectors
    and URL patterns.  Returns valid Python source code.
    """
    name = config.get("name", "Unnamed")
    slug = config.get("slug", name.lower().strip().replace(" ", "-"))
    url = config.get("url", "https://example.com").rstrip("/")
    category = config.get("category", "anime")

    class_name = slug.replace("-", " ").replace("_", " ").title().replace(" ", "")

    search_params = config.get("search", {})
    episodes_params = config.get("episodes", {"use_generic": True})
    stream_params = config.get("stream", {"type": "scrape"})

    search_code = _build_search(search_params)
    episodes_code = _build_episodes(episodes_params)
    stream_code = _build_stream(stream_params)

    config_desc = json.dumps(config, indent=2)[:200]

    return _PLUGIN_TEMPLATE.format(
        name=name,
        slug=slug,
        url=url,
        category=category,
        class_name=class_name,
        config_desc=config_desc,
        search_code=search_code,
        episodes_code=episodes_code,
        stream_code=stream_code,
    )


def generate_config_interactive() -> dict:
    """Prompt user (or AI agent) for site structure details.

    Returns a config dict suitable for generate_from_config().
    """
    import sys
    config: dict = {}

    print("=== Plugin Config Generator ===")
    config["name"] = input("Site name (e.g. 'MyAnime'): ").strip()
    config["slug"] = input("Slug (e.g. 'myanime'): ").strip() or config["name"].lower().replace(" ", "-")
    config["url"] = input("Base URL (e.g. 'https://myanime.site'): ").strip()
    config["category"] = input("Category [anime/movies] (default: anime): ").strip() or "anime"

    print("\n── Search Page ──")
    search: dict = {}
    search["url"] = input("Search path (e.g. '/search'): ").strip() or "/search"
    search["params"] = input("Query param (e.g. 'q'): ").strip() or "q"
    params_dict = {search["params"]: "{query}"}
    search["params"] = params_dict
    search["result_selector"] = input("CSS selector for result items: ").strip() or "a[href*='/anime/']"
    search["title_from"] = input("Title from [text/attr] (default: text): ").strip() or "text"
    search["link_attr"] = input("Link attribute (default: href): ").strip() or "href"
    img_q = input("Image selector (optional, e.g. 'img'): ").strip()
    if img_q:
        search["image_selector"] = img_q
        search["image_attr"] = input("Image attribute (default: src): ").strip() or "src"
    config["search"] = search

    print("\n── Episode Page ──")
    episodes: dict = {}
    ep_type = input("Use generic extraction [Y/n]: ").strip().lower()
    episodes["use_generic"] = ep_type not in ("n", "no")
    if not episodes["use_generic"]:
        episodes["result_selector"] = input("CSS selector for episode links: ").strip()
        episodes["title_attr"] = input("Title from [text/attr]: ").strip() or "text"
        episodes["link_attr"] = input("Link attribute (default: href): ").strip() or "href"
    config["episodes"] = episodes

    print("\n── Stream Extraction ──")
    stream: dict = {}
    s_type = input("Stream type [scrape/iframe/ytdlp/m3u8_in_page] (default: scrape): ").strip() or "scrape"
    stream["type"] = s_type
    if s_type == "iframe":
        stream["iframe_selector"] = input("Iframe CSS selector: ").strip() or "iframe[src*='embed']"
        ytdlp_q = input("Use yt-dlp as fallback [Y/n]: ").strip().lower()
        stream["use_ytdlp"] = ytdlp_q not in ("n", "no")
        ref_q = input("Custom Referer URL (leave blank for episode URL): ").strip()
        if ref_q:
            stream["referer"] = ref_q
    stream["extract_mp4"] = True
    stream["extract_m3u8"] = True
    config["stream"] = stream

    return config


def generate_from_file(config_path: str) -> str:
    """Read a JSON config file and generate plugin source."""
    with open(config_path) as f:
        config = json.load(f)
    return generate_from_config(config)


def save_plugin(config_path: str, output_dir: str | None = None) -> str:
    """Generate plugin from config file and save to disk.

    Returns the output file path.
    """
    if config_path.endswith(".json"):
        config = json.load(open(config_path))
    else:
        # Maybe it's a .py config module?
        import importlib.util
        spec = importlib.util.spec_from_file_location("_user_config", config_path)
        if not spec or not spec.loader:
            raise ValueError(f"Cannot load config from {config_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        config = getattr(mod, "CONFIG", None)
        if not config:
            raise ValueError(f"No CONFIG dict found in {config_path}")

    source = generate_from_config(config)
    slug = config.get("slug", config.get("name", "unnamed").lower().replace(" ", "-"))
    fname = f"{slug}.py"

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, fname)
    else:
        from .cli import _project_dir
        out_path = os.path.join(_project_dir(), fname)

    if os.path.exists(out_path):
        base, ext = os.path.splitext(out_path)
        out_path = f"{base}.new{ext}"

    with open(out_path, "w") as f:
        f.write(source)

    print(f"Generated plugin: {out_path}")
    return out_path
