from __future__ import annotations
import re
import time
import urllib.parse
from typing import Optional
import requests
from bs4 import BeautifulSoup

from anime_watch.core import SESSION, SCRAPE_TIMEOUT
from anime_watch.models import TorrentResult


def _build_magnet(info_hash: str, name: str) -> str:
    dn = urllib.parse.quote(name)
    trs = [
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://open.demonii.com:1337/announce",
        "udp://tracker.openbittorrent.com:6969/announce",
        "udp://tracker.coppersurfer.tk:6969/announce",
        "udp://exodus.desync.com:6969/announce",
        "http://tracker.opentrackr.org:1337/announce",
        "https://tracker.tamersunion.org:443/announce",
    ]
    tr_str = "".join(f"&tr={urllib.parse.quote(t)}" for t in trs)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={dn}{tr_str}"


def _parse_size(text: str) -> int:
    text = text.strip().upper()
    m = re.match(r"([\d.]+)\s*(K|M|G|T)?I?B?", text)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2) or "B"
    return int(val * {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[unit])


# ─── The Pirate Bay (apibay.org) ──────────────────────────────────
def search_tpb(query: str) -> list[TorrentResult]:
    try:
        api_url = f"https://apibay.org/q.php?q={urllib.parse.quote(query)}"
        resp = SESSION.get(api_url, timeout=SCRAPE_TIMEOUT)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        results = []
        for item in data:
            info_hash = (item.get("info_hash") or "").lower()
            if not info_hash or info_hash == "0000000000000000000000000000000000000000":
                continue
            name = item.get("name") or "Unknown"
            results.append(TorrentResult(
                info_hash=info_hash,
                name=name,
                size_bytes=int(item.get("size", 0)),
                seeders=int(item.get("seeders", 0)),
                leechers=int(item.get("leechers", 0)),
                source="TPB",
                magnet=_build_magnet(info_hash, name),
                added=int(item["added"]) if item.get("added") else None,
            ))
        results.sort(key=lambda r: r.seeders, reverse=True)
        return results
    except (requests.RequestException, ValueError, TypeError):
        return []


# ─── EZTV (eztvx.to API) ─────────────────────────────────────────
def search_eztv(query: str) -> list[TorrentResult]:
    try:
        resp = SESSION.get(f"https://eztvx.to/api/get-torrents?limit=50&page=1&search={urllib.parse.quote(query)}",
                           timeout=SCRAPE_TIMEOUT)
        if resp.status_code != 200:
            return []
        data = resp.json()
        results = []
        for t in data.get("torrents") or []:
            info_hash = (t.get("hash") or "").lower()
            if not info_hash:
                continue
            name = t.get("title") or t.get("filename") or "Unknown"
            magnet = t.get("magnet_url") or _build_magnet(info_hash, name)
            results.append(TorrentResult(
                info_hash=info_hash,
                name=name,
                size_bytes=int(t.get("size_bytes", 0)),
                seeders=t.get("seeds", 0),
                leechers=t.get("peers", 0),
                source="EZTV",
                magnet=magnet,
                added=t.get("date_released_unix"),
            ))
        results.sort(key=lambda r: r.seeders, reverse=True)
        return results
    except (requests.RequestException, ValueError, TypeError):
        return []


# ─── Nyaa.si (RSS) ────────────────────────────────────────────────
def search_nyaa(query: str) -> list[TorrentResult]:
    try:
        params = {"page": "rss", "q": query, "c": "0_0", "f": "0"}
        resp = SESSION.get("https://nyaa.si/", params=params, timeout=SCRAPE_TIMEOUT)
        if resp.status_code != 200:
            return []
        results = []
        for item in resp.text.split("<item>")[1:]:
            info_hash = _nyaa_tag(item, "nyaa:infoHash").lower()
            name = _nyaa_unescape(_nyaa_tag(item, "title"))
            if not info_hash or not name:
                continue
            seeders = int(_nyaa_tag(item, "nyaa:seeders") or 0)
            leechers = int(_nyaa_tag(item, "nyaa:leechers") or 0)
            size_text = _nyaa_tag(item, "nyaa:size")
            size_bytes = _parse_size(size_text) if size_text else 0
            results.append(TorrentResult(
                info_hash=info_hash,
                name=name,
                size_bytes=size_bytes,
                seeders=seeders,
                leechers=leechers,
                source="Nyaa",
                magnet=_build_magnet(info_hash, name),
            ))
        results.sort(key=lambda r: r.seeders, reverse=True)
        return results
    except requests.RequestException:
        return []


def _nyaa_tag(text: str, name: str) -> str:
    m = re.search(f'<{name}>(?:<!\\[CDATA\\[)?(.*?)(?:\\]\\]>)?</{name}>', text, re.S)
    return m.group(1).strip() if m else ""


def _nyaa_unescape(text: str) -> str:
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"")
    return text


# ─── Multi-source search (concurrent, with per-source timeout) ───

