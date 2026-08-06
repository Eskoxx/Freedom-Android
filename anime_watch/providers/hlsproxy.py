"""Generic local HLS proxy with priority prefetch.

CDNs commonly throttle per connection and are slow to ramp new connections.
mpv's HLS demuxer fetches segments sequentially on a single connection, so
startup/seek pay the full per-connection penalty and sustained playback can
stall. This proxy downloads segments concurrently into a local cache and
serves mpv from 127.0.0.1.

Design (measured against live CDNs):
- On-demand (player-critical) segment requests go to a dedicated urgent pool
  so they never queue behind background prefetch jobs.
- The highest-bandwidth variant is warmed (the one players actually pick),
  with a small initial flood so startup is not a connection stampede.
- Supports both variant masters (#EXT-X-STREAM-INF) and flat media playlists.
"""

from __future__ import annotations

import http.server
import re
import socketserver
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse, urlencode, parse_qs

import requests

_DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


class HlsProxy:
    PREFETCH = 20
    WORKERS = 20
    WARM_SEGS = 8
    URGENT_WORKERS = 4
    SEG_TIMEOUT = 25

    def __init__(self, master_url: str, referer: str = "", headers: Optional[dict] = None,
                 key_url: Optional[str] = None, variant_height: Optional[int] = None):
        self._referer = referer
        self._headers = {"User-Agent": _DEFAULT_UA, **(headers or {})}
        self._key_url = key_url
        self._variant_height = variant_height
        self._local = threading.local()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=self.WORKERS, thread_name_prefix="hlsseg")
        self._urgent = ThreadPoolExecutor(max_workers=self.URGENT_WORKERS, thread_name_prefix="hlsurg")
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
        self._video_bws: list[int] = []
        self._pl_lock = threading.Lock()
        self._pl_inflight: dict[str, Future] = {}
        self._closed = False
        self._flat = False
        self._build(master_url)
        self.server = self._start_server()
        self.master_url = f"http://127.0.0.1:{self.server.server_address[1]}/master.m3u8"
        self._warm()

    def _sess(self) -> requests.Session:
        s = getattr(self._local, "sess", None)
        if s is None:
            s = requests.Session()
            s.headers.update(self._headers)
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

    def _rewrite_media(self, text: str, base_url: str, group_idx: int) -> str:
        lines = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                lines.append(line)
            elif s.startswith("#EXT-X-KEY"):
                if self._key_url:
                    lines.append(re.sub(r'URI="([^"]*)"', f'URI="{self._key_url}"', line))
                else:
                    lines.append(line)
            elif s.startswith("#"):
                lines.append(line)
            else:
                seg_orig = s if "://" in s else f"{base_url.rsplit('/', 1)[0]}/{s.lstrip('/')}"
                lines.append(f"/seg/{self._new_seg(seg_orig, group_idx)}.ts")
        return "\n".join(lines)

    def _fetch_playlist(self, url: str, route: str) -> None:
        try:
            resp = self._sess().get(url, headers={"Referer": self._referer}, timeout=15)
            resp.raise_for_status()
        except Exception:
            with self._pl_lock:
                self._pl_inflight.pop(route, None)
            return
        gi = self._route_group(route) if not self._flat else 0
        rewritten = self._rewrite_media(resp.text, url, gi)
        with self._pl_lock:
            self._routes[route] = rewritten
            self._pl_inflight.pop(route, None)

    def _route_group(self, route: str) -> int:
        if route.startswith("/v"):
            return int(re.match(r"/v(\d+)\.m3u8", route).group(1))
        if route.startswith("/a"):
            return int(re.match(r"/a(\d+)\.m3u8", route).group(1))
        return int(re.match(r"/s(\d+)\.m3u8", route).group(1))

    def _build(self, master_url: str) -> None:
        resp = self._sess().get(master_url, headers={"Referer": self._referer}, timeout=15)
        resp.raise_for_status()
        lines = resp.text.splitlines()

        if not any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
            # Flat media playlist (no variants): rewrite it in place.
            self._flat = True
            self._groups.append([])
            self._group_pos[0] = {}
            self._routes["/master.m3u8"] = self._rewrite_media(resp.text, master_url, 0)
            return

        media = []
        for line in lines:
            if line.startswith("#EXT-X-MEDIA"):
                attrs = self._parse_attrs(line)
                t = attrs.get("TYPE")
                uri = attrs.get("URI", "")
                if t in ("AUDIO", "SUBTITLES") and uri:
                    media.append((t, uri, attrs.get("DEFAULT") == "YES"))

        base = master_url[: master_url.rfind("/") + 1]

        media_routes = {}
        for gi, (t, uri, default) in enumerate(media):
            uri = uri if "://" in uri else urljoin(base, uri)
            self._groups.append([])
            self._group_pos[gi] = {}
            route = f"/{'a' if t == 'AUDIO' else 's'}{gi}.m3u8"
            self._pending[route] = uri
            self._group_route[gi] = route
            media_routes[uri] = route
            if t == "AUDIO" and default and self._default_audio_group is None:
                self._default_audio_group = gi
                self._default_audio_route = route

        vid_uris = []
        vid_bws = []
        vid_heights: list[int] = []
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                vid_bws.append(int(m.group(1)) if m else 0)
                hm = re.search(r"RESOLUTION=\d+x(\d+)", line)
                vid_heights.append(int(hm.group(1)) if hm else 0)
                for j in range(i + 1, len(lines)):
                    nxt = lines[j].strip()
                    if nxt and not nxt.startswith("#"):
                        vid_uris.append(nxt)
                        break
        if self._variant_height:
            keep = [k for k, h in enumerate(vid_heights) if h == self._variant_height]
            if keep:
                vid_uris = [vid_uris[k] for k in keep]
                vid_bws = [vid_bws[k] for k in keep]
                vid_heights = [vid_heights[k] for k in keep]
        vid_routes = {}
        base_vid = len(media)
        for gi, uri in enumerate(vid_uris):
            full = base_vid + gi
            uri = uri if "://" in uri else urljoin(base, uri)
            self._groups.append([])
            self._group_pos[full] = {}
            route = f"/v{full}.m3u8"
            self._pending[route] = uri
            self._group_route[full] = route
            self._video_routes.append(route)
            self._video_bws.append(vid_bws[gi])
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
                if uri is not None:
                    key = uri if "://" in uri else urljoin(base, uri)
                    route = vid_routes.get(key)
                    if route is not None:
                        rewritten.append(line)
                        rewritten.append(route)
                    i = j + 1
                else:
                    i += 1
            elif line.startswith("#EXT-X-MEDIA"):
                attrs = self._parse_attrs(line)
                uri = attrs.get("URI", "")
                key = uri if "://" in uri else urljoin(base, uri)
                route = media_routes.get(key)
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

    def _start_server(self) -> "_HlsProxyServer":
        server = _HlsProxyServer(self, _HlsProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        try:
            self.server.shutdown()
            self.server.server_close()
        except OSError:
            pass
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._urgent.shutdown(wait=False, cancel_futures=True)

    def _submit(self, idx: int, urgent: bool = False) -> Optional[Future]:
        with self._lock:
            if self._closed:
                return None
            fut = self._inflight.get(idx)
            if fut is None:
                pool = self._urgent if urgent else self._pool
                fut = pool.submit(self._fetch, idx)
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
            # Player-critical request: never queue behind prefetch.
            fut = self._submit(idx, urgent=True)
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
        wait_futs: list[Future] = []
        if self._flat:
            for j in self._groups[0][:self.WARM_SEGS + 1]:
                wait_futs.append(self._submit(j))
            for f in wait_futs[:2]:
                try:
                    f.result(timeout=30)
                except Exception:
                    pass
            return
        futs = [self._pool.submit(lambda r=r: self._ensure_playlist(r)) for r in self._video_routes]
        audio_routes = [r for gi, r in self._group_route.items() if r.startswith("/a")]
        if self._default_audio_route:
            audio_routes = [self._default_audio_route]
        for route in audio_routes:
            futs.append(self._pool.submit(lambda r=route: self._ensure_playlist(r)))
        # Warm subtitle playlists too so the player registers all subtitle
        # tracks early (otherwise they appear ~10s in and UIs miss them).
        for gi, route in self._group_route.items():
            if route.startswith("/s"):
                futs.append(self._pool.submit(lambda r=route: self._ensure_playlist(r)))
        for f in futs:
            try:
                f.result(timeout=30)
            except Exception:
                pass
        primary = None
        if self._video_bws:
            primary = self._video_routes[self._video_bws.index(max(self._video_bws))]
        elif self._video_routes:
            primary = self._video_routes[-1]
        if primary:
            gi = self._route_group(primary)
            for j in self._groups[gi][:self.WARM_SEGS + 1]:
                wait_futs.append(self._submit(j))
        for route in self._video_routes:
            if route == primary:
                continue
            gi = self._route_group(route)
            if self._groups[gi]:
                self._submit(self._groups[gi][0])
        if self._default_audio_route is not None:
            gi = self._route_group(self._default_audio_route)
            for j in self._groups[gi][:self.WARM_SEGS + 1]:
                wait_futs.append(self._submit(j))
        else:
            # No DEFAULT=YES audio: warm every audio group so the first audio
            # segments are cached regardless of which one the player picks.
            for gi, route in self._group_route.items():
                if route.startswith("/a") and self._groups[gi]:
                    for j in self._groups[gi][:self.WARM_SEGS + 1]:
                        wait_futs.append(self._submit(j))
        # Block until the first video + audio segments are cached so the
        # player starts with both streams in lockstep (no audio lag).
        for f in wait_futs[:3]:
            try:
                f.result(timeout=30)
            except Exception:
                pass
        # Warm the first segment of every audio + subtitle group so the
        # player's group probing is instant and all tracks register early
        # (otherwise UIs that read the track list once miss them).
        for gi, route in self._group_route.items():
            if (route.startswith("/a") or route.startswith("/s")) and self._groups[gi]:
                self._submit(self._groups[gi][0])


class _HlsProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, proxy: HlsProxy, handler):
        self.proxy = proxy
        super().__init__(("127.0.0.1", 0), handler)


class _HlsProxyHandler(http.server.BaseHTTPRequestHandler):
    server: _HlsProxyServer

    def do_GET(self):
        path = self.path.split("?")[0]
        proxy = self.server.proxy
        if path in proxy._routes:
            self._send(200, "application/vnd.apple.mpegurl", proxy._routes[path].encode())
        elif path in proxy._pending and proxy._ensure_playlist(path):
            self._send(200, "application/vnd.apple.mpegurl", proxy._routes[path].encode())
        elif path.startswith("/seg/"):
            try:
                idx = int(path[len("/seg/"):].rstrip(".ts"))
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
