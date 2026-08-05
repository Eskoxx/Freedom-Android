from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
import string
import time
import uuid
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

import requests

from anime_watch.models import SearchResult, Episode, StreamSource, MediaResult
from .base import BaseProvider

HOST_POOL = [
    "https://api6.aoneroom.com",
    "https://api5.aoneroom.com",
    "https://api4.aoneroom.com",
    "https://api4sg.aoneroom.com",
    "https://api3.aoneroom.com",
    "https://api6sg.aoneroom.com",
    "https://api.inmoviebox.com",
]

SECRET_KEY_DEFAULT = "76iRl07s0xSN9jqmEWAt79EBJZulIQIsV64FZr2O"
RETRY_STATUS_CODES = {403, 406, 407, 429, 500, 502, 503, 504}
SEARCH_TIMEOUT = 10

ANDROID_VERSIONS = [
    ("9", "PQ3A.190605.03081104"),
    ("10", "QP1A.191005.007.A3"),
    ("11", "RP1A.200720.011"),
    ("12", "S1B.220414.015"),
    ("13", "TQ2A.230405.003"),
]

REDMI_DEVICES = [
    ("23078RKD5C", "Redmi"),
    ("2201117TY", "Redmi"),
    ("2201117TG", "Redmi"),
    ("22101316G", "Redmi"),
    ("21121210G", "Redmi"),
    ("M2012K11AG", "Redmi"),
    ("M2007J20CG", "Redmi"),
]

VERSION_CODES = [50020042, 50020043, 50020044, 50020045, 50020046]
NETWORK_TYPES = ["NETWORK_WIFI", "NETWORK_MOBILE"]
TIMEZONES = ["Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "America/New_York", "Europe/London"]


def _b64_decode(val: str) -> bytes:
    padded = val
    padding = (4 - len(padded) % 4) % 4
    padded += "=" * padding
    import base64
    return base64.b64decode(padded)


def _b64_encode(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _generate_x_client_token(ts: int) -> str:
    ts_str = str(ts)
    reversed_ts = ts_str[::-1]
    hash_val = _md5_hex(reversed_ts.encode())
    return f"{ts_str},{hash_val}"


def _sorted_query_string(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    sorted_pairs = []
    for key in sorted(params.keys()):
        for val in sorted(params[key]):
            sorted_pairs.append(f"{key}={val}")
    return "&".join(sorted_pairs)


def _build_canonical_string(method: str, accept: Optional[str], content_type: Optional[str],
                             url: str, body: Optional[str], timestamp_ms: int) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    query = _sorted_query_string(url)
    canonical_url = f"{path}?{query}" if query else path

    body_hash = ""
    body_length = ""
    if body is not None:
        body_bytes = body.encode()
        body_length = str(len(body_bytes))
        truncated = body_bytes[:102400]
        body_hash = _md5_hex(truncated)

    return f"{method.upper()}\n{accept or ''}\n{content_type or ''}\n{body_length}\n{timestamp_ms}\n{body_hash}\n{canonical_url}"


def _generate_x_tr_signature(method: str, accept: Optional[str], content_type: Optional[str],
                              url: str, body: Optional[str], timestamp_ms: int) -> str:
    canonical = _build_canonical_string(method, accept, content_type, url, body, timestamp_ms)
    secret_bytes = _b64_decode(SECRET_KEY_DEFAULT)
    mac = hmac.new(secret_bytes, canonical.encode(), hashlib.md5)
    sig_b64 = _b64_encode(mac.digest())
    return f"{timestamp_ms}|2|{sig_b64}"


def _random_hex(length: int) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(length))


def _random_uuid() -> str:
    return f"{_random_hex(8)}-{_random_hex(4)}-{_random_hex(4)}-{_random_hex(4)}-{_random_hex(12)}"


def _generate_client_info_and_ua() -> tuple[str, str]:
    android = random.choice(ANDROID_VERSIONS)
    device = random.choice(REDMI_DEVICES)
    version_code = random.choice(VERSION_CODES)
    network = random.choice(NETWORK_TYPES)
    timezone = random.choice(TIMEZONES)
    gaid = _random_uuid()
    device_id = _random_hex(32)

    user_agent = (
        f"com.community.oneroom/{version_code} (Linux; U; Android {android[0]}; en_US; "
        f"{device[0]}; Build/{android[1]}; Cronet/135.0.7012.3)"
    )

    client_info = json.dumps({
        "package_name": "com.community.oneroom",
        "version_name": "3.0.03.0529.03",
        "version_code": version_code,
        "os": "android",
        "os_version": android[0],
        "install_ch": "ps",
        "device_id": device_id,
        "install_store": "ps",
        "gaid": gaid,
        "brand": device[1],
        "model": device[0],
        "system_language": "en",
        "net": network,
        "region": "US",
        "timezone": timezone,
        "sp_code": "40401",
        "X-Play-Mode": "2",
    })

    return user_agent, client_info


def _random_spoofed_ip() -> str:
    host = random.randint(1, 253)
    return f"103.241.224.{host}"


def _build_signed_headers(method: str, url: str, body: Optional[str],
                           auth_token: Optional[str], user_agent: str,
                           client_info: str, spoofed_ip: str) -> dict[str, str]:
    ts = int(time.time() * 1000)
    accept = "application/json"
    content_type = "application/json"

    client_token = _generate_x_client_token(ts)
    signature = _generate_x_tr_signature(method, accept, content_type, url, body, ts)

    headers = {
        "User-Agent": user_agent,
        "Accept": accept,
        "Content-Type": content_type,
        "Connection": "keep-alive",
        "X-Client-Token": client_token,
        "x-tr-signature": signature,
        "X-Client-Info": client_info,
        "X-Client-Status": "0",
        "X-Forwarded-For": spoofed_ip,
    }

    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    return headers


class MovieBoxProvider(BaseProvider):
    name = "MovieBox"
    slug = "moviebox"
    url = "https://moviebox.app"
    category = "movies"

    def __init__(self):
        self._session = requests.Session()
        self._token: Optional[str] = None
        self._active_idx: int = 0
        ua, ci = _generate_client_info_and_ua()
        self._user_agent = ua
        self._client_info = ci
        self._spoofed_ip = _random_spoofed_ip()
        self._initialized = False

    def _ensure_init(self):
        if self._initialized:
            return
        for i in range(len(HOST_POOL)):
            idx = (self._active_idx + i) % len(HOST_POOL)
            base = HOST_POOL[idx]
            url = f"{base}/wefeed-mobile-bff/tab-operating?page=1&tabId=0&version="
            try:
                headers = _build_signed_headers(
                    "GET", url, None, None,
                    self._user_agent, self._client_info, self._spoofed_ip,
                )
                r = self._session.get(url, headers=headers, timeout=SEARCH_TIMEOUT)
                if r.status_code in RETRY_STATUS_CODES:
                    continue
                x_user = r.headers.get("x-user")
                if x_user:
                    try:
                        xj = json.loads(x_user)
                        tok = xj.get("token", "")
                        if tok:
                            self._token = tok
                    except (json.JSONDecodeError, KeyError):
                        pass
                self._active_idx = idx
                self._initialized = True
                return
            except (requests.RequestException, json.JSONDecodeError):
                continue

    def _request(self, method: str, path_and_query: str, body: Optional[dict] = None) -> Optional[dict]:
        self._ensure_init()
        body_str = json.dumps(body) if body else None
        start_idx = self._active_idx

        for i in range(len(HOST_POOL)):
            idx = (start_idx + i) % len(HOST_POOL)
            base = HOST_POOL[idx]
            url = f"{base}{path_and_query}"

            headers = _build_signed_headers(
                method, url, body_str, self._token,
                self._user_agent, self._client_info, self._spoofed_ip,
            )

            try:
                if method == "POST":
                    r = self._session.post(url, headers=headers, data=body_str, timeout=SEARCH_TIMEOUT)
                else:
                    r = self._session.get(url, headers=headers, timeout=SEARCH_TIMEOUT)

                if r.status_code in RETRY_STATUS_CODES:
                    continue

                x_user = r.headers.get("x-user")
                if x_user:
                    try:
                        xj = json.loads(x_user)
                        tok = xj.get("token", "")
                        if tok:
                            self._token = tok
                    except (json.JSONDecodeError, KeyError):
                        pass

                self._active_idx = idx

                try:
                    result = r.json()
                    if "data" in result and result["data"] is not None:
                        return result["data"]
                    return result
                except (json.JSONDecodeError, KeyError):
                    continue
            except requests.RequestException:
                continue
        return None

    def search(self, query: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        data = self._request("POST", "/wefeed-mobile-bff/subject-api/search/v2", {
            "keyword": query,
            "page": 1,
            "perPage": 20,
            "subjectType": "All",
            "tabId": "All",
        })
        if not data or not isinstance(data, dict):
            return results
        raw_results = data.get("results", [])
        for group in raw_results:
            if not isinstance(group, dict):
                continue
            subjects = group.get("subjects", [])
            if not isinstance(subjects, list):
                continue
            for item in subjects:
                if not isinstance(item, dict):
                    continue
                subject_id = item.get("subjectId") or item.get("id")
                if not subject_id:
                    continue
                subject_type = item.get("subjectType", 1)
                if subject_type not in (1, 2):
                    continue
                title = item.get("title", "")
                year_str = item.get("releaseDate", "")
                year = year_str[:4] if year_str else ""
                media_type = "tv" if subject_type == 2 else "movie"
                cover = item.get("cover", {}) or {}
                poster = ""
                if isinstance(cover, dict):
                    poster = cover.get("url", "")
                elif isinstance(cover, str):
                    poster = cover
                season: Optional[int] = None
                if media_type == "tv":
                    m = re.search(r'\bS(\d+)\b', title)
                    if m:
                        season = int(m.group(1))
                display = f"{title} ({year})" if year else title
                results.append(SearchResult(
                    title=display,
                    url="",
                    site_name=self.name,
                    image=poster,
                    data={
                        "subject_id": str(subject_id),
                        "media_type": media_type,
                        "title": title,
                        "year": year,
                        "season": season,
                    },
                ))
        return results

    def _get_dubs(self, subject_id: str) -> list[dict]:
        detail = self._request("GET", f"/wefeed-mobile-bff/subject-api/get?subjectId={subject_id}")
        if detail and isinstance(detail, dict):
            return detail.get("dubs", []) or []
        return []

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        data = result.data
        subject_id = data.get("subject_id")
        media_type = data.get("media_type", "movie")
        title = data.get("title", result.title)
        target_season: Optional[int] = data.get("season")

        if not subject_id:
            return []

        dubs = self._get_dubs(subject_id)
        base_data = {
            "subject_id": subject_id,
            "media_type": media_type,
            "title": title,
            "year": data.get("year", ""),
            "dubs": dubs,
            "language": data.get("language", ""),
        }

        if media_type == "movie":
            return [Episode(
                title=f"{title} (Movie)",
                url="",
                number="1",
                site_name=self.name,
                anime_name=title,
                data={**base_data},
            )]

        episodes: list[Episode] = []
        seen: set[str] = set()
        page = 1
        while True:
            resp = self._request(
                "GET",
                f"/wefeed-mobile-bff/subject-api/resource?subjectId={subject_id}&se=0&ep=0&page={page}&perPage=20"
            )
            if not resp or not isinstance(resp, dict):
                break
            items = resp.get("list", [])
            if not items:
                break
            for item in items:
                ep_code = item.get("episode", 0)
                if not ep_code or str(ep_code) in seen:
                    continue
                seen.add(str(ep_code))
                ep_code_int = int(ep_code)
                season_num = ep_code_int // 100
                if target_season is not None and season_num != target_season:
                    continue
                ep_num = ep_code_int % 100
                ep_title = item.get("title") or f"Episode {ep_num}"
                label = f"S{season_num:02d}E{ep_num:02d}"
                episodes.append(Episode(
                    title=f"{label} - {ep_title}",
                    url="",
                    number=label,
                    site_name=self.name,
                    anime_name=title,
                    data={
                        **base_data,
                        "season": season_num,
                        "episode": ep_num,
                    },
                ))
            if not resp.get("pager", {}).get("hasMore"):
                break
            page += 1
        episodes.sort(key=lambda e: e.data["season"] * 1000 + e.data["episode"])
        return episodes

    def _get_all_resources(self, subject_id: str, season: int = 0, episode: int = 0) -> list[dict]:
        all_items: list[dict] = []
        target_ep_code = season * 100 + episode
        for res in ["1080", "720", "480", "360", ""]:
            page = 1
            while True:
                path = (
                    f"/wefeed-mobile-bff/subject-api/resource?subjectId={subject_id}&se={season}&ep={episode}"
                    f"&page={page}&perPage=20{'' if not res else '&resolution=' + res}"
                )
                data = self._request("GET", path)
                if not data or not isinstance(data, dict):
                    break
                items = data.get("list", [])
                if not items:
                    break
                for item in items:
                    if season == 0 and episode == 0:
                        all_items.append(item)
                    else:
                        ep_code = item.get("episode", 0)
                        if ep_code and int(ep_code) == target_ep_code:
                            all_items.append(item)
                has_more = data.get("pager", {}).get("hasMore")
                if not has_more:
                    break
                page += 1
        return all_items

    def extract_stream(self, episode: Episode, audio_pref: str = "sub",
                       quality_pref: str = "best") -> Optional[StreamSource]:
        data = episode.data
        subject_id = data.get("subject_id")
        season = data.get("season", 0)
        ep = data.get("episode", 0)
        media_type = data.get("media_type", "movie")

        if not subject_id:
            return None

        if media_type == "movie":
            season = 0
            ep = 0

        dubs: list = data.get("dubs", []) or []
        if dubs and audio_pref != "sub":
            for dub in dubs:
                if dub.get("lanCode", "").lower() == audio_pref.lower():
                    subject_id = str(dub.get("subjectId", subject_id))
                    break

        items = self._get_all_resources(subject_id, season, ep)
        if not items:
            return None

        chosen = None
        if quality_pref != "best":
            target = int(quality_pref.replace("p", ""))
            best = None
            best_diff = 99999
            for item in items:
                q_str = str(item.get("resolution", "0"))
                try:
                    q = int(re.sub(r"\D", "", q_str))
                except (ValueError, TypeError):
                    continue
                diff = abs(q - target)
                best_q = int(re.sub(r"\D", "", str(best.get("resolution", "0")))) if best else 0
                if diff < best_diff or (diff == best_diff and q > best_q):
                    best = item
                    best_diff = diff
            chosen = best
        if not chosen:
            chosen = max(items, key=lambda x: int(re.sub(r"\D", "", str(x.get("resolution", "0"))) or 0))

        stream_url = chosen.get("resourceLink") or chosen.get("url", "")
        if not stream_url:
            return None

        stream_quality = chosen.get("resolution", str(chosen.get("quality", "unknown")))

        captions = self._request(
            "GET",
            f"/wefeed-mobile-bff/subject-api/get-ext-captions?subjectId={subject_id}&resourceId={chosen.get('resourceId', '')}"
        )

        subs = None
        caps_list = captions.get("extCaptions") if isinstance(captions, dict) else (captions if isinstance(captions, list) else [])
        if caps_list:
            subs = []
            for cap in caps_list:
                cap_url = cap.get("url") or cap.get("src", "")
                cap_lang = cap.get("lan") or cap.get("lang") or cap.get("language", "Unknown")
                cap_label = cap.get("lanName") or cap.get("label") or cap_lang
                if cap_url:
                    subs.append({"url": cap_url, "label": cap_label, "lang": cap_lang})

        return StreamSource(
            url=stream_url,
            site_name=self.name,
            quality=stream_quality,
            is_direct=True,
            subtitles=subs or None,
        )

    def get_servers(self, episode: Episode) -> list[dict]:
        dubs: list = episode.data.get("dubs", []) or []
        servers = []
        seen = set()
        for dub in dubs:
            code = dub.get("lanCode", "").lower()
            name = dub.get("lanName", "").replace(" dub", "").strip()
            if not code or not name or code in seen:
                continue
            seen.add(code)
            display = f"{name} ({code.upper()})"
            servers.append({
                "name": name,
                "display": display,
                "link_id": dub.get("subjectId", ""),
                "type": code,
            })
        return servers

    def resolve(self, media: MediaResult, audio_pref: str = "sub",
                quality_pref: str = "best") -> Optional[StreamSource]:
        if media.media_type != "movie":
            return None
        # Search for the movie by title to get subject_id
        results = self.search(media.title or "")
        for r in results:
            if r.data.get("media_type") == "movie":
                eps = self.get_episodes(r)
                if eps:
                    return self.extract_stream(eps[0], audio_pref, quality_pref)
        return None
