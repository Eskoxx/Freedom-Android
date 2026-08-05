from __future__ import annotations

import html as html_mod
import http.server
import json
import re
import socketserver
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse, urlunparse, urlencode, parse_qs, quote, urljoin

import requests

from anime_watch.models import SearchResult, Episode, StreamSource, MediaResult
from anime_watch.core import SESSION, SCRAPE_TIMEOUT
from .base import BaseProvider


class _ManifestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server._manifest.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass


class _ManifestServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    def __init__(self, manifest: str):
        self._manifest = manifest
        super().__init__(("127.0.0.1", 0), _ManifestHandler)


BASE = "https://streamingunity.vip"
CDN = "https://cdn.streamingunity.vip"
IFRAME_BASE = f"{BASE}/en/iframe"
SEARCH_URL = f"{BASE}/en/search"
TITLE_URL = f"{BASE}/en/titles"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}

_DEBUG_PATH = "/data/data/io.freedom/cache/aw-su-debug.txt"


def _get_page_props(url: str, headers: dict | None = None) -> dict:
    resp = SESSION.get(url, headers=headers or HEADERS, timeout=SCRAPE_TIMEOUT)
    resp.raise_for_status()
    m = re.search(r'data-page="([^"]+)"', resp.text)
    if not m:
        return {}
    return json.loads(html_mod.unescape(m.group(1))).get("props", {})


def _get_image_url(images: list[dict]) -> str:
    for img in images:
        if img.get("type") == "poster":
            return f"{CDN}/images/{img['filename']}"
    for img in images:
        if img.get("type") == "cover":
            return f"{CDN}/images/{img['filename']}"
    return ""


def _write_debug(msg: str):
    try:
        with open(_DEBUG_PATH, "w") as f:
            f.write(msg)
    except Exception:
        pass


def _build_master_url(embed_url: str) -> tuple[str, str] | None:
    resp = SESSION.get(
        embed_url,
        headers={"Referer": BASE, **HEADERS},
        timeout=SCRAPE_TIMEOUT,
    )
    if resp.status_code != 200:
        return None

    html = resp.text
    token = re.search(r"'token':\s*'([^']+)'", html)
    expires = re.search(r"'expires':\s*'([^']+)'", html)
    sm = re.search(r'window\.streams\s*=\s*(\[.+?\])\s*;', html)

    if not token or not expires or not sm:
        return None

    token = token.group(1)
    expires = expires.group(1)
    streams = json.loads(sm.group(1).replace("'", '"'))
    if not streams:
        return None

    selected = streams[0]
    dbg = {"servers": streams, "selected": selected.get("name")}
    _write_debug(json.dumps(dbg, indent=2))

    u = urlparse(selected["url"])
    qs_list = [("token", token), ("expires", expires)]
    lang = parse_qs(urlparse(embed_url).query).get("lang", ["en"])[0]
    qs_list.append(("lang", lang))
    qs_list.append(("h", "1"))
    master_url = urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(qs_list), u.fragment))
    return master_url, embed_url


def _parse_qualities(master_text: str) -> dict[str, str]:
    lines = master_text.splitlines()
    quals: dict[str, str] = {}
    for i, line in enumerate(lines):
        m = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
        if m:
            h = int(m.group(2))
            label = f"{h}p"
            for j in range(i + 1, min(i + 10, len(lines))):
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("#"):
                    quals[label] = nxt
                    break
    return quals


def _fetch_master(episode: Episode) -> Optional[dict]:
    cached = episode.data.get("_su_master")
    if cached:
        return cached

    title_id = episode.data.get("title_id")
    slug = episode.data.get("slug", "")
    media_type = episode.data.get("media_type", "movie")
    episode_id = episode.data.get("episode_id")

    if not title_id:
        return None

    try:
        if media_type == "tv" and episode_id:
            iframe_url = f"{IFRAME_BASE}/{title_id}?episode_id={episode_id}&next_episode=1"
        else:
            iframe_url = f"{IFRAME_BASE}/{title_id}"

        resp = SESSION.get(
            iframe_url,
            headers={"Referer": f"{TITLE_URL}/{title_id}-{slug}", **HEADERS},
            timeout=SCRAPE_TIMEOUT,
        )
        if resp.status_code != 200:
            return None

        m = re.search(r'src="([^"]*vixcloud\.co[^"]*)"', resp.text)
        if not m:
            return None

        embed_url = m.group(1).replace("&amp;", "&")
        result = _build_master_url(embed_url)
        if not result:
            return None

        master_url, vixcloud_embed_url = result

        m3 = SESSION.get(master_url, headers=HEADERS, timeout=SCRAPE_TIMEOUT)
        if m3.status_code != 200:
            return None

        master_text = m3.text
        quals = _parse_qualities(master_text)

        data = {
            "master_url": master_url,
            "master_text": master_text,
            "referer": vixcloud_embed_url,
            "qualities": quals,
        }
        episode.data["_su_master"] = data
        return data
    except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
        return None


class _VixHlsProxy:
    """Local HLS proxy that prefetches segments in parallel ahead of the player.

    vix-content.net throttles per connection, so the player's single sequential
    HLS demuxer connection gets capped (~600kbps) while a browser's ~6 parallel
    connections are fast. This proxy downloads segments concurrently into a
    local cache and serves the player from 127.0.0.1, restoring that parallelism.
    """

    PREFETCH = 20
    WORKERS = 20
    SEG_TIMEOUT = 25

    def __init__(self, master_url: str, referer: str, selected: Optional[str] = None):
        self._referer = referer
        self._selected = selected
        self._local = threading.local()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=self.WORKERS, thread_name_prefix="vixseg")
        self._cache: dict[int, bytes] = {}
        self._inflight: dict[int, Future] = {}
        self._seg_url: dict[int, str] = {}
        self._seg_type: dict[int, str] = {}
        self._seg_group: dict[int, int] = {}
        self._group_pos: dict[int, dict[int, int]] = {}
        self._groups: list[list[int]] = []
        self._routes: dict[str, str] = {}
        self._pending: dict[str, str] = {}
        self._group_route: dict[int, str] = {}
        self._default_audio_group: Optional[int] = None
        self._default_audio_route: Optional[str] = None
        self._video_routes: list[str] = []
        self._pl_lock = threading.Lock()
        self._pl_inflight: dict[str, Future] = {}
        self._build(master_url)
        self.server = self._start_server()
        self.master_url = f"http://127.0.0.1:{self.server.server_address[1]}/master.m3u8"
        self._warm()

    def _sess(self) -> requests.Session:
        s = getattr(self._local, "sess", None)
        if s is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            self._local.sess = s
        return s

    @staticmethod
    def _parse_attrs(line: str) -> dict[str, str]:
        m = re.search(r":(.+)$", line)
        if not m:
            return {}
        attrs = {}
        for match in re.finditer(r'([\w-]+)=("([^"]*)"|([^,"\s]+))', m.group(1)):
            attrs[match.group(1)] = match.group(3) or match.group(4)
        return attrs

    def _new_seg(self, url: str, group_idx: int) -> int:
        with self._lock:
            idx = len(self._seg_url)
            self._seg_url[idx] = url
            self._seg_type[idx] = "video/MP2T"
            self._seg_group[idx] = group_idx
            pos = len(self._groups[group_idx])
            self._groups[group_idx].append(idx)
            self._group_pos[group_idx][idx] = pos
            return idx

    def _ensure_playlist(self, route: str) -> bool:
        """Fetch + rewrite a sub-playlist on demand (thread-safe, no double fetch)."""
        with self._pl_lock:
            if route in self._routes:
                return True
            url = self._pending.get(route)
            if url is None:
                return False
            fut = self._pl_inflight.get(route)
            if fut is None:
                fut = self._pool.submit(self._fetch_playlist, url, route)
                self._pl_inflight[route] = fut
        try:
            fut.result(timeout=30)
        except Exception:
            return False
        return route in self._routes

    def _fetch_playlist(self, url: str, route: str) -> None:
        try:
            resp = self._sess().get(url, headers={"Referer": self._referer}, timeout=15)
            resp.raise_for_status()
        except Exception:
            with self._pl_lock:
                self._pl_inflight.pop(route, None)
            return
        lines = []
        for line in resp.text.splitlines():
            s = line.strip()
            if not s:
                lines.append(line)
            elif s.startswith("#EXT-X-KEY"):
                lines.append(re.sub(r'URI="([^"]*)"', 'URI="https://vixcloud.co/storage/enc.key"', line))
            elif s.startswith("#"):
                lines.append(line)
            else:
                seg_orig = s if "://" in s else f"{url.rsplit('/', 1)[0]}/{s.lstrip('/')}"
                lines.append(f"/seg/{self._new_seg(seg_orig, self._route_group(route))}")
        with self._pl_lock:
            self._routes[route] = "\n".join(lines)
            self._pl_inflight.pop(route, None)

    def _route_group(self, route: str) -> int:
        if route.startswith("/v"):
            m = re.match(r"/v(\d+)\.m3u8", route)
            return int(m.group(1))
        if route.startswith("/a"):
            m = re.match(r"/a(\d+)\.m3u8", route)
            return int(m.group(1))
        m = re.match(r"/s(\d+)\.m3u8", route)
        return int(m.group(1))

    def _build(self, master_url: str) -> None:
        resp = self._sess().get(master_url, headers={"Referer": self._referer}, timeout=15)
        resp.raise_for_status()
        lines = resp.text.splitlines()
        base = master_url.split("?", 1)[0].rsplit("/", 1)[0]

        def _abs(u: str) -> str:
            return u if "://" in u else f"{base}/{u.lstrip('/')}"

        media = []
        for line in lines:
            if line.startswith("#EXT-X-MEDIA"):
                attrs = self._parse_attrs(line)
                t = attrs.get("TYPE")
                uri = attrs.get("URI", "")
                if t in ("AUDIO", "SUBTITLES") and uri:
                    media.append((t, uri, _abs(uri), attrs.get("DEFAULT") == "YES"))

        media_routes = {}
        for gi, (t, uri, abs_url, default) in enumerate(media):
            self._groups.append([])
            self._group_pos[gi] = {}
            route = f"/{'a' if t == 'AUDIO' else 's'}{gi}.m3u8"
            self._pending[route] = abs_url
            self._group_route[gi] = route
            media_routes[uri] = route
            if t == "AUDIO" and default and self._default_audio_group is None:
                self._default_audio_group = gi
                self._default_audio_route = route

        variants = []
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                height = None
                m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                if m:
                    height = int(m.group(2))
                for j in range(i + 1, len(lines)):
                    nxt = lines[j].strip()
                    if nxt and not nxt.startswith("#"):
                        variants.append((line, nxt, _abs(nxt), height))
                        break

        if self._selected:
            want = int(re.sub(r"[^0-9]", "", self._selected) or 0)
            if want:
                matches = [v for v in variants if v[3] == want]
                if matches:
                    variants = matches

        vid_routes = {}
        base_vid = len(media)
        for gi, (_, uri, abs_url, _) in enumerate(variants):
            full = base_vid + gi
            self._groups.append([])
            self._group_pos[full] = {}
            route = f"/v{full}.m3u8"
            self._pending[route] = abs_url
            self._group_route[full] = route
            self._video_routes.append(route)
            vid_routes[uri] = route

        rewritten = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if line.startswith("#EXT-X-STREAM-INF"):
                uri = None
                j = i + 1
                while j < n:
                    nxt = lines[j].strip()
                    if nxt and not nxt.startswith("#"):
                        uri = nxt
                        break
                    j += 1
                route = vid_routes.get(uri) if uri is not None else None
                if route is not None:
                    rewritten.append(line)
                    rewritten.append(route)
                i = j + 1 if uri is not None else i + 1
            elif line.startswith("#EXT-X-MEDIA"):
                attrs = self._parse_attrs(line)
                uri = attrs.get("URI", "")
                route = media_routes.get(uri)
                if route:
                    line = line.replace(f'URI="{uri}"', f'URI="{route}"')
                rewritten.append(line)
                i += 1
            elif not line.startswith("#") and line.strip():
                i += 1
            else:
                rewritten.append(line)
                i += 1
        self._routes["/master.m3u8"] = "\n".join(rewritten)

    def _start_server(self) -> "_VixProxyServer":
        server = _VixProxyServer(self, _VixProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def shutdown(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        except OSError:
            pass
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _submit(self, idx: int) -> Future:
        with self._lock:
            fut = self._inflight.get(idx)
            if fut is None:
                fut = self._pool.submit(self._fetch, idx)
                self._inflight[idx] = fut
        return fut

    def _fetch(self, idx: int) -> Optional[bytes]:
        sess = self._sess()
        url = self._seg_url[idx]
        last = None
        for attempt in range(2):
            try:
                resp = sess.get(url, headers={"Referer": self._referer}, timeout=self.SEG_TIMEOUT)
                resp.raise_for_status()
                data = resp.content
                if not data:
                    raise ValueError("empty segment")
                self._seg_type[idx] = "video/MP2T" if data[:1] == b"\x47" else resp.headers.get("Content-Type", "application/octet-stream")
                with self._lock:
                    self._cache[idx] = data
                    self._inflight.pop(idx, None)
                return data
            except Exception as e:
                last = e
        with self._lock:
            self._inflight.pop(idx, None)
        return None

    def _get(self, idx: int) -> Optional[bytes]:
        with self._lock:
            data = self._cache.get(idx)
            fut = None if data is not None else self._inflight.get(idx)
        if data is None and fut is None:
            fut = self._submit(idx)
        if fut is not None:
            try:
                data = fut.result(timeout=self.SEG_TIMEOUT)
            except Exception:
                data = None
        if data is not None:
            self._prefetch_group(idx)
        return data

    def _prefetch_group(self, idx: int) -> None:
        group = self._seg_group.get(idx)
        if group is None:
            return
        segs = self._groups[group]
        pos = self._group_pos[group].get(idx)
        if pos is None:
            return
        for j in segs[pos + 1:pos + 1 + self.PREFETCH]:
            with self._lock:
                if j in self._cache or j in self._inflight:
                    continue
            self._submit(j)

    def _warm(self) -> None:
        futs = [self._pool.submit(lambda r=r: self._ensure_playlist(r)) for r in self._video_routes]
        if self._default_audio_route:
            futs.append(self._pool.submit(lambda r=self._default_audio_route: self._ensure_playlist(r)))
        for f in futs:
            try:
                f.result(timeout=30)
            except Exception:
                pass
        primary = self._video_routes[:1]
        for route in primary:
            gi = self._route_group(route)
            for j in self._groups[gi][:self.PREFETCH + 1]:
                self._submit(j)
        for route in self._video_routes[1:]:
            gi = self._route_group(route)
            if self._groups[gi]:
                self._submit(self._groups[gi][0])
        if self._default_audio_route is not None:
            gi = self._route_group(self._default_audio_route)
            for j in self._groups[gi][:self.PREFETCH + 1]:
                self._submit(j)


class _VixProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, proxy: _VixHlsProxy, handler):
        self.proxy = proxy
        super().__init__(("127.0.0.1", 0), handler)


class _VixProxyHandler(http.server.BaseHTTPRequestHandler):
    server: _VixProxyServer

    def do_GET(self):
        path = self.path.split("?")[0]
        proxy = self.server.proxy
        if path in proxy._routes:
            self._send(200, "application/vnd.apple.mpegurl", proxy._routes[path].encode())
        elif path in proxy._pending and proxy._ensure_playlist(path):
            self._send(200, "application/vnd.apple.mpegurl", proxy._routes[path].encode())
        elif path.startswith("/seg/"):
            try:
                idx = int(path[len("/seg/"):])
            except ValueError:
                self.send_error(400)
                return
            data = proxy._get(idx)
            if data is None:
                self.send_error(502)
                return
            self._send(200, proxy._seg_type.get(idx, "video/MP2T"), data)
        else:
            self.send_error(404)

    def _send(self, code: int, mime: str, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _make_proxied_stream(master_url: str, referer: str, selected: Optional[str] = None) -> Optional[StreamSource]:
    try:
        proxy = _VixHlsProxy(master_url, referer, selected)
    except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError, OSError):
        return None
    return StreamSource(
        url=proxy.master_url,
        site_name="StreamingUnity",
        quality=selected or "auto",
        is_direct=True,
        headers=None,
        subtitles=None,
        proxy_server=proxy,
    )


class StreamingUnityProvider(BaseProvider):
    name = "StreamingUnity"
    slug = "streamingunity"
    url = "https://streamingunity.vip"
    category = "movies"

    def search(self, query: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        try:
            props = _get_page_props(f"{SEARCH_URL}?q={quote(query)}")
            titles = props.get("titles", [])
            query_words = [w.lower() for w in query.split() if w]
            for item in titles:
                name = item.get("name", "")
                name_lower = name.lower()
                if query_words and not all(w in name_lower for w in query_words):
                    continue
                title_id = item["id"]
                slug = item.get("slug", "")
                media_type = item.get("type", "movie")
                score = item.get("score", "")
                year = ""
                date_str = item.get("last_air_date") or item.get("release_date") or ""
                if date_str:
                    year = date_str[:4]

                display = f"{name} ({year})" if year else name
                if media_type == "tv":
                    seasons = item.get("seasons_count", 0)
                    if seasons:
                        display = f"{name} ({year})" if year else name

                poster = _get_image_url(item.get("images", []))
                results.append(SearchResult(
                    title=display,
                    url=f"{TITLE_URL}/{title_id}-{slug}",
                    site_name=self.name,
                    image=poster,
                    data={
                        "title_id": title_id,
                        "slug": slug,
                        "media_type": media_type,
                        "name": name,
                        "year": year,
                        "score": score,
                    },
                ))
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
            pass
        return results

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        data = result.data
        title_id = data.get("title_id")
        slug = data.get("slug", "")
        media_type = data.get("media_type", "movie")
        name = data.get("name", result.title)

        if not title_id:
            return []

        if media_type == "movie":
            return [Episode(
                title=f"{name} (Movie)",
                url=result.url,
                number="1",
                site_name=self.name,
                anime_name=name,
                data={
                    "title_id": title_id,
                    "slug": slug,
                    "media_type": "movie",
                    "name": name,
                    "year": data.get("year", ""),
                },
            )]

        episodes: list[Episode] = []
        try:
            props = _get_page_props(f"{TITLE_URL}/{title_id}-{slug}")
            title = props.get("title", {})
            seasons = title.get("seasons", [])

            for season_info in seasons:
                season_num = season_info.get("number", 1)

                season_props = _get_page_props(f"{TITLE_URL}/{title_id}-{slug}/season-{season_num}")
                loaded = season_props.get("loadedSeason", {})
                season_eps = loaded.get("episodes", [])

                for ep in season_eps:
                    ep_num = ep.get("number", 1)
                    ep_name = ep.get("name", f"Episode {ep_num}")
                    scws_id = ep.get("scws_id")
                    episode_id = ep.get("id")

                    episodes.append(Episode(
                        title=f"S{season_num} E{ep_num} - {ep_name}",
                        url=result.url,
                        number=f"{season_num}.{ep_num}",
                        site_name=self.name,
                        anime_name=name,
                        data={
                            "title_id": title_id,
                            "slug": slug,
                            "media_type": "tv",
                            "name": name,
                            "year": data.get("year", ""),
                            "season": season_num,
                            "episode": ep_num,
                            "episode_id": episode_id,
                            "scws_id": scws_id,
                        },
                    ))
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
            pass

        return episodes

    def get_servers(self, episode: Episode) -> list[dict]:
        master = _fetch_master(episode)
        if not master:
            return []
        quals = master.get("qualities", {})
        sorted_quals = sorted(quals.items(), key=lambda kv: int(kv[0].replace("p", "")), reverse=True)
        return [
            {"name": label, "display": label, "link_id": label}
            for label, _ in sorted_quals
        ]

    def extract_stream(self, episode: Episode, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        master = _fetch_master(episode)
        if not master:
            return None

        referer = master.get("referer", BASE)
        quals = master.get("qualities", {})
        selected = episode.data.get("server_name", "")
        if selected not in quals:
            selected = None

        proxied = _make_proxied_stream(master["master_url"], referer, selected)
        if proxied:
            return proxied

        if selected is None:
            return StreamSource(
                url=master["master_url"],
                site_name=self.name,
                quality="auto",
                is_direct=True,
                headers={"Referer": referer},
            )

        vid_uri = quals[selected]
        if not vid_uri.startswith("http"):
            vid_uri = urljoin(master["master_url"], vid_uri)

        audio_lines = []
        sub_lines = []
        for line in master["master_text"].splitlines():
            if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                audio_lines.append(line)
            elif line.startswith("#EXT-X-MEDIA:TYPE=SUBTITLES"):
                sub_lines.append(line)

        height = selected.replace("p", "")
        inf_line = None
        for line in master["master_text"].splitlines():
            if re.search(rf"RESOLUTION=\d+x{height}", line):
                inf_line = line
                break
        if not inf_line:
            return None

        manifest = "#EXTM3U\n#EXT-X-VERSION:3\n"
        for al in audio_lines:
            manifest += al + "\n"
        for sl in sub_lines:
            manifest += sl + "\n"
        manifest += inf_line + "\n" + vid_uri + "\n"

        try:
            server = _ManifestServer(manifest)
            port = server.server_port
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
        except Exception:
            return None

        return StreamSource(
            url=f"http://127.0.0.1:{port}/",
            site_name=self.name,
            quality=selected,
            is_direct=True,
            headers={"Referer": referer},
            proxy_server=server,
        )

    def resolve(self, media: MediaResult, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        try:
            props = _get_page_props(f"{SEARCH_URL}?q={quote(media.title)}")
            titles = props.get("titles", [])
            for item in titles:
                if item.get("type", "movie") != media.media_type:
                    continue
                item_year = ""
                date_str = item.get("last_air_date") or item.get("release_date") or ""
                if date_str:
                    item_year = date_str[:4]
                if media.year and item_year and item_year != media.year:
                    continue
                title_id = item["id"]
                slug = item.get("slug", "")
                if media.media_type == "tv":
                    iframe_url = f"{IFRAME_BASE}/{title_id}?next_episode=1"
                else:
                    iframe_url = f"{IFRAME_BASE}/{title_id}"

                resp = SESSION.get(
                    iframe_url,
                    headers={"Referer": f"{TITLE_URL}/{title_id}-{slug}", **HEADERS},
                    timeout=SCRAPE_TIMEOUT,
                )
                if resp.status_code != 200:
                    continue

                m = re.search(r'src="([^"]*vixcloud\.co[^"]*)"', resp.text)
                if not m:
                    continue

                embed_url = m.group(1).replace("&amp;", "&")
                result = _build_master_url(embed_url)
                if not result:
                    continue

                hls_url, vixcloud_embed_url = result
                proxied = _make_proxied_stream(hls_url, vixcloud_embed_url)
                if proxied:
                    return proxied

                return StreamSource(
                    url=hls_url,
                    site_name=self.name,
                    quality="auto",
                    is_direct=True,
                    headers={"Referer": vixcloud_embed_url},
                )
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
            pass
        return None
