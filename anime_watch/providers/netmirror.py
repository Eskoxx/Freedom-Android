from __future__ import annotations

import json
import os
import re
import subprocess
import time
import threading
import socketserver
import http.server
from typing import Optional

import logging

import requests

from anime_watch.models import SearchResult, Episode, StreamSource, MediaResult
from .base import BaseProvider

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.config/anime-watch")
CONFIG_FILE = os.path.join(CONFIG_DIR, "net77_cookies.json")
SETUP_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "setup_netmirror.py",
)
BASE = "https://net77.cc"
PV_BASE = f"{BASE}/pv"
PLAYLIST_HOST = "https://net52.cc"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": f"{BASE}/",
}

AUDIO_LANG_MAP = {
    "sub": "eng",
    "eng": "eng",
    "english": "eng",
    "hin": "hin",
    "hindi": "hin",
    "jpn": "jpn",
    "japanese": "jpn",
}


def load_cookies() -> dict[str, str]:
    if not os.path.isfile(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def parse_token(token: str) -> Optional[dict]:
    raw = token
    if "&in=" in raw:
        raw = raw.split("&in=", 1)[1]
    if raw.startswith("in="):
        raw = raw[3:]
    parts = raw.split("::")
    if len(parts) < 6:
        return None
    return {
        "raw": raw,
        "in_token": parts[0],
        "hash": parts[1],
        "timestamp": parts[2],
        "is_premium": parts[4] == "p",
        "user_token": parts[5],
    }


class _ProxyHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, rewritten_master, sub_playlists, seg_map, referer):
        self.rewritten_master = rewritten_master
        self.sub_playlists = sub_playlists
        self.seg_map = seg_map
        self.referer = referer
        super().__init__(("127.0.0.1", 0), _ProxyRequestHandler)


class _ProxyRequestHandler(http.server.BaseHTTPRequestHandler):
    server: _ProxyHTTPServer

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/master.m3u8":
            self._serve_content(
                self.server.rewritten_master,
                "application/vnd.apple.mpegurl",
            )
        elif path.startswith("/sub/"):
            lookup = self.path[5:] if self.path.startswith("/sub/") else path[5:]
            sub_info = self.server.sub_playlists.get(lookup)
            if sub_info and "rewritten" in sub_info:
                self._serve_content(
                    sub_info["rewritten"],
                    "application/vnd.apple.mpegurl",
                )
            else:
                self.send_error(404)
        elif path.startswith("/seg/"):
            self._serve_seg(path)
        else:
            self.send_error(404)

    def _serve_content(self, content, mime):
        data = content.encode() if isinstance(content, str) else content
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _serve_seg(self, path):
        orig_url = self.server.seg_map.get(path)
        if not orig_url:
            self.send_error(404)
            return

        try:
            resp = requests.get(
                orig_url,
                headers={"Referer": self.server.referer},
                timeout=30,
            )
            if resp.status_code != 200:
                self.send_error(resp.status_code)
                return

            data = resp.content
            ct = self._detect_content_type(data, resp)

            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_error(502)

    def _detect_content_type(self, data, original_resp):
        if len(data) >= 2 and data[:2] == b"\x1f\x8b":
            return "application/octet-stream"
        if len(data) >= 1 and data[0] == 0x47:
            return "video/MP2T"
        if len(data) >= 1 and data[0] == 0x89:
            return "video/MP2T"
        return original_resp.headers.get("Content-Type", "application/octet-stream")

    def log_message(self, *a):
        pass


class NetMirrorProvider(BaseProvider):
    name = "NetMirror"
    slug = "netmirror"
    url = BASE
    category = "movies"

    def _sess(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(HEADERS)
        return s

    def _auth_sess(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(HEADERS)
        cookies = load_cookies()
        if cookies:
            s.cookies.update(cookies)
        return s

    def search(self, query: str) -> list[SearchResult]:
        sess = self._sess()
        results: list[SearchResult] = []

        # Section 1 — main net77.cc
        try:
            resp = sess.get(
                f"{BASE}/search.php",
                params={"s": query},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("searchResult", data)
                for item in items if isinstance(items, list) else items:
                    if not isinstance(item, dict):
                        continue
                    mid = item.get("id")
                    title = item.get("t", "") or item.get("title", "")
                    if not mid or not title:
                        continue
                    results.append(SearchResult(
                        title=title,
                        url=f"{BASE}/?id={mid}",
                        site_name=self.name,
                        image="",
                        data={
                            "id": mid,
                            "title": title,
                            "media_type": "movie",
                            "section": "main",
                        },
                    ))
        except (requests.RequestException, json.JSONDecodeError, ValueError):
            pass

        # Section 2 — PrimeMirror /pv
        try:
            resp = sess.get(
                f"{PV_BASE}/search.php",
                params={"s": query},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("searchResult", data)
                for item in items if isinstance(items, list) else items:
                    if not isinstance(item, dict):
                        continue
                    mid = item.get("id")
                    title = item.get("t", "") or item.get("title", "")
                    if not mid or not title:
                        continue
                    runtime = item.get("r", "")
                    year = item.get("y", "")
                    media_type = "tv" if "series" in runtime.lower() else "movie"
                    display_title = f"{title} ({year})" if year else title
                    results.append(SearchResult(
                        title=display_title,
                        url=f"{PV_BASE}/?id={mid}",
                        site_name=self.name,
                        image="",
                        data={
                            "id": mid,
                            "title": title,
                            "media_type": media_type,
                            "year": item.get("y", ""),
                            "section": "pv",
                        },
                    ))
        except (requests.RequestException, json.JSONDecodeError, ValueError):
            pass

        return results

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        section = result.data.get("section", "main")
        if section == "pv":
            return self._pv_get_episodes(result)
        return self._main_get_episodes(result)

    def _main_get_episodes(self, result: SearchResult) -> list[Episode]:
        data = result.data
        mid = data.get("id")
        title = data.get("title", result.title)

        if not mid:
            return []

        sess = self._auth_sess()

        try:
            for attempt in range(2):
                try:
                    ts = int(time.time())
                    resp = sess.get(
                        f"{BASE}/post.php",
                        params={"t": ts, "id": mid},
                        timeout=20,
                    )
                    if resp.status_code == 200:
                        break
                except requests.RequestException:
                    if attempt == 1:
                        raise
                    continue
            else:
                return [self._movie_ep(title, mid, data)]

            body = resp.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError):
            return [self._movie_ep(title, mid, data)]

        if body.get("status") == "n":
            return [self._movie_ep(title, mid, data)]

        media_type = body.get("type", "m")

        if media_type == "m" or media_type == "movie":
            return [self._movie_ep(title, mid, data)]

        # Collect all episodes with pagination
        seen_ids = set()

        def parse_episodes(ep_list):
            for ep in ep_list:
                if not isinstance(ep, dict):
                    continue
                ep_id = ep.get("id")
                if not ep_id or ep_id in seen_ids:
                    continue
                seen_ids.add(ep_id)

                ep_title = ep.get("t", "")
                season_label = ep.get("s", "")
                ep_num = str(ep.get("ep", "1"))
                runtime = ep.get("time", "")

                display = ep_title or f"Episode {ep_num}"
                if season_label:
                    display = f"{season_label}E{ep_num} — {display}"

                episodes.append(Episode(
                    title=display,
                    url=f"{BASE}/?id={ep_id}",
                    number=ep_num,
                    site_name=self.name,
                    anime_name=title,
                    data={
                        "id": ep_id,
                        "title": title,
                        "media_type": "tv",
                        "season": season_label or "",
                        "episode": ep_num,
                        "runtime": runtime,
                        "section": "main",
                    },
                ))

        episodes: list[Episode] = []
        parse_episodes(body.get("episodes", []))

        if not episodes:
            return [self._movie_ep(title, mid, data)]

        seasons = body.get("season", [])
        if seasons:
            for s in seasons:
                sid = s.get("id", "")
                if not sid:
                    continue
                try:
                    for attempt in range(2):
                        try:
                            ts = int(time.time())
                            resp = sess.get(
                                f"{BASE}/episodes.php",
                                params={"t": ts, "s": sid, "series": mid},
                                timeout=20,
                            )
                            if resp.status_code == 200:
                                break
                        except requests.RequestException:
                            if attempt == 1:
                                raise
                            continue
                    else:
                        continue
                    season_body = resp.json()
                    season_eps = season_body.get("episodes", [])
                    existing = {e.data.get("id") for e in episodes}
                    for ep in season_eps:
                        if not isinstance(ep, dict):
                            continue
                        ep_id = ep.get("id")
                        if not ep_id or ep_id in existing:
                            continue
                        existing.add(ep_id)

                        season_label = ep.get("s", "")
                        ep_num = str(ep.get("ep", "1"))
                        ep_title = ep.get("t", "")
                        display = ep_title or f"Episode {ep_num}"
                        if season_label:
                            display = f"{season_label}E{ep_num} — {display}"

                        episodes.append(Episode(
                            title=display,
                            url=f"{BASE}/?id={ep_id}",
                            number=ep_num,
                            site_name=self.name,
                            anime_name=title,
                            data={
                                "id": ep_id,
                                "title": title,
                                "media_type": "tv",
                                "season": season_label or "",
                                "episode": ep_num,
                                "runtime": ep.get("time", ""),
                                "section": "main",
                            },
                        ))
                except Exception:
                    continue

        return episodes

    def _pv_get_episodes(self, result: SearchResult) -> list[Episode]:
        data = result.data
        mid = data.get("id")
        title = data.get("title", result.title)
        if not mid:
            return []

        sess = self._auth_sess()
        try:
            ts = int(time.time())
            resp = sess.get(
                f"{PV_BASE}/post.php",
                params={"t": ts, "id": mid},
                timeout=20,
            )
            if resp.status_code != 200:
                return [self._movie_ep(title, mid, data)]
            body = resp.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError):
            return [self._movie_ep(title, mid, data)]

        if body.get("type", "m") in ("m", "movie"):
            return [self._movie_ep(title, mid, data)]

        seen_ids = set()
        episodes: list[Episode] = []

        for ep in body.get("episodes", []):
            if not isinstance(ep, dict):
                continue
            ep_id = ep.get("id")
            if not ep_id or ep_id in seen_ids:
                continue
            seen_ids.add(ep_id)
            season_label = ep.get("s", "")
            ep_num = str(ep.get("ep", "1")).lstrip("E")
            ep_title = ep.get("t", "")
            display = ep_title or f"Episode {ep_num}"
            if season_label:
                display = f"{season_label}E{ep_num} — {display}"
            episodes.append(Episode(
                title=display,
                url=f"{PV_BASE}/?id={ep_id}",
                number=ep_num,
                site_name=self.name,
                anime_name=title,
                data={
                    "id": ep_id,
                    "title": title,
                    "media_type": "tv",
                    "season": season_label or "",
                    "episode": ep_num,
                    "runtime": ep.get("time", ""),
                    "section": "pv",
                },
            ))

        # Season pagination (same as main)
        seasons = body.get("season", [])
        for s in seasons:
            sid = s.get("id", "")
            if not sid:
                continue
            try:
                ts = int(time.time())
                resp = sess.get(
                    f"{PV_BASE}/episodes.php",
                    params={"t": ts, "s": sid, "series": mid},
                    timeout=20,
                )
                if resp.status_code != 200:
                    continue
                season_body = resp.json()
                existing = {e.data.get("id") for e in episodes}
                for ep in season_body.get("episodes", []):
                    if not isinstance(ep, dict):
                        continue
                    ep_id = ep.get("id")
                    if not ep_id or ep_id in existing:
                        continue
                    existing.add(ep_id)
                    season_label = ep.get("s", "")
                    ep_num = str(ep.get("ep", "1")).lstrip("E")
                    ep_title = ep.get("t", "")
                    display = ep_title or f"Episode {ep_num}"
                    if season_label:
                        display = f"{season_label}E{ep_num} — {display}"
                    episodes.append(Episode(
                        title=display,
                        url=f"{PV_BASE}/?id={ep_id}",
                        number=ep_num,
                        site_name=self.name,
                        anime_name=title,
                        data={
                            "id": ep_id,
                            "title": title,
                            "media_type": "tv",
                            "season": season_label or "",
                            "episode": ep_num,
                            "runtime": ep.get("time", ""),
                            "section": "pv",
                        },
                    ))
            except Exception:
                continue

        return episodes or [self._movie_ep(title, mid, data)]

    def _movie_ep(self, title: str, mid: str, data: dict) -> Episode:
        section = data.get("section", "main")
        base_url = PV_BASE if section == "pv" else BASE
        return Episode(
            title=f"{title} (Movie)",
            url=f"{base_url}/?id={mid}",
            number="1",
            site_name=self.name,
            anime_name=title,
            data={
                "id": mid,
                "title": title,
                "year": data.get("year", ""),
                "media_type": "movie",
                "section": section,
            },
        )

    def _cookies_valid(self) -> bool:
        cookies = load_cookies()
        if not cookies or not cookies.get("user_token"):
            return False
        try:
            sess = self._auth_sess()
            resp = sess.get(f"{BASE}/search.php", params={"s": "a"}, timeout=10)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _prompt_setup(self):
        logger.warning("NetMirror (net77.cc) requires fresh login cookies. Run: python3 %s", SETUP_SCRIPT)

    def _parse_hls_attrs(self, line: str) -> dict[str, str]:
        m = re.search(r':(.+)$', line)
        if not m:
            return {}
        attrs = {}
        for match in re.finditer(r'([\w-]+)=("([^"]*)"|([^,"\s]+))', m.group(1)):
            key = match.group(1)
            value = match.group(3) or match.group(4)
            attrs[key] = value
        return attrs

    def _hls_proxy(self, master_url: str, referer: str,
                   audio_pref: str) -> tuple[Optional[str], Optional[_ProxyHTTPServer]]:
        try:
            resp = requests.get(master_url, headers={"Referer": referer}, timeout=15)
            if resp.status_code != 200:
                return None, None
        except requests.RequestException:
            return None, None

        master = resp.text
        base = resp.url.split("?")[0].rsplit("/", 1)[0]
        selected_lang = AUDIO_LANG_MAP.get(audio_pref.lower(), "eng")

        sub_playlists = {}
        seg_map = {}
        idx = 0

        for line in master.splitlines():
            if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                attrs = self._parse_hls_attrs(line)
                uri = attrs.get("URI", "")
                if uri:
                    sub_url = uri if "://" in uri else f"{base}/{uri.lstrip('/')}"
                    sub_playlists[uri] = {
                        "url": sub_url,
                        "lang": attrs.get("LANGUAGE", "").lower(),
                        "name": attrs.get("NAME", ""),
                        "type": "audio",
                    }

        lines = master.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                for j in range(i + 1, len(lines)):
                    next_line = lines[j].strip()
                    if next_line and not next_line.startswith("#"):
                        video_url = next_line if "://" in next_line else f"{base}/{next_line.lstrip('/')}"
                        sub_playlists[next_line] = {
                            "url": video_url,
                            "type": "video",
                        }
                        break

        for name, info in sub_playlists.items():
            try:
                sub_resp = requests.get(info["url"], headers={"Referer": referer}, timeout=15)
                if sub_resp.status_code != 200:
                    continue
            except requests.RequestException:
                continue

            sub_base = info["url"].rsplit("/", 1)[0]
            sub_body = sub_resp.text
            rewritten_lines = []
            for sub_line in sub_body.splitlines():
                if not sub_line.strip():
                    rewritten_lines.append(sub_line)
                else:
                    uri_match = re.search(r'URI="([^"]*)"', sub_line)
                    if uri_match:
                        tag_uri = uri_match.group(1)
                        seg_orig = tag_uri if "://" in tag_uri else f"{sub_base}/{tag_uri.lstrip('/')}"
                        seg_path = f"/seg/{idx}"
                        seg_map[seg_path] = seg_orig
                        rewritten_lines.append(sub_line.replace(f'URI="{tag_uri}"', f'URI="{seg_path}"'))
                        idx += 1
                    elif sub_line.startswith("#"):
                        rewritten_lines.append(sub_line)
                    else:
                        seg_orig = sub_line if "://" in sub_line else f"{sub_base}/{sub_line.lstrip('/')}"
                        seg_path = f"/seg/{idx}"
                        seg_map[seg_path] = seg_orig
                        rewritten_lines.append(seg_path)
                        idx += 1
            info["rewritten"] = "\n".join(rewritten_lines)

        if idx == 0:
            return None, None

        rewritten_lines = []
        for line in lines:
            if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                attrs = self._parse_hls_attrs(line)
                uri = attrs.get("URI", "")
                lang = attrs.get("LANGUAGE", "").lower()
                is_selected = lang == selected_lang

                if is_selected:
                    line = re.sub(r'DEFAULT=(NO|YES)', 'DEFAULT=YES', line)
                    if 'DEFAULT=' not in line:
                        line = line.replace('URI=', 'DEFAULT=YES,AUTOSELECT=YES,URI=')
                    line = re.sub(r'AUTOSELECT=(NO|YES)', 'AUTOSELECT=YES', line)
                else:
                    line = line.replace('DEFAULT=YES', 'DEFAULT=NO')
                    line = re.sub(r'AUTOSELECT=(NO|YES)', 'AUTOSELECT=NO', line)
                    if 'AUTOSELECT=' not in line and 'DEFAULT=' in line:
                        line = line.replace('DEFAULT=', 'AUTOSELECT=NO,DEFAULT=')

                if uri:
                    if uri in sub_playlists and "rewritten" in sub_playlists[uri]:
                        line = line.replace(f'URI="{uri}"', f'URI="/sub/{uri}"')
                rewritten_lines.append(line)

            elif not line.startswith("#") and line.strip():
                stripped = line.strip()
                if stripped in sub_playlists and "rewritten" in sub_playlists[stripped]:
                    rewritten_lines.append(f"/sub/{stripped}")
                else:
                    rewritten_lines.append(line)
            else:
                rewritten_lines.append(line)

        rewritten_master = "\n".join(rewritten_lines)

        server = _ProxyHTTPServer(rewritten_master, sub_playlists, seg_map, referer)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return f"http://127.0.0.1:{port}/master.m3u8", server

    def get_servers(self, episode: Episode) -> list[dict]:
        section = episode.data.get("section", "main")
        if section == "pv":
            return self._pv_get_servers(episode)
        return self._main_get_servers(episode)

    def _main_get_servers(self, episode: Episode) -> list[dict]:
        _default = {"name": "Default", "display": "Default Audio", "link_id": "eng", "type": "eng"}
        data = episode.data
        ep_id = data.get("id")
        if not ep_id:
            return [_default]

        sess = self._auth_sess()
        token_info = self._get_play_token(sess, ep_id)
        if not token_info:
            if not self._cookies_valid():
                self._prompt_setup()
            return [_default]

        data["_cached_token"] = token_info

        playlist_data = self._get_playlist(sess, ep_id, token_info)
        if playlist_data:
            hls_url = playlist_data
        else:
            hls_url = f"{PLAYLIST_HOST}/hls/{ep_id}.m3u8?in={token_info['raw']}"

        data["_cached_playlist_url"] = hls_url
        data["_cached_tracks"] = self._get_subtitle_tracks(sess, ep_id, token_info)

        try:
            resp = sess.get(
                hls_url,
                headers={"Referer": f"{PLAYLIST_HOST}/play.php?id={ep_id}"},
                timeout=15,
            )
            if resp.status_code != 200:
                return [_default]

            tracks = []
            for line in resp.text.splitlines():
                if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                    attrs = self._parse_hls_attrs(line)
                    lang = attrs.get("LANGUAGE", "").lower()
                    name = attrs.get("NAME", "")
                    if lang and name:
                        tracks.append({
                            "name": name,
                            "display": f"{name} ({lang.upper()})",
                            "link_id": lang,
                            "type": lang,
                        })

            return tracks if tracks else [_default]
        except Exception:
            return [_default]

    def _pv_get_servers(self, episode: Episode) -> list[dict]:
        _default = {"name": "Default", "display": "Default Audio", "link_id": "eng", "type": "eng"}
        data = episode.data
        ep_id = data.get("id")
        if not ep_id:
            return [_default]

        sess = self._auth_sess()
        token_info = self._pv_get_play_token(sess, ep_id)
        if not token_info:
            if not self._cookies_valid():
                self._prompt_setup()
            return [_default]
        data["_cached_token"] = token_info

        hls_url = self._pv_get_playlist(sess, ep_id, token_info)
        if hls_url:
            data["_cached_playlist_url"] = hls_url
            data["_cached_tracks"] = getattr(sess, "_pv_cached_tracks", [])
        else:
            data["_cached_tracks"] = []
        return [_default]

    def _get_subtitle_tracks(self, sess, ep_id, token_info):
        try:
            ts = token_info.get("timestamp", int(time.time()))
            h = token_info.get("raw", "")
            resp = sess.get(
                f"{PLAYLIST_HOST}/playlist.php?id={ep_id}&t=Movie&tm={ts}&h={h}",
                headers={"Referer": f"{BASE}/"},
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            items = data if isinstance(data, list) else [data]
            tracks = items[0].get("tracks", []) if items else []
            return tracks or []
        except Exception:
            return []

    def _build_subtitles(self, tracks: list[dict]) -> Optional[list[dict[str, str]]]:
        if not tracks:
            return None
        subs = []
        for track in tracks:
            file_url = track.get("file", "")
            if not file_url:
                continue
            if file_url.startswith("//"):
                file_url = "https:" + file_url
            subs.append({
                "url": file_url,
                "label": track.get("label", "") or track.get("language", ""),
                "lang": (track.get("language") or track.get("label", "")).lower(),
            })
        return subs if subs else None

    def extract_stream(self, episode: Episode,
                       audio_pref: str = "sub",
                       quality_pref: str = "best") -> Optional[StreamSource]:
        section = episode.data.get("section", "main")
        if section == "pv":
            return self._pv_extract_stream(episode, audio_pref, quality_pref)
        return self._main_extract_stream(episode, audio_pref, quality_pref)

    def _main_extract_stream(self, episode: Episode,
                              audio_pref: str = "sub",
                              quality_pref: str = "best") -> Optional[StreamSource]:
        data = episode.data
        ep_id = data.get("id")

        if not ep_id:
            return None

        sess = self._auth_sess()

        token_info = data.get("_cached_token")
        if token_info:
            ts = int(token_info.get("timestamp", 0))
            if time.time() - ts > 120:
                token_info = None
        if not token_info:
            token_info = self._get_play_token(sess, ep_id)

        if not token_info:
            if not self._cookies_valid():
                self._prompt_setup()
            return None

        if not token_info["is_premium"]:
            if not self._cookies_valid():
                self._prompt_setup()
            return None

        playlist_url = data.get("_cached_playlist_url")
        if not playlist_url:
            playlist_data = self._get_playlist(sess, ep_id, token_info)
            if playlist_data:
                playlist_url = playlist_data
            else:
                playlist_url = f"{PLAYLIST_HOST}/hls/{ep_id}.m3u8?in={token_info['raw']}"

        tracks = data.get("_cached_tracks")
        if tracks is None:
            tracks = self._get_subtitle_tracks(sess, ep_id, token_info)
            data["_cached_tracks"] = tracks
        subtitles = self._build_subtitles(tracks)

        referer = f"{PLAYLIST_HOST}/play.php?id={ep_id}"
        proxy_url, proxy_server = self._hls_proxy(playlist_url, referer, audio_pref)

        if not proxy_url:
            return StreamSource(
                url=playlist_url,
                site_name=self.name,
                quality=quality_pref,
                is_direct=True,
                headers={"Referer": referer},
                subtitles=subtitles,
            )

        return StreamSource(
            url=proxy_url,
            site_name=self.name,
            quality=quality_pref,
            is_direct=True,
            subtitles=subtitles,
            proxy_server=proxy_server,
            extra_mpv_args=["--vo=x11", "--ontop", "--geometry=50%:50%"],
        )

    def _pv_extract_stream(self, episode: Episode,
                            audio_pref: str = "sub",
                            quality_pref: str = "best") -> Optional[StreamSource]:
        data = episode.data
        ep_id = data.get("id")
        if not ep_id:
            return None

        sess = self._auth_sess()
        token_info = data.get("_cached_token")
        if token_info:
            ts = int(token_info.get("timestamp", 0))
            if time.time() - ts > 120:
                token_info = None
        if not token_info:
            token_info = self._pv_get_play_token(sess, ep_id)
        if not token_info:
            if not self._cookies_valid():
                self._prompt_setup()
            return None
        if not token_info["is_premium"]:
            if not self._cookies_valid():
                self._prompt_setup()
            return None

        playlist_url = data.get("_cached_playlist_url")
        if not playlist_url:
            playlist_url = self._pv_get_playlist(sess, ep_id, token_info)
        if not playlist_url:
            return None

        token_raw = token_info.get("raw", "")
        referer = f"{PLAYLIST_HOST}/pv/player.php?id={ep_id}&in={token_raw}"
        proxy_url, proxy_server = self._hls_proxy(playlist_url, referer, audio_pref)

        tracks = data.get("_cached_tracks")
        if tracks is None:
            tracks = getattr(sess, "_pv_cached_tracks", [])
            data["_cached_tracks"] = tracks
        subtitles = self._build_subtitles(tracks)

        if not proxy_url:
            return StreamSource(
                url=playlist_url,
                site_name=self.name,
                quality=quality_pref,
                is_direct=True,
                headers={"Referer": referer},
                subtitles=subtitles,
            )

        return StreamSource(
            url=proxy_url,
            site_name=self.name,
            quality=quality_pref,
            is_direct=True,
            proxy_server=proxy_server,
            subtitles=subtitles,
            extra_mpv_args=["--vo=x11", "--ontop", "--geometry=50%:50%"],
        )

    def resolve(self, media: MediaResult,
                audio_pref: str = "sub",
                quality_pref: str = "best") -> Optional[StreamSource]:
        results = self.search(media.title)
        if not results:
            return None

        for r in results:
            rd = r.data
            if media.year and rd.get("year") and rd["year"] != str(media.year):
                continue
            episodes = self.get_episodes(r)
            if not episodes:
                continue
            return self.extract_stream(episodes[0], audio_pref, quality_pref)

        return self.extract_stream(self.get_episodes(results[0])[0],
                                   audio_pref, quality_pref)

    def _get_play_token(self, sess: requests.Session,
                        ep_id: str) -> Optional[dict]:
        try:
            resp = sess.post(
                f"{BASE}/play.php",
                data={"id": ep_id},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            token = body.get("h", "")
            if not token:
                return None
            parsed = parse_token(token)
            return parsed
        except (requests.RequestException, json.JSONDecodeError, ValueError):
            return None

    def _get_playlist(self, sess: requests.Session,
                      ep_id: str,
                      token_info: dict) -> Optional[str]:
        try:
            ts = token_info.get("timestamp", int(time.time()))
            h = token_info.get("raw", "")
            playlist_url = (
                f"{PLAYLIST_HOST}/playlist.php"
                f"?id={ep_id}&t=Movie&tm={ts}&h={h}"
            )
            resp = sess.get(
                playlist_url,
                headers={"Referer": f"{BASE}/"},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if isinstance(data, list) and data:
                sources = data[0].get("sources", [])
            else:
                sources = data.get("sources", [])

            if not sources:
                return None

            chosen = None
            for s in sources:
                if s.get("default") == "true" or s.get("default") is True:
                    chosen = s
                    break
            if not chosen:
                chosen = sources[0]

            file_path = chosen.get("file", "")
            if not file_path:
                return None

            full_url = f"{PLAYLIST_HOST}{file_path}"
            full_url = full_url.replace("?in=in=", "?in=")
            return full_url
        except Exception:
            return None

    def _pv_get_play_token(self, sess: requests.Session,
                            ep_id: str) -> Optional[dict]:
        try:
            resp = sess.post(
                f"{PV_BASE}/play.php",
                data={"id": ep_id},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            token = body.get("h", "")
            if not token:
                return None
            parsed = parse_token(token)
            return parsed
        except (requests.RequestException, json.JSONDecodeError, ValueError):
            return None

    def _pv_get_playlist(self, sess: requests.Session,
                          ep_id: str,
                          token_info: dict) -> Optional[str]:
        try:
            import re as _re
            # Step 1: load player.php to get fresh data-h (signed token for this session)
            token_raw = token_info.get("raw", "")
            player_page_url = f"{PLAYLIST_HOST}/pv/player.php?id={ep_id}&in={token_raw}"
            player_resp = sess.get(
                player_page_url,
                headers={"Referer": f"{PV_BASE}/"},
                timeout=15,
            )
            if player_resp.status_code != 200:
                return None
            html = player_resp.text
            body_h = _re.search(r'data-h="([^"]*)"', html)
            body_time = _re.search(r'data-time="([^"]*)"', html)
            body_title = _re.search(r'data-title="([^"]*)"', html)
            if not body_h or not body_time or not body_title:
                return None
            h_val = body_h.group(1)
            tm_val = body_time.group(1)
            title_val = body_title.group(1)

            # Step 2: get the real playlist from playlist2.php
            resp = sess.get(
                f"{PLAYLIST_HOST}/pv/playlist2.php",
                params={"id": ep_id, "t": title_val, "tm": tm_val, "h": h_val},
                headers={"Referer": player_page_url},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            sources = data[0].get("sources", []) if isinstance(data, list) else data.get("sources", [])
            if not sources:
                return None
            chosen = None
            for s in sources:
                if s.get("default") == "true" or s.get("default") is True:
                    chosen = s
                    break
            if not chosen:
                chosen = sources[0]
            file_path = chosen.get("file", "")
            if not file_path:
                return None
            full_url = f"{PLAYLIST_HOST}{file_path}"
            full_url = full_url.replace("?in=in=", "?in=")

            # Cache subtitles from playlist2 response
            tracks = data[0].get("tracks", [])
            if tracks:
                sess._pv_cached_tracks = tracks

            return full_url
        except Exception:
            return None

    def get_supported_qualities(self) -> list[str]:
        return ["best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub", "eng", "hin", "jpn"]
