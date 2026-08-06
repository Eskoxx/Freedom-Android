from __future__ import annotations
import base64
import json
import re
import socketserver
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urljoin

import requests

from anime_watch.providers.base import BaseProvider
from anime_watch.providers.hlsproxy import HlsProxy
from anime_watch.models import Episode, SearchResult, StreamSource


BASE = "https://api.speedracelight.com"
DB   = "https://db.speedracelight.com"
MVM1 = b"mvm1"

QUALITY_HEIGHTS = {
    "2160p": 2160,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}


def u32(x: int) -> int:
    return x & 0xFFFFFFFF


def fmix(e: int) -> int:
    e = u32(e)
    e ^= e >> 16
    e = u32(e * 2246822507)
    e ^= e >> 13
    e = u32(e * 3266489909)
    e ^= e >> 16
    return u32(e)


def rot(e: int, t: int) -> int:
    e = u32(e)
    t &= 31
    return e if t == 0 else u32((e << t) | (e >> (32 - t)))


def fnv1a(s: str) -> int:
    h = 2166136261
    for c in s:
        h = u32((h ^ ord(c)) * 16777619)
    return h


def decode_b64url(s: str) -> bytearray:
    s = s.replace("-", "+").replace("_", "/")
    pad = (4 - len(s) % 4) % 4
    return bytearray(base64.b64decode(s + "=" * pad))


def init_state(seed: str, media_id: int):
    s_val = fmix(u32(fmix(fnv1a(seed)) ^ fmix(u32(media_id ^ 2654435769))))
    S: list = [None] * 61
    for i in range(8):
        t_idx = s_val % 61
        s_val = rot(u32(s_val + 2654435769), 7 + (7 & i))
        S[t_idx] = u32(s_val ^ fmix(s_val))
        s_val = fmix(u32(s_val + t_idx))
    return S, fmix(u32(2779096485 ^ s_val))


def generate_xor_key(S: list, acc: int, length: int) -> bytes:
    S = list(S)
    out = bytearray()
    ctr = 0
    while len(out) < length:
        o = acc % 61
        in_s = o < 61 and S[o] is not None
        mask = 0xFFFFFFFF if in_s else 0
        r = u32(S[o]) if in_s else 0

        n_s = u32(r ^ u32(2654435769 * (ctr + 1)))
        c_val = u32(u32(acc ^ n_s) | u32(acc & n_s & mask))

        xor_temp = u32(rot(u32(c_val + acc), 31 & o) ^ rot(acc, 31 & ((o * 7) & 0xFFFFFFFF)))
        acc = fmix(u32(xor_temp + 2654435769))

        if o < 61:
            S[o] = acc

        out.append(acc & 0xFF)
        if len(out) < length:
            out.append((acc >> 8) & 0xFF)
        if len(out) < length:
            out.append((acc >> 16) & 0xFF)
        if len(out) < length:
            out.append((acc >> 24) & 0xFF)
        ctr += 1
    return bytes(out)


def decrypt(seed: str, media_id: int, ct_b64: str) -> dict:
    raw = decode_b64url(ct_b64)
    S, acc = init_state(seed, media_id)
    xor_key = generate_xor_key(S, acc, len(raw))
    decrypted = bytes(a ^ b for a, b in zip(raw, xor_key))
    if decrypted[:4] != MVM1:
        raise ValueError(f"Decryption failed: bad magic {decrypted[:4].hex()}")
    return json.loads(decrypted[4:].decode("utf-8"))


_source_cache: dict[str, dict] = {}


def fetch_sources(media_id: int, media_type: str, title: str = "",
                  year: str = "", imdb_id: str = "",
                  season_id: str = "", episode_id: str = "",
                  endpoint: str = "cdn") -> Optional[dict]:
    key = f"{media_id}:{media_type}:{title}:{year}:{imdb_id}:{season_id}:{episode_id}:{endpoint}"
    cached = _source_cache.get(key)
    if cached:
        return cached

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Referer": "https://www.fmovies.gd/",
    })

    r = sess.get(f"{BASE}/seed", params={"mediaId": str(media_id)}, timeout=15)
    try:
        seed = r.json()["seed"]
    except (ValueError, KeyError):
        return None

    params = {
        "title": title,
        "mediaType": media_type,
        "year": year,
        "episodeId": episode_id or "1",
        "seasonId": season_id or "1",
        "tmdbId": str(media_id),
        "imdbId": imdb_id,
        "enc": "2",
        "seed": seed,
    }

    try:
        r = sess.get(f"{BASE}/{endpoint}/sources-with-title",
                     params={k: v for k, v in params.items() if v}, timeout=20)
        ct = r.text.strip()
    except requests.exceptions.RequestException:
        return None

    try:
        data = decrypt(seed, media_id, ct)
        if data:
            _source_cache[key] = data
        return data
    except Exception:
        return None


def _pick_variant(master_url: str, max_height: int, headers: dict) -> Optional[str]:
    try:
        r = requests.get(master_url, headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        lines = r.text.splitlines()
    except Exception:
        return None

    base = master_url[: master_url.rfind("/") + 1]

    best_h = 0
    best_url = None
    fallback_h = 999999
    fallback_url = None

    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if not m:
                continue
            h = int(m.group(1))
            if i + 1 < len(lines):
                variant = lines[i + 1].strip()
                if not variant or variant.startswith("#"):
                    continue
                variant = variant if "://" in variant else urljoin(base, variant)
                if h <= max_height and h > best_h:
                    best_h = h
                    best_url = variant
                if h < fallback_h:
                    fallback_h = h
                    fallback_url = variant

    return best_url or fallback_url


class _PlaylistHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server._playlist.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass


class _PlaylistServer(socketserver.ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    def __init__(self, playlist: str):
        self._playlist = playlist
        super().__init__(("127.0.0.1", 0), _PlaylistHandler)
    def shutdown(self):
        super().shutdown()


class FmoviesProvider(BaseProvider):
    name = "Fmovies"
    slug = "fmovies"
    url = "https://www.fmovies.gd"
    category = "movies"

    _session: Optional[requests.Session] = None

    def _sess(self) -> requests.Session:
        if self._session is None:
            from requests.adapters import HTTPAdapter
            s = requests.Session()
            adapter = HTTPAdapter(pool_maxsize=50, pool_connections=50,
                                  max_retries=0)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            s.headers.update({
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                "Referer": self.url,
            })
            self._session = s
        return self._session

    def search(self, query: str) -> list[SearchResult]:
        results = []
        for media_type in ("movie", "tv"):
            r = self._sess().get(
                f"{DB}/3/search/{media_type}",
                params={"query": query, "language": "en"},
                timeout=15,
            )
            if r.status_code != 200:
                continue
            for item in r.json().get("results", [])[:10]:
                title = item.get("title") or item.get("name", "")
                year = ""
                date = item.get("release_date") or item.get("first_air_date", "")
                if date:
                    m = re.match(r"(\d{4})", date)
                    if m:
                        year = m.group(1)
                poster = item.get("poster_path") or ""
                if poster:
                    poster = f"https://image.tmdb.org/t/p/w185{poster}"
                imdb_id = item.get("external_ids", {}).get("imdb_id", "")
                display_title = f"{title} ({year})" if year else title
                results.append(SearchResult(
                    title=display_title,
                    url=f"https://www.fmovies.gd/{media_type}/{item['id']}",
                    site_name=self.name,
                    image=poster,
                    data={
                        "tmdb_id": item["id"],
                        "media_type": media_type,
                        "year": year,
                        "imdb_id": imdb_id,
                        "title": title,
                    },
                ))
        return results

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        if result.data.get("media_type") == "movie":
            return [
                Episode(
                    title=result.title,
                    url=result.url,
                    number="1",
                    site_name=self.name,
                    anime_name=result.title,
                    data=result.data,
                )
            ]
        tmdb_id = result.data.get("tmdb_id")
        if not tmdb_id:
            return []
        try:
            r = self._sess().get(f"{DB}/3/tv/{tmdb_id}", timeout=15)
            if r.status_code != 200:
                return []
            show = r.json()
            show_name = show.get("name") or result.title
            episodes: list[Episode] = []
            for s in show.get("seasons", []):
                sn = s.get("season_number")
                if sn is None or sn < 1:
                    continue
                sr = self._sess().get(f"{DB}/3/tv/{tmdb_id}/season/{sn}",
                                      timeout=15)
                if sr.status_code != 200:
                    continue
                for ep in sr.json().get("episodes", []):
                    en = ep.get("episode_number")
                    if en is None:
                        continue
                    ep_name = ep.get("name") or f"Episode {en}"
                    label = f"S{sn:02d}E{en:02d} — {ep_name}"
                    episodes.append(Episode(
                        title=label,
                        url=result.url,
                        number=str(en),
                        site_name=self.name,
                        anime_name=show_name,
                        data={
                            **result.data,
                            "season": str(sn),
                            "episode": str(en),
                            "episode_title": ep_name,
                        },
                    ))
            return episodes
        except Exception:
            return []

    def _extract_qualities(self, source_urls: list[str]) -> list[str]:
        h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36", "Referer": "https://www.fmovies.gd/"}
        qualities: set[str] = set()
        for url in source_urls:
            if not url:
                continue
            try:
                r = requests.get(url, headers=h, timeout=10)
                if r.status_code != 200:
                    continue
                for line in r.text.splitlines():
                    if line.startswith("#EXT-X-STREAM-INF:"):
                        m = re.search(r"RESOLUTION=\d+x(\d+)", line)
                        if m:
                            h = int(m.group(1))
                            label = next((k for k, v in QUALITY_HEIGHTS.items() if v == h), f"{h}p")
                            qualities.add(label)
                    elif line.startswith("#EXT-X-TARGETDURATION"):
                        parts = url.split("/")
                        for p in parts:
                            if p in QUALITY_HEIGHTS:
                                qualities.add(p)
            except Exception:
                pass
        if not qualities:
            qualities.add("Auto")
        return sorted(qualities, key=lambda q: QUALITY_HEIGHTS.get(q, 0), reverse=True)

    def get_servers(self, episode: Episode) -> list[dict]:
        data = episode.data
        media_id = data.get("tmdb_id")
        if not media_id:
            return []

        result = fetch_sources(
            media_id=int(media_id),
            media_type=data.get("media_type", "movie"),
            title=data.get("title", ""),
            year=data.get("year", ""),
            imdb_id=data.get("imdb_id", ""),
            season_id=data.get("season", ""),
            episode_id=data.get("episode", ""),
        )
        if not result:
            return []

        sources = result.get("sources", [])
        if not sources:
            return []

        qualities = self._extract_qualities([s.get("url", "") for s in sources])
        seen = set()
        servers = []
        for q in qualities:
            if q in seen:
                continue
            seen.add(q)
            servers.append({
                "name": q,
                "display": q,
                "link_id": "",
                "type": "quality",
            })
        return servers

    def extract_stream(self, episode: Episode,
                       audio_pref: str = "sub",
                       quality_pref: str = "best") -> Optional[StreamSource]:
        data = episode.data
        media_id = data.get("tmdb_id")
        if not media_id:
            return None

        sess = self._sess()
        headers = {"Referer": "https://www.fmovies.gd/"}

        result = fetch_sources(
            media_id=int(media_id),
            media_type=data.get("media_type", "movie"),
            title=data.get("title", ""),
            year=data.get("year", ""),
            imdb_id=data.get("imdb_id", ""),
            season_id=data.get("season", ""),
            episode_id=data.get("episode", ""),
        )
        if not result:
            return None

        sources = result.get("sources", [])
        if not sources:
            return None

        chosen_server = episode.data.get("server_name", "")

        target_url = ""
        if chosen_server:
            for s in sources:
                if s.get("quality") == chosen_server:
                    target_url = s.get("url", "")
                    break

        if not target_url:
            is_tv = data.get("media_type") == "tv"
            if is_tv:
                max_h = QUALITY_HEIGHTS.get(quality_pref, 99999)
                candidates = sorted(
                    [s for s in sources if s.get("url")],
                    key=lambda s: QUALITY_HEIGHTS.get(s.get("quality", ""), 0),
                    reverse=True,
                )
                for s in candidates:
                    h = QUALITY_HEIGHTS.get(s.get("quality", ""), 0)
                    if h > max_h:
                        continue
                    target_url = s["url"]
                    break
                if not target_url:
                    target_url = candidates[0]["url"] if candidates else ""
            else:
                if sources:
                    target_url = sources[0].get("url", "")
                if target_url:
                    max_h = QUALITY_HEIGHTS.get(quality_pref, 99999)
                    v = _pick_variant(target_url, max_h, headers)
                    if v:
                        target_url = v

        if not target_url:
            return None

        server = None
        is_tv = data.get("media_type") == "tv"
        if is_tv:
            # TV shows: route through the shared HLS proxy (parallel segment
            # prefetch + urgent pool) instead of letting mpv fetch segments
            # sequentially on one throttled connection.
            try:
                proxy = HlsProxy(target_url, referer="https://www.fmovies.gd/", headers=headers)
                target_url = proxy.master_url
                server = proxy
            except Exception:
                server = None
        else:
            playlist_text = None
            try:
                resp = sess.get(target_url, headers=headers, timeout=15)
                if resp.status_code == 200:
                    playlist_text = resp.text
            except requests.RequestException:
                pass
            if playlist_text:
                server = _PlaylistServer(playlist_text)
                port = server.server_address[1]
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                target_url = f"http://127.0.0.1:{port}/playlist.m3u8"

        subs = result.get("subtitles", [])
        sub_list = None
        cleanup: list[str] = []
        if subs:
            sub_list = []
            for s in subs:
                lang = s.get("lang", "")
                if "english" not in lang.lower() and not lang.lower().startswith("en"):
                    continue
                url = s.get("url")
                if not url:
                    continue
                try:
                    resp = sess.get(url, timeout=10)
                    if resp.status_code == 200:
                        ext = ".vtt" if url.endswith(".vtt") else ".srt"
                        stmp = tempfile.NamedTemporaryFile(
                            suffix=ext, delete=False, prefix="aw-fm-sub-"
                        )
                        stmp.write(resp.content)
                        stmp.close()
                        sub_list.append({"url": stmp.name, "label": lang, "lang": "en"})
                        cleanup.append(stmp.name)
                except Exception:
                    pass

        return StreamSource(
            url=target_url,
            site_name=self.name,
            quality=quality_pref,
            is_direct=True,
            headers=headers,
            subtitles=sub_list or None,
            proxy_server=server,
            cleanup_paths=cleanup or None,
        )

    def get_supported_qualities(self) -> list[str]:
        return ["best"] + list(QUALITY_HEIGHTS.keys())
