from __future__ import annotations

import json
from typing import Optional
import requests

from anime_watch.core import SESSION, SCRAPE_TIMEOUT
from anime_watch.models import MediaResult, Episode, SearchResult
from .base import BaseProvider

API_BASE = "https://api.bingr.one/api"


class TMDbProvider(BaseProvider):
    name = "TMDB"
    slug = "tmdb"
    url = "https://bingr.one"
    category = "movies"

    def search(self, query: str) -> list[SearchResult]:
        return [
            SearchResult(
                title=r.title,
                url="",
                site_name=self.name,
                year=r.year,
                poster=r.poster,
                data={"tmdb_id": r.tmdb_id, "media_type": r.media_type},
            )
            for r in search_movies(query)
        ]

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        tmdb_id = result.data.get("tmdb_id") if result.data else None
        media_type = result.data.get("media_type") if result.data else None
        if tmdb_id and media_type == "tv":
            episodes = get_tv_episodes(tmdb_id)
            for ep in episodes:
                ep.site_name = self.name
            return episodes
        return []


def search_movies(query: str) -> list[MediaResult]:
    results: list[MediaResult] = []
    for media_type in ("movie", "tv"):
        try:
            resp = SESSION.get(
                f"{API_BASE}/search",
                params={"q": query, "type": media_type},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            for item in data.get("results", []):
                tmdb_id = item.get("id")
                title = item.get("title", "")
                year = item.get("year", "")
                poster = item.get("poster", "") or ""
                results.append(MediaResult(
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    title=title,
                    year=str(year) if year else None,
                    poster=poster if poster.startswith("http") else f"https://image.tmdb.org/t/p/w500{poster}" if poster else None,
                ))
        except (requests.RequestException, json.JSONDecodeError, KeyError):
            pass
    return results

def get_tv_episodes(tmdb_id: int) -> list[Episode]:
    episodes: list[Episode] = []
    try:
        resp = SESSION.get(
            f"{API_BASE}/details/tv/{tmdb_id}",
            headers={"Origin": "https://bingr.one", "Referer": "https://bingr.one/"},
            timeout=SCRAPE_TIMEOUT,
        )
        if resp.status_code == 200:
            detail = resp.json()
            seasons = detail.get("seasons", [])
            for s in seasons:
                season_num = s.get("season", 1)
                if season_num is None or season_num == 0:
                    continue
                try:
                    ep_resp = SESSION.get(
                        f"{API_BASE}/episodes/{tmdb_id}/{season_num}",
                        headers={"Origin": "https://bingr.one", "Referer": "https://bingr.one/"},
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
                            url="",
                            number=f"{season_num}.{ep_num}",
                            site_name="TMDB",
                            anime_name="",
                            data={"tmdb_id": tmdb_id, "season": season_num, "episode": ep_num},
                        ))
                except (requests.RequestException, json.JSONDecodeError):
                    continue
    except (requests.RequestException, json.JSONDecodeError):
        pass
    return episodes
