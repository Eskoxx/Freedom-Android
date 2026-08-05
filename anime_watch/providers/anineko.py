import html
import re
from typing import Optional
import requests
from bs4 import BeautifulSoup
from anime_watch.models import SearchResult, Episode, StreamSource
from anime_watch.core import SESSION, SCRAPE_TIMEOUT
from .base import BaseProvider

BASE = "https://anineko.to"


class AniNekoProvider(BaseProvider):
    name = "AniNeko"
    slug = "anineko"
    url = BASE
    category = "anime"

    def get_supported_qualities(self) -> list[str]:
        return ["1080p", "720p", "480p", "360p", "best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub", "dub"]

    # ---------- search ----------
    def search(self, query: str) -> list[SearchResult]:
        results = []
        ql = query.lower()
        try:
            resp = SESSION.get(
                f"{BASE}/browser",
                params={"keyword": query},
                headers={"Referer": f"{BASE}/"},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return results
            soup = BeautifulSoup(resp.text, "lxml")
            for article in soup.select("article.nv-anime-card, article[class*='browse-card'], article"):
                cls = " ".join(article.get("class") or [])
                if "anime" not in cls.lower():
                    continue
                a = article.select_one("a[href*='/watch/']")
                if not a:
                    continue
                href = a.get("href", "")
                m = re.search(r"/watch/([^/?#]+)", href)
                if not m:
                    continue
                img = a.select_one("img")
                title = (img.get("alt") or "").strip() if img else ""
                if not title:
                    title_el = article.select_one(".nv-anime-title, h3, .anime-title")
                    title = title_el.get_text(strip=True) if title_el else ""
                if not title or len(title) <= 2:
                    continue
                if ql not in title.lower() and ql not in href.lower():
                    continue
                fu = href if href.startswith("http") else f"{BASE}{href}"
                thumb = img.get("src") or img.get("data-src") or "" if img else ""
                results.append(SearchResult(
                    title=title,
                    url=fu,
                    site_name=self.name,
                    image=thumb or "",
                ))
        except requests.RequestException:
            pass
        return results

    # ---------- episodes ----------
    def get_episodes(self, result: SearchResult) -> list[Episode]:
        episodes = []
        m = re.search(r"/watch/([^/?#]+)", result.url)
        if not m:
            return episodes
        slug = m.group(1)
        an = result.title.split(" (")[0].strip()
        try:
            resp = SESSION.get(
                f"{BASE}/watch/{slug}",
                headers={"Referer": f"{BASE}/"},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return episodes
            soup = BeautifulSoup(resp.text, "lxml")
            seen = set()
            for article in soup.select("article[class*='episode-item'], article.nv-info-episode-item, article"):
                cls = " ".join(article.get("class") or [])
                if "episode" not in cls.lower():
                    continue
                link = article.select_one("a[href*='/ep-']")
                if not link:
                    continue
                href = link.get("href", "")
                nm = re.search(r"/ep-(\d+)", href)
                if not nm:
                    continue
                num = nm.group(1)
                if num in seen:
                    continue
                seen.add(num)
                title = link.get_text(strip=True) or f"Episode {num}"
                # fallback: title may be inside a span
                span = link.select_one("span")
                if span and span.get_text(strip=True):
                    title = span.get_text(strip=True)
                block_text = article.get_text(" ", strip=True).lower()
                has_sub = "sub" in block_text and "dub" not in block_text or " sub" in block_text
                has_dub = "dub" in block_text
                episodes.append(Episode(
                    title=title,
                    url=f"{BASE}/watch/{slug}/ep-{num}",
                    number=num,
                    site_name=self.name,
                    anime_name=an,
                    data={"slug": slug, "ep_slug": f"ep-{num}", "sub": 1 if has_sub else 0, "dub": 1 if has_dub else 0},
                ))
            episodes.sort(key=lambda e: int(e.number))
        except requests.RequestException:
            pass
        return episodes

    # ---------- stream ----------
    def extract_stream(self, episode: Episode, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        slug = (episode.data or {}).get("slug", "")
        ep_slug = (episode.data or {}).get("ep_slug", "")
        if not slug or not ep_slug:
            m = re.search(r"/watch/([^/?#]+)/(ep-\d+)", episode.url)
            if m:
                slug, ep_slug = m.group(1), m.group(2)
        if not slug:
            return None
        try:
            resp = SESSION.get(
                f"{BASE}/watch/{slug}/{ep_slug}",
                headers={"Referer": f"{BASE}/watch/{slug}"},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            text = resp.text
            # pick embeds for the wanted audio track
            wanted = "dub" if audio_pref == "dub" else "sub"
            panels = re.findall(
                r'<div\b[^>]*class=["\'][^"\']*nv-server-grid[^"\']*["\'][^>]*data-id=["\']([^"\']+)["\'][^>]*>([\s\S]*?)(?=<div\b[^>]*class=["\'][^"\']*nv-server-grid|$)',
                text,
            )
            embeds = []
            for _panel_id, body in panels:
                body_lower = body.lower()
                panel_audio = "dub" if "dub" in body_lower else "sub"
                if panel_audio != wanted:
                    continue
                embeds.extend(re.findall(r'data-video=["\']([^"\']+)["\']', body))
            if not embeds:
                # fallback: any data-video on the page, prefer wanted audio label proximity
                embeds = re.findall(r'data-video=["\']([^"\']+)["\']', text)
            all_subs = []
            seen_sub_urls = set()
            for embed in embeds:
                embed = html.unescape(embed)
                hls, subs = self._extract_hls(embed)
                for s_ in subs:
                    if s_["url"] not in seen_sub_urls:
                        seen_sub_urls.add(s_["url"])
                        all_subs.append(s_)
                if hls:
                    # AniNeko segments are PNG-wrapped TS (like Anikoto) —
                    # route through the strip proxy so mpv/players get clean MPEG-TS
                    from .anikoto import _proxy_hls as _strip_proxy
                    proxy_url, proxy_server = _strip_proxy(hls, self._origin(embed), "anineko")
                    if proxy_url:
                        return StreamSource(
                            url=proxy_url,
                            site_name=self.name,
                            quality=quality_pref,
                            is_direct=True,
                            headers={"Referer": self._origin(embed)},
                            subtitles=all_subs or None,
                            proxy_server=proxy_server,
                        )
                    return StreamSource(
                        url=hls,
                        site_name=self.name,
                        quality=quality_pref,
                        is_direct=True,
                        headers={"Referer": self._origin(embed)},
                        subtitles=all_subs or None,
                    )
            # last resort: return the embed page URL (yt-dlp / mpv can often handle it)
            if embeds:
                e0 = html.unescape(embeds[0])
                return StreamSource(
                    url=e0,
                    site_name=self.name,
                    quality=quality_pref,
                    is_direct=False,
                    headers={"Referer": self._origin(e0)},
                )
        except requests.RequestException:
            pass
        return None

    def _extract_hls(self, embed_url: str):
        """Return (hls_url, subtitles_list) from an AniNeko embed page."""
        try:
            resp = SESSION.get(
                embed_url,
                headers={"Referer": f"{BASE}/"},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None, []
            text = resp.text
            patterns = [
                r'const\s+src\s*=\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
                r'file\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
                r'["\'](https?://[^"\']+/master\.m3u8[^"\']*)["\']',
                r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
            ]
            hls = None
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    hls = html.unescape(m.group(1))
                    break
            subs = []
            # .vtt subtitle URLs on the embed page (anizara CDN)
            for v in re.findall(r'https?://[^"\']+\.vtt[^"\']*', text):
                u = html.unescape(v)
                lang = "en"
                lm = re.search(r"[-_]([a-z]{2,3})(?:-\d+)?\.vtt", u.lower())
                if lm:
                    lang = lm.group(1)
                subs.append({"url": u, "label": lang, "lang": lang})
            # dedupe
            seen = set()
            subs = [s for s in subs if not (s["url"] in seen or seen.add(s["url"]))]
            return hls, subs
        except requests.RequestException:
            return None, []

    @staticmethod
    def _origin(url: str) -> str:
        m = re.match(r"(https?://[^/]+)", url)
        return (m.group(1) + "/") if m else f"{BASE}/"
