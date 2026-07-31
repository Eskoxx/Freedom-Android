from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class TorrentResult:
    info_hash: str
    name: str
    size_bytes: int
    seeders: int
    leechers: int
    source: str
    magnet: str
    added: Optional[int] = None

    @property
    def size_str(self) -> str:
        b = self.size_bytes
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} PB"

@dataclass
class Site:
    name: str
    slug: str
    url: str = ""
    rank: int = 0
    category: str = "anime"
    def __post_init__(self):
        self.url = self.url.rstrip("/")

@dataclass
class SearchResult:
    title: str
    url: str
    site_name: str
    episode: str = "1"
    image: str = ""
    data: dict = field(default_factory=dict)

@dataclass
class Episode:
    title: str
    url: str
    number: str
    site_name: str
    anime_name: str = ""
    category: str = ""
    data: dict = field(default_factory=dict)

@dataclass
class StreamSource:
    url: str
    site_name: str
    quality: str = "unknown"
    is_direct: bool = False
    headers: Optional[dict[str, str]] = None
    no_ytdl: bool = False
    subtitles: Optional[list[dict[str, str]]] = None
    raw_playlist: Optional[str] = None
    proxy_server: Optional[object] = None
    cleanup_paths: Optional[list[str]] = None
    extra_mpv_args: Optional[list[str]] = None

@dataclass
class SearchResultGroup:
    title: str
    results: list[SearchResult] = field(default_factory=list)

    @property
    def providers(self) -> str:
        return " + ".join(r.site_name for r in self.results)

@dataclass
class MediaResult:
    tmdb_id: int
    media_type: str
    title: str
    year: Optional[str] = None
    poster: Optional[str] = None
    imdb_id: Optional[str] = None
