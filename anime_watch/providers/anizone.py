import html
import json
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from anime_watch.models import SearchResult, Episode, StreamSource
from anime_watch.core import SESSION, SCRAPE_TIMEOUT
from .base import BaseProvider

BASE = "https://anizone.to"


class _VariantHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, body: bytes):
        self._body = body
        super().__init__(("127.0.0.1", 0), _VariantHandler)


class _VariantHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = self.server._body
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(data)))
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, *a):
        pass


def _serve_synthetic_master(body: str) -> tuple[str, HTTPServer]:
    server = _VariantHTTPServer(body.encode())
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}/master.m3u8", server


def _decode_entities(s: str) -> str:
    return html.unescape(s).strip()


def _pick_title(titles: dict) -> str:
    for key in ("1", "5", "8"):
        if titles.get(key):
            return titles[key]
    for v in titles.values():
        if v:
            return v
    return ""


def _process_json_arg(raw: str) -> dict:
    PH = "\x01U\x01"
    s = re.sub(r"\\\\u([0-9a-fA-F]{4})", f"{PH}\\1", raw)
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace(f"{PH}", "\\u")
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return {}


def _extract_json_arg(xdata: str, key: str) -> Optional[str]:
    m = re.search(rf"{key}:\s*JSON\.parse\('((?:[^'\\]|\\.)*)'\)", xdata)
    return m.group(1) if m else None


class AniZoneProvider(BaseProvider):
    name = "AniZone"
    slug = "anizone"
    url = BASE
    category = "anime"

    def get_supported_qualities(self) -> list[str]:
        return ["1080p", "720p", "480p", "best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub", "dub"]

    # ---------- search ----------
    def search(self, query: str) -> list[SearchResult]:
        results = []
        seen = set()
        # filter strictly by the original query's meaningful words
        q_words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3]
        for q in (query, self._compact(query)):
            if not q:
                continue
            results.extend(self._search_one(q, seen, q_words))
        return results

    def _compact(self, query: str) -> str:
        first = query.split()[0] if query.split() else query
        return re.sub(r"[^a-zA-Z0-9]", "", first)

    def _search_one(self, query: str, seen: set, q_words: list) -> list[SearchResult]:
        results = []
        try:
            resp = SESSION.get(
                f"{BASE}/anime",
                params={"search": query},
                headers={"Referer": f"{BASE}/"},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return results
            text = resp.text
            for m in re.finditer(r'x-data="(\{[^"]*anmTitles[^"]*\})"', text):
                ctx_end = min(len(text), m.end() + 3000)
                ctx = text[m.start():ctx_end]
                slug_m = re.search(r'href="(?:https://anizone\.to)?/anime/([a-z0-9-]+)"', ctx)
                if not slug_m:
                    continue
                slug = slug_m.group(1)
                xdata = _decode_entities(m.group(1))
                raw = _extract_json_arg(xdata, "anmTitles")
                if not raw:
                    continue
                title = _pick_title(_process_json_arg(raw))
                if not title:
                    continue
                # relevance: title must contain at least 2 whole query words
                # (word-boundary match, so "one" doesn't match "stone")
                tl = title.lower()
                if q_words:
                    hits = sum(1 for w in q_words if re.search(rf"\b{w}\b", tl))
                    full = " ".join(q_words)
                    if hits < 2 and full not in tl:
                        continue
                if slug in seen:
                    continue
                seen.add(slug)
                results.append(SearchResult(
                    title=title,
                    url=f"{BASE}/anime/{slug}",
                    site_name=self.name,
                ))
        except requests.RequestException:
            pass
        return results

    # ---------- episodes ----------
    def get_episodes(self, result: SearchResult) -> list[Episode]:
        episodes = []
        m = re.search(r"/anime/([a-z0-9-]+)", result.url)
        if not m:
            return episodes
        slug = m.group(1)
        an = result.title.split(" (")[0].strip()
        try:
            resp = SESSION.get(
                f"{BASE}/anime/{slug}",
                headers={"Referer": f"{BASE}/"},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return episodes
            text = resp.text
            seen = set()
            for m2 in re.finditer(r'x-data="(\{[^"]*epsTitles[^"]*\})"', text):
                ctx_end = min(len(text), m2.end() + 3000)
                ctx = text[m2.start():ctx_end]
                num_m = re.search(r'href="(?:https://anizone\.to)?/anime/[a-z0-9-]+/(\d+)"', ctx)
                if not num_m:
                    continue
                num = num_m.group(1)
                if num in seen:
                    continue
                seen.add(num)
                xdata = _decode_entities(m2.group(1))
                title = f"Episode {num}"
                raw = _extract_json_arg(xdata, "epsTitles")
                if raw:
                    title = _pick_title(_process_json_arg(raw)) or title
                episodes.append(Episode(
                    title=title,
                    url=f"{BASE}/anime/{slug}/{num}",
                    number=num,
                    site_name=self.name,
                    anime_name=an,
                    data={"slug": slug, "ep_num": num, "sub": 1, "dub": 0},
                ))
            episodes.sort(key=lambda e: int(e.number))
        except requests.RequestException:
            pass
        return episodes

    # ---------- stream ----------
    def extract_stream(self, episode: Episode, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        slug = (episode.data or {}).get("slug", "")
        ep_num = (episode.data or {}).get("ep_num", "")
        if not slug or not ep_num:
            m = re.search(r"/anime/([a-z0-9-]+)/(\d+)", episode.url)
            if m:
                slug, ep_num = m.group(1), m.group(2)
        if not slug:
            return None
        try:
            resp = SESSION.get(
                f"{BASE}/anime/{slug}/{ep_num}",
                headers={"Referer": f"{BASE}/anime/{slug}"},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            text = resp.text
            hls_m = re.search(r'<media-player[^>]+src="([^"]+\.m3u8[^"]*)"', text, re.I)
            if not hls_m:
                return None
            hls = _decode_entities(hls_m.group(1))

            subtitles = []
            for t in re.finditer(r"<track\b([^>]*)>", text, re.I):
                attrs = t.group(1)
                kind = re.search(r'kind="([^"]*)"', attrs, re.I)
                if not kind or kind.group(1).lower() != "subtitles":
                    continue
                src = re.search(r'src=["\']?([^\s"\'>]+)["\']?', attrs, re.I)
                label = re.search(r'label="([^"]*)"', attrs, re.I)
                srclang = re.search(r'srclang="([^"]*)"', attrs, re.I)
                if src:
                    subtitles.append({
                        "url": _decode_entities(src.group(1)),
                        "label": label.group(1) if label else "",
                        "lang": srclang.group(1) if srclang else "",
                    })

            # Hand the player a synthetic master with the audio group preserved
            # and only the best-matching video variant, served locally — mpv skips
            # its multi-variant resolution (~2.5-4s faster startup on this CDN)
            # while keeping sound.
            hls, server = self._pick_variant(hls, quality_pref)

            return StreamSource(
                url=hls,
                site_name=self.name,
                quality=quality_pref,
                is_direct=True,
                headers={"Referer": f"{BASE}/"},
                subtitles=subtitles or None,
                proxy_server=server,
            )
        except requests.RequestException:
            pass
        return None

    def _pick_variant(self, master_url: str, quality_pref: str) -> tuple[str, Optional[HTTPServer]]:
        """Pick the best matching video variant and serve a synthetic master
        that keeps the CDN's separate AUDIO renditions attached, so the player
        gets sound without paying the full multi-variant resolution cost."""
        target_h = None
        if quality_pref != "best":
            try:
                target_h = int(quality_pref.replace("p", ""))
            except ValueError:
                pass
        try:
            resp = SESSION.get(
                master_url,
                headers={"Referer": f"{BASE}/"},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return master_url, None
            lines = resp.text.splitlines()
            base = master_url.rsplit("/", 1)[0] + "/"

            media = []
            for line in lines:
                if line.startswith("#EXT-X-MEDIA:"):
                    def _abs(match):
                        return 'URI="%s"' % (urljoin(base, match.group(1)),)
                    media.append(re.sub(r'URI="([^"]*)"', _abs, line))
                elif line.startswith("#EXT-X-SESSION-DATA"):
                    media.append(line)

            best_url, best_h, fallback_url, fallback_h = None, 0, None, 10 ** 9
            best_stream_inf = None
            for i, line in enumerate(lines):
                if "#EXT-X-STREAM-INF" not in line:
                    continue
                m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                h = int(m.group(2)) if m else 0
                if i + 1 >= len(lines):
                    continue
                variant = lines[i + 1].strip()
                if not variant or variant.startswith("#"):
                    continue
                vurl = urljoin(base, variant.lstrip("/"))
                if target_h is not None and h <= target_h and h > best_h:
                    best_h, best_url, best_stream_inf = h, vurl, line
                elif target_h is None and h > best_h:
                    best_h, best_url, best_stream_inf = h, vurl, line
                if h < fallback_h:
                    fallback_h, fallback_url = h, vurl
            if best_url is None:
                best_url, best_stream_inf = fallback_url, None
            if best_url is None:
                return master_url, None

            lines_out = ["#EXTM3U", "#EXT-X-VERSION:3"] + media
            if best_stream_inf:
                lines_out.append(best_stream_inf)
            lines_out.append(best_url)
            return _serve_synthetic_master("\n".join(lines_out))
        except requests.RequestException:
            return master_url, None
