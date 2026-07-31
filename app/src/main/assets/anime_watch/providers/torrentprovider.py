from __future__ import annotations
from anime_watch.models import TorrentResult
from anime_watch.torrent import search as ts

class TPBProvider:
    name = "TPB"
    def search(self, query: str) -> list[TorrentResult]:
        return ts.search_tpb(query)
    def get_supported_qualities(self): return ["best"]
    def get_supported_audio(self): return ["sub"]

class EZTVProvider:
    name = "EZTV"
    def search(self, query: str) -> list[TorrentResult]:
        return ts.search_eztv(query)
    def get_supported_qualities(self): return ["best"]
    def get_supported_audio(self): return ["sub"]

class NyaaProvider:
    name = "Nyaa"
    def search(self, query: str) -> list[TorrentResult]:
        return ts.search_nyaa(query)
    def get_supported_qualities(self): return ["best"]
    def get_supported_audio(self): return ["sub"]
