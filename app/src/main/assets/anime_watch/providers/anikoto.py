import json, os, tempfile, uuid, threading, socketserver
import re
from typing import Optional
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote
from http.server import HTTPServer, BaseHTTPRequestHandler
from anime_watch.models import SearchResult, Episode, StreamSource
from anime_watch.core import SESSION, SCRAPE_TIMEOUT
from .base import BaseProvider


def _pick_hls_variant(master_url: str, quality_pref: str) -> Optional[str]:
    target_h = int(quality_pref.replace("p", ""))
    try:
        resp = SESSION.get(
            master_url,
            headers={"Referer": "https://megaplay.buzz/"},
            timeout=SCRAPE_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        body = resp.text
    except requests.RequestException:
        return None

    best_url = None
    best_h = 0
    fallback_url = None
    fallback_h = 99999
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            if not m:
                continue
            h = int(m.group(2))
            if i + 1 < len(lines):
                variant = lines[i + 1].strip()
                if not variant or variant.startswith("#"):
                    continue
                if not variant.startswith("http"):
                    base = master_url.rsplit("/", 1)[0]
                    variant = f"{base}/{variant.lstrip('/')}"
                if h <= target_h and h > best_h:
                    best_h = h
                    best_url = variant
                if h < fallback_h:
                    fallback_h = h
                    fallback_url = variant
    return best_url or fallback_url


PNG_WRAP_CDNS = {"mt.nekostream.site", "9hjkrt.nekostream.site", "vidtub.kotocdn.site", "megap.kotocdn.site"}


class _ProxyHTTPServer(HTTPServer):
    def __init__(self, playlist, referer, seg_map):
        self._playlist = playlist
        self._referer = referer
        self._seg_map = seg_map
        super().__init__(("127.0.0.1", 0), _SegmentProxyHandler)


class _SegmentProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        if self.path == "/playlist.m3u8":
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.end_headers()
            self.wfile.write(server._playlist.encode())
            return
        orig_url = server._seg_map.get(self.path)
        if not orig_url:
            self.send_response(404)
            self.end_headers()
            return
        try:
            resp = SESSION.get(
                orig_url,
                headers={"Referer": server._referer},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                self.send_response(502)
                self.end_headers()
                return
            data = resp.content
            if data[:1] == b"\x89":
                offset = 8
                while offset < len(data) - 8:
                    chunk_len = int.from_bytes(data[offset:offset+4], 'big')
                    if data[offset+4:offset+8] == b"IEND":
                        data = data[offset + 12:]
                        break
                    offset += 12 + chunk_len
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_response(502)
            self.end_headers()

    def log_message(self, *a):
        pass


def _proxy_hls(playlist_url: str, referer: str, cdn_domain: str) -> tuple[str, HTTPServer]:
    resp = SESSION.get(
        playlist_url,
        headers={"Referer": referer},
        timeout=SCRAPE_TIMEOUT,
    )
    if resp.status_code != 200:
        return playlist_url, None
    body = resp.text
    seg_map = {}
    idx = 0
    new_lines = []
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            new_lines.append(line)
            continue
        if "://" in line:
            orig = line
        else:
            base = playlist_url.rsplit("/", 1)[0]
            orig = f"{base}/{line.lstrip('/')}"
        path = f"/seg/{idx}"
        seg_map[path] = orig
        new_lines.append(path)
        idx += 1

    server = _ProxyHTTPServer("\n".join(new_lines), referer, seg_map)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}/playlist.m3u8", server


TYPE_LABELS = {"sub": "SUB", "dub": "DUB", "hsub": "HSUB"}


class AnikotoProvider(BaseProvider):
    name = "Anikoto"
    slug = "anikoto"
    url = "https://anikototv.to"

    def get_supported_qualities(self) -> list[str]:
        return ["1080p", "720p", "480p", "best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub", "dub"]

    def search(self, query: str) -> list[SearchResult]:
        results = []
        ql = query.lower()
        try:
            resp = SESSION.get(
                f"{self.url}/filter",
                params={"keyword": query},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return results
            soup = BeautifulSoup(resp.text, "lxml")
            for item in soup.select(".main .item"):
                poster = item.select_one(".poster")
                media_id = poster.get("data-tip") if poster else None
                if not media_id:
                    continue
                title_el = item.select_one(".name")
                title = title_el.get_text(strip=True) if title_el else ""
                if not title or len(title) <= 2 or ql not in title.lower():
                    continue
                img = item.select_one("img")
                thumb = img.get("src") if img else ""
                results.append(SearchResult(
                    title=title,
                    url=f"{self.url}/mid/{media_id}",
                    site_name=self.name,
                    image=thumb or "",
                ))
        except requests.RequestException:
            pass
        return results

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        episodes = []
        m = re.search(r"/mid/(\d+)", result.url)
        if not m:
            return episodes
        media_id = m.group(1)
        an = result.title.split(" (")[0].strip()

        try:
            resp = SESSION.get(
                f"{self.url}/ajax/episode/list/{media_id}",
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": result.url},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return episodes
            body = resp.json()
            if body.get("status") != 200:
                return episodes
            soup = BeautifulSoup(body["result"], "lxml")
            for a in soup.find_all("a", href=True):
                num = a.get("data-num", "")
                title = a.get_text(strip=True) or f"Episode {num}"
                data_ids = a.get("data-ids", "")
                if not data_ids:
                    continue
                mal_id = a.get("data-mal", "")
                slug = a.get("data-slug", "")
                ts = a.get("data-timestamp", "")
                episodes.append(Episode(
                    title=title,
                    url=result.url,
                    number=str(num),
                    site_name=self.name,
                    anime_name=an,
                    data={"data_ids": data_ids, "media_id": media_id,
                          "mal_id": mal_id, "slug": slug, "ts": ts},
                ))
        except (requests.RequestException, json.JSONDecodeError, KeyError):
            pass
        return episodes

    def get_servers(self, episode: Episode) -> list[dict]:
        data_ids = episode.data.get("data_ids", "")
        if not data_ids:
            return []
        servers = []
        seen = set()
        try:
            resp = SESSION.get(
                f"{self.url}/ajax/server/list?servers={data_ids}",
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": episode.url},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("status") == 200:
                    soup = BeautifulSoup(body["result"], "lxml")
                    excluded_servers = set()
                    for li in soup.select("li[data-link-id]"):
                        name = li.get_text(strip=True)
                        if name.lower() in excluded_servers:
                            continue
                        link_id = li.get("data-link-id", "")
                        dtype = "sub"
                        parent = li.find_parent(class_="type")
                        if parent:
                            dt = parent.get("data-type", "")
                            if dt == "dub":
                                dtype = "dub"
                            elif dt == "hsub":
                                dtype = "hsub"
                        display = f"{name} ({TYPE_LABELS.get(dtype, dtype.upper())})"
                        if display not in seen:
                            seen.add(display)
                            servers.append({
                                "name": name,
                                "display": display,
                                "link_id": link_id,
                                "type": dtype,
                            })
        except (requests.RequestException, json.JSONDecodeError, KeyError):
            pass

        mal_id = episode.data.get("mal_id", "")
        slug = episode.data.get("slug", "")
        ts = episode.data.get("ts", "")
        if mal_id and slug and ts:
            try:
                r2 = SESSION.get(
                    f"https://mapper.nekostream.site/api/mal/{mal_id}/{slug}/{ts}",
                    timeout=SCRAPE_TIMEOUT,
                )
                if r2.status_code == 200:
                    mapper_data = r2.json()
                    for sv_name, sv_data in mapper_data.items():
                        if sv_name == "status":
                            continue
                        if "kiwi" in sv_name.lower():
                            continue
                        for lang in ("sub", "dub"):
                            entry = sv_data.get(lang)
                            if not entry:
                                continue
                            has_url = bool(entry.get("url"))
                            has_dl = bool(entry.get("download"))
                            if has_url or has_dl:
                                display = f"{sv_name} ({TYPE_LABELS.get(lang, lang.upper())})"
                                if not has_url:
                                    display = f"[DL] {display}"
                                if display not in seen:
                                    seen.add(display)
                                    dl_url = ""
                                    if has_dl and isinstance(entry.get("download"), dict):
                                        dl_url = entry["download"].get(sv_name, "")
                                    servers.append({
                                        "name": sv_name,
                                        "display": display,
                                        "link_id": entry.get("url", ""),
                                        "type": lang,
                                        "download_url": dl_url,
                                    })
            except (requests.RequestException, json.JSONDecodeError):
                pass

        return servers

    def extract_stream(self, episode: Episode, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        servers = self.get_servers(episode)
        if not servers:
            return None

        chosen = episode.data.get("server_name", "")
        link_id = None
        download_url = None
        audio_type = audio_pref

        if chosen:
            for sv in servers:
                if sv.get("display") == chosen:
                    link_id = sv.get("link_id") or ""
                    download_url = sv.get("download_url", "") or ""
                    audio_type = sv.get("type", audio_pref)
                    break
            if download_url and not link_id:
                return StreamSource(
                    url=download_url,
                    site_name=self.name,
                    quality=quality_pref,
                    is_direct=False,
                    headers={"Referer": "https://pahe.nekostream.site/"},
                )
            if not link_id:
                return None
        else:
            for sv in servers:
                if sv.get("type") == audio_pref and sv.get("link_id"):
                    link_id = sv["link_id"]
                    audio_type = audio_pref
                    break
            if not link_id:
                link_id = servers[0].get("link_id", "")
                audio_type = servers[0].get("type", audio_pref)
            if not link_id:
                return None

        try:
            resp = SESSION.get(
                f"{self.url}/ajax/server?get={link_id}",
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": episode.url},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            if body.get("status") != 200:
                return None
            embed_url = body["result"].get("url", "")
            if not embed_url:
                return None
        except (requests.RequestException, json.JSONDecodeError, KeyError):
            return None

        domain = urlparse(embed_url).netloc
        result = self._extract_megaclone(embed_url, domain, quality_pref, audio_type)
        if result is None:
            result = self._extract_generic(embed_url, quality_pref)
        return result

    def _extract_megaclone(self, embed_url: str, domain: str, quality_pref: str, audio_type: str = "sub") -> Optional[StreamSource]:
        if "/stream/" not in embed_url:
            return None

        try:
            embed_resp = SESSION.get(
                embed_url,
                headers={"Referer": self.url},
                timeout=SCRAPE_TIMEOUT,
            )
            if embed_resp.status_code != 200:
                return None

            file_id_match = re.search(r"File\s+(\d+)\s+-", embed_resp.text)
            if not file_id_match:
                return self._extract_generic(embed_url, quality_pref)
            file_id = file_id_match.group(1)

            api_base = f"https://{domain}"
            api_url = f"{api_base}/stream/getSources?id={file_id}"
            if audio_type == "dub":
                api_url += "&type=dub"
            sources_resp = SESSION.get(
                api_url,
                headers={
                    "Referer": embed_url,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=SCRAPE_TIMEOUT,
            )
            if sources_resp.status_code != 200:
                return None

            sources_data = sources_resp.json()
            sources = sources_data.get("sources", {})
            m3u8_url = sources.get("file", "") if isinstance(sources, dict) else ""
            if not m3u8_url:
                return None

            if quality_pref != "best":
                best_variant = _pick_hls_variant(m3u8_url, quality_pref)
                if best_variant:
                    m3u8_url = best_variant

            proxy_server = None
            parsed_m3u8 = urlparse(m3u8_url)
            if parsed_m3u8.netloc in PNG_WRAP_CDNS or parsed_m3u8.netloc.endswith('.nekostream.site'):
                proxy_url, proxy_server = _proxy_hls(m3u8_url, "https://megaplay.buzz/", parsed_m3u8.netloc)
                if proxy_url:
                    m3u8_url = proxy_url

            subtitles = [
                {"url": t["file"], "label": t.get("label", ""), "lang": t.get("label", "").lower()}
                for t in sources_data.get("tracks", [])
                if t.get("kind") == "captions" and t.get("file")
            ]

            return StreamSource(
                url=m3u8_url,
                site_name=self.name,
                quality=quality_pref,
                is_direct=True,
                headers={"Referer": "https://megaplay.buzz/"},
                subtitles=subtitles or None,
                proxy_server=proxy_server,
            )

        except (requests.RequestException, json.JSONDecodeError, KeyError):
            return None

    def _extract_generic(self, embed_url: str, quality_pref: str) -> Optional[StreamSource]:
        try:
            resp = SESSION.get(
                embed_url,
                headers={"Referer": self.url},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None

            m3u8_match = re.search(r'https?://[^"\'<> ]+?\.m3u8[^"\'<> ]*', resp.text)
            if m3u8_match:
                url = m3u8_match.group(0)
                if quality_pref != "best":
                    variant = _pick_hls_variant(url, quality_pref)
                    if variant:
                        url = variant
                return StreamSource(
                    url=url,
                    site_name=self.name,
                    quality=quality_pref,
                    is_direct=True,
                    headers={"Referer": f"{urlparse(embed_url).scheme}://{urlparse(embed_url).netloc}/"},
                )

            mp4_match = re.search(r'https?://[^"\'<> ]+?\.mp4[^"\'<> ]*', resp.text)
            if mp4_match:
                return StreamSource(
                    url=mp4_match.group(0),
                    site_name=self.name,
                    quality=quality_pref,
                    is_direct=True,
                )

            return None
        except requests.RequestException:
            return None
