from typing import Optional
from anime_watch.models import SearchResult, Episode, StreamSource, MediaResult

class BaseProvider:
    name: str = ""
    slug: str = ""
    url: str = ""
    category: str = "anime"

    def search(self, query: str) -> list[SearchResult]:
        return []

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        return []

    def resolve(self, result: SearchResult, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        return None

    def extract_stream(self, episode: Episode, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        return None

    def get_supported_qualities(self) -> list[str]:
        return ["best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub"]
