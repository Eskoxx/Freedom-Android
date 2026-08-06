from __future__ import annotations

import json
from typing import Optional
import requests

from anime_watch.models import SearchResult, Episode, StreamSource, MediaResult
from anime_watch.core import SESSION, SCRAPE_TIMEOUT
from .base import BaseProvider
from .hlsproxy import HlsProxy

API_BASE = "https://api.bingr.one/api"
SERVERS = ["s11", "s12"]
SERVER_NAMES = {"s11": "Sirius", "s12": "Quasar"}

HEADERS = {
    "Origin": "https://bingr.one",
    "Referer": "https://bingr.one/",
}


class BingrProvider(BaseProvider):
    name = "Bingr"
    slug = "bingr"
    url = "https://bingr.one"
    category = "movies"

    def get_supported_qualities(self) -> list[str]:
        return ["1080p", "720p", "480p", "360p", "best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub"]

    def search(self, query: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        for media_type in ("movie", "tv"):
            try:
                resp = SESSION.get(
                    f"{API_BASE}/search",
                    params={"q": query, "type": media_type},
                    headers=HEADERS,
                    timeout=SCRAPE_TIMEOUT,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                for item in data.get("results", []):
                    tmdb_id = item.get("id")
                    title = item.get("title", "")
                    year = item.get("year", "")
                    display = f"{title} ({year})" if year else title
                    poster = item.get("poster", "") or ""
                    results.append(SearchResult(
                        title=display,
                        url=f"{API_BASE}/details/{media_type}/{tmdb_id}",
                        site_name=self.name,
                        image=poster,
                        data={
                            "tmdb_id": tmdb_id,
                            "media_type": media_type,
                            "title": title,
                            "year": year,
                        },
                    ))
            except (requests.RequestException, json.JSONDecodeError, KeyError):
                pass
        return results

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        data = result.data
        media_type = data.get("media_type", "movie")
        tmdb_id = data.get("tmdb_id")
        title = data.get("title", result.title)

        if not tmdb_id:
            return []

        if media_type == "movie":
            return [Episode(
                title=f"{title} (Movie)",
                url=result.url,
                number="1",
                site_name=self.name,
                anime_name=title,
                data={
                    "tmdb_id": tmdb_id,
                    "media_type": "movie",
                    "title": title,
                    "year": data.get("year", ""),
                },
            )]

        if media_type == "tv":
            try:
                resp = SESSION.get(
                    f"{API_BASE}/details/tv/{tmdb_id}",
                    headers=HEADERS,
                    timeout=SCRAPE_TIMEOUT,
                )
                if resp.status_code == 200:
                    detail = resp.json()
                    seasons = detail.get("seasons", [])
                    episodes: list[Episode] = []
                    for s in seasons:
                        season_num = s.get("season", 1)
                        if season_num is None or season_num == 0:
                            continue
                        try:
                            ep_resp = SESSION.get(
                                f"{API_BASE}/episodes/{tmdb_id}/{season_num}",
                                headers=HEADERS,
                                timeout=SCRAPE_TIMEOUT,
                            )
                            if ep_resp.status_code != 200:
                                continue
                            ep_data = ep_resp.json()
                            for ep in ep_data.get("episodes", []):
                                ep_num = ep.get("episode", 1)
                                ep_title = ep.get("title", f"Episode {ep_num}")
                                episodes.append(Episode(
                                    title=f"S{season_num} E{ep_num} - {ep_title}",
                                    url=result.url,
                                    number=f"{season_num}.{ep_num}",
                                    site_name=self.name,
                                    anime_name=title,
                                    data={
                                        "tmdb_id": tmdb_id,
                                        "media_type": "tv",
                                        "title": title,
                                        "year": data.get("year", ""),
                                        "season": season_num,
                                        "episode": ep_num,
                                    },
                                ))
                        except (requests.RequestException, json.JSONDecodeError):
                            continue
                    return episodes
            except (requests.RequestException, json.JSONDecodeError):
                pass

        return []

    def get_servers(self, episode: Episode) -> list[dict]:
        return [
            {"name": srv, "display": SERVER_NAMES.get(srv, srv), "link_id": srv, "type": "sub"}
            for srv in SERVERS
        ]

    def _try_server(self, srv: str, tmdb_id: int, media_type: str, query: dict, quality_pref: str) -> Optional[StreamSource]:
        try:
            resp = SESSION.post(
                f"{API_BASE}/stream",
                json={"srv": srv, "t": media_type, "id": str(tmdb_id), "query": query},
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            sources = body.get("sources", [])
            if not sources:
                return None

            chosen = self._pick_quality(sources, quality_pref)
            if not chosen:
                chosen = sources[0]

            url = chosen["url"]
            stream_quality = chosen.get("quality", "unknown")

            # TV shows: route through the shared HLS proxy (parallel segment
            # prefetch + urgent pool) instead of mpv's single sequential
            # connection, which the CDNs throttle hard on episode streams.
            proxy = None
            if media_type == "tv":
                try:
                    proxy = HlsProxy(url, referer="https://bingr.one/", headers=HEADERS)
                    url = proxy.master_url
                except Exception:
                    proxy = None

            subs: list[dict] = []
            for s in body.get("subtitles", []):
                u = s.get("url", "")
                if u and s.get("lang", "").lower().startswith("en"):
                    subs.append({"url": u, "label": s.get("label", ""), "lang": "en"})
            if not subs:
                try:
                    r = SESSION.get(
                        f"{API_BASE}/subtitles/vdrk/{media_type}/{tmdb_id}",
                        params={"season": query.get("season", 1), "ep": query.get("episode", 1)},
                        headers=HEADERS,
                        timeout=SCRAPE_TIMEOUT,
                    )
                    if r.status_code == 200:
                        seen = set()
                        for s in r.json().get("subtitles", []):
                            u = s.get("url", "")
                            if u and s.get("lang") == "en" and u not in seen:
                                seen.add(u)
                                subs.append({"url": u, "label": s.get("label", ""), "lang": "en"})
                except Exception:
                    pass

            return StreamSource(
                url=url,
                site_name=f"{self.name} ({SERVER_NAMES.get(srv, srv)})",
                quality=stream_quality,
                is_direct=True,
                headers=HEADERS,
                subtitles=subs or None,
                proxy_server=proxy,
            )
        except (requests.RequestException, json.JSONDecodeError, KeyError):
            return None

    def extract_stream(self, episode: Episode, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        data = episode.data
        tmdb_id = data.get("tmdb_id")
        media_type = data.get("media_type", "movie")
        title = data.get("title", "")
        year = data.get("year", "")

        if not tmdb_id:
            return None

        query: dict = {"title": title}
        if year:
            query["year"] = year
        if media_type == "tv":
            query["season"] = data.get("season", 1)
            query["episode"] = data.get("episode", 1)

        chosen_server = data.get("server_name", "")
        if chosen_server:
            for srv, display in SERVER_NAMES.items():
                if display == chosen_server or srv == chosen_server:
                    result = self._try_server(srv, tmdb_id, media_type, query, quality_pref)
                    if result:
                        return result
                    break

        for srv in SERVERS:
            result = self._try_server(srv, tmdb_id, media_type, query, quality_pref)
            if result:
                return result

        return None

    def resolve(self, media: MediaResult, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        if media.media_type != "movie":
            return None
        query = {"title": media.title, "year": media.year}
        for srv in SERVERS:
            result = self._try_server(srv, media.tmdb_id, "movie", query, quality_pref)
            if result:
                return result
        return None

    @staticmethod
    def _pick_quality(sources: list[dict], pref: str) -> Optional[dict]:
        if pref == "best":
            return sources[0] if sources else None
        target = int(pref.replace("p", ""))
        best = None
        best_diff = 99999
        for s in sources:
            q_str = s.get("quality", "0").replace("p", "")
            try:
                q = int(q_str)
            except ValueError:
                continue
            diff = abs(q - target)
            if diff < best_diff or (diff == best_diff and q > (int(best.get("quality", "0").replace("p", "")) if best else 0)):
                best = s
                best_diff = diff
        return best
