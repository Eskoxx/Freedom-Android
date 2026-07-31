import json
import re
import urllib.parse
from typing import Optional
import requests
from bs4 import BeautifulSoup
from anime_watch.models import SearchResult, Episode, StreamSource
from anime_watch.core import SESSION, SCRAPE_TIMEOUT, extract_with_ytdlp, scrape_page_for_video
from .base import BaseProvider

class AniDBProvider(BaseProvider):
    name = "AniDB"
    slug = "anidb"
    url = "https://anidb.app"
    category = "anime"
    
    def get_supported_qualities(self) -> list[str]:
        return ["1080p", "720p", "360p", "best"]
        
    def get_supported_audio(self) -> list[str]:
        return ["sub", "dub"]
        
    def search(self, query: str) -> list[SearchResult]:
        results = []
        try:
            resp = SESSION.get(f"{self.url}/search/suggestions", params={"q": query}, timeout=SCRAPE_TIMEOUT)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                for link in soup.select('a[href*="/anime/"]'):
                    href = link.get("href", "")
                    te = link.select_one("p")
                    title = te.get_text(strip=True) if te else link.get("title", "")
                    if title and len(title) > 2 and query.lower() in title.lower():
                        fu = href if href.startswith("http") else urllib.parse.urljoin(self.url, href)
                        results.append(SearchResult(title=title, url=fu, site_name=self.name))
            if results:
                seen_urls = {r.url for r in results}
                extras = self._expand_seasons(results[0].url, seen_urls)
                results.extend(extras)
        except requests.RequestException:
            pass
        return results

    def _expand_seasons(self, url: str, seen: set[str]) -> list[SearchResult]:
        extras = []
        try:
            resp = SESSION.get(url, timeout=SCRAPE_TIMEOUT)
            if resp.status_code != 200:
                return extras
            soup = BeautifulSoup(resp.text, "lxml")
            for div in soup.find_all("div"):
                text = div.get_text(strip=True)
                if text.startswith("Seasons") and "entries" in text:
                    for a in div.find_all("a", href=True):
                        href = a["href"]
                        if "/anime/" not in href:
                            continue
                        fu = href if href.startswith("http") else urllib.parse.urljoin(self.url, href)
                        if fu in seen:
                            continue
                        raw = a.get_text(strip=True)
                        clean = re.sub(r"^\d+", "", raw)
                        clean = re.sub(r"\d{4}$", "", clean).strip()
                        clean = re.sub(r"^Now", "", clean).strip()
                        if clean and len(clean) > 2:
                            seen.add(fu)
                            extras.append(SearchResult(title=clean, url=fu, site_name=self.name))
                    break
        except requests.RequestException:
            pass
        return extras

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        episodes = []
        an = result.title.split(" (")[0].strip()
        m = re.search(r"-(\d+)$", result.url.rstrip("/"))
        if not m: return episodes
        try:
            resp = SESSION.get(f"{self.url}/api/frontend/anime/{m.group(1)}/episodes", timeout=SCRAPE_TIMEOUT)
            if resp.status_code == 200:
                for ep in resp.json().get("episodes", []):
                    en = ep.get("number", 0)
                    et = ep.get("title", "") or f"Episode {en}"
                    episodes.append(Episode(title=et, url=result.url, number=str(en), site_name=self.name, anime_name=an))
        except (requests.RequestException, json.JSONDecodeError):
            pass
        return episodes

    def extract_stream(self, episode: Episode, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        m = re.search(r"-(\d+)$", episode.url.rstrip("/"))
        if not m: return None
        try:
            ep_resp = SESSION.get(f"{self.url}/api/frontend/anime/{m.group(1)}/episodes", timeout=SCRAPE_TIMEOUT)
            if ep_resp.status_code != 200: return None
            eps = ep_resp.json().get("episodes", [])
            if not eps: return None
            ep = next((e for e in eps if e.get("number") == int(episode.number) or e.get("id") == int(episode.number)), None)
            if not ep: return None
            
            lang_resp = SESSION.get(f"{self.url}/api/frontend/episode/{ep['id']}/languages", timeout=SCRAPE_TIMEOUT)
            if lang_resp.status_code != 200: return None
            langs = lang_resp.json().get("languages", [])
            if not langs: return None
            
            pref_code = "jpn" if audio_pref == "sub" else "eng"
            
            lo = next((l for l in langs if l.get("code") == pref_code), None)
            if not lo and pref_code != "jpn":
                lo = next((l for l in langs if l.get("code") == "jpn"), None)
            if not lo:
                lo = langs[0]
                
            embed = lo.get("embed_url", "")
            if not embed: return None
            
            # Scrape first to get the master.m3u8 which contains all resolutions
            s = scrape_page_for_video(embed, self.name)
            if s and s.url: return s
            
            # Fallback to yt-dlp which might just grab the highest resolution stream
            s = extract_with_ytdlp(embed)
            if s and s.url: return s
        except (requests.RequestException, json.JSONDecodeError, KeyError): 
            pass
        return None
