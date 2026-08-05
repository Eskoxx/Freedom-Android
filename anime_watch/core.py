import json
import re
import shutil
import subprocess
import urllib.parse
from typing import Optional
import requests
from bs4 import BeautifulSoup
from .models import Episode, StreamSource

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SCRAPE_TIMEOUT = 8

def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def fetch_episodes_generic(anime_url: str, site_name: str) -> list[Episode]:
    episodes = []
    try:
        resp = SESSION.get(anime_url, timeout=SCRAPE_TIMEOUT)
        if resp.status_code != 200: return episodes
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        for link in soup.select('a[href*="/episode/"], a[href*="/ep-"], [class*="episode"] a[href], [id*="episode"] a[href]'):
            href = link.get("href", "")
            if href in seen: continue
            seen.add(href)
            if not re.search(r"/(?:ep(?:-|isode/))\d+", href): continue
            title = link.get_text(strip=True) or f"Episode {len(episodes) + 1}"
            en = re.search(r"(\d+)", title)
            epn = en.group(1) if en else "1"
            fu = href if href.startswith("http") else urllib.parse.urljoin(anime_url, href)
            episodes.append(Episode(title=title.strip(), url=fu, number=epn, site_name=site_name))
    except requests.RequestException:
        pass
    return episodes

def extract_with_ytdlp(url: str) -> Optional[StreamSource]:
    try:
        r = subprocess.run(["yt-dlp", "--no-warnings", "--dump-json", "--no-download", "--no-playlist", url], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout.strip().split("\n")[0])
            if data.get("url"):
                return StreamSource(url=data["url"], site_name="yt-dlp", quality=data.get("resolution", "unknown"), is_direct=True)
            fmts = data.get("formats", [])
            if fmts:
                b = fmts[-1]
                return StreamSource(url=b.get("url", ""), site_name="yt-dlp", quality=b.get("format_note", "unknown"), is_direct=True)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        pass
    return None

def scrape_page_for_video(page_url: str, site_name: str = "") -> Optional[StreamSource]:
    try:
        resp = SESSION.get(page_url, timeout=SCRAPE_TIMEOUT)
        if resp.status_code != 200: return None
        if len(resp.text) < 200 and "just a moment" in resp.text.lower(): return None
        soup = BeautifulSoup(resp.text, "lxml")
        cands = []
        for s in soup.select("video source[src]"): u = s.get("src", ""); u and cands.append((u, 100, "vs"))
        for v in soup.select("video[src]"): u = v.get("src", ""); u and cands.append((u, 90, "vt"))
        kh = ["mp4upload", "streamtape", "vidstream", "gogoanime", "yourupload", "doodstream", "dood", "embedsito", "mcloud", "mixdrop", "vidsrc", "netu", "gdriveplayer"]
        for ifr in soup.select("iframe[src]"): s = ifr.get("src", ""); any(h in s.lower() for h in kh) and cands.append((s, 80, "ki")) or (s.startswith("http") and cands.append((s, 50, "gi")))
        for a in ("data-src", "data-url", "data-video", "data-source"):
            for el in soup.select(f"[{a}]"):
                v = el.get(a, ""); v and (v.startswith("http") or v.endswith((".mp4", ".m3u8"))) and cands.append((v, 70, f"da"))
        for sc in soup.select("script"):
            t = sc.string or ""
            for u in re.findall(r'(https?://[^"\'<>]+\.(?:mp4|m3u8)(?:\?[^"\'<>]*)?)', t): cands.append((u, 60, "sm"))
        cands.sort(key=lambda x: -x[1])
        tried = set()
        for url, pri, st in cands:
            url = url.split("?")[0] if url.endswith(".mp4") else url
            if url in tried: continue
            tried.add(url)
            if re.search(r'\.(mp4|m3u8)(\?|$)', url): return StreamSource(url=url, site_name=site_name or "scrape", quality="direct", is_direct=True)
            s = extract_with_ytdlp(url)
            if s and s.url: return s
    except requests.RequestException: pass
    return None
