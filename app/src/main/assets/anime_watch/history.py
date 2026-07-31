from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import json
import os
import time

HISTORY_FILE = "history.json"


def _history_path() -> str:
    home = os.environ.get("HOME") or os.path.expanduser("~") or "."
    return os.path.join(os.path.abspath(home), HISTORY_FILE)


@dataclass
class HistoryEntry:
    anime_name: str
    episode_title: str
    episode_number: str
    site_name: str
    url: str
    data: dict = field(default_factory=dict)
    timestamp: float = 0.0
    progress: float = 0.0
    duration: float = 0.0

    @property
    def display(self) -> str:
        return f"{self.anime_name} — Ep {self.episode_number}"

    @property
    def is_finished(self) -> bool:
        if self.duration <= 0:
            return False
        return self.progress >= self.duration * 0.95

    @property
    def progress_pct(self) -> float:
        if self.duration <= 0:
            return 0.0
        return min(100.0, self.progress / self.duration * 100.0)


def load_history() -> list[HistoryEntry]:
    path = _history_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            return []
        return [HistoryEntry(**e) for e in raw if isinstance(e, dict)]
    except (json.JSONDecodeError, OSError, TypeError):
        return []


def save_history(entries: list[HistoryEntry]):
    path = _history_path()
    try:
        raw = [asdict(e) for e in entries]
        with open(path, "w") as f:
            json.dump(raw, f, indent=2)
    except OSError:
        import sys
        print(f"[anime_watch] Failed to save history to {path}", file=sys.stderr)


def add_entry(entry: HistoryEntry):
    entries = load_history()
    key = (entry.anime_name.lower().strip(),
           entry.episode_number,
           entry.site_name.lower().strip())
    for i, e in enumerate(entries):
        ek = (e.anime_name.lower().strip(),
              e.episode_number,
              e.site_name.lower().strip())
        if ek == key:
            entries[i] = entry
            save_history(entries)
            return
    entries.insert(0, entry)
    save_history(entries)


def get_history(limit: int = 100) -> list[HistoryEntry]:
    entries = load_history()
    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return entries[:limit]


def get_continue_watching(limit: int = 5) -> list[HistoryEntry]:
    entries = load_history()
    candidates = [e for e in entries
                  if e.duration > 0 and not e.is_finished]
    seen = set()
    deduped = []
    for e in candidates:
        key = e.anime_name.lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    deduped.sort(key=lambda e: e.timestamp, reverse=True)
    return deduped[:limit]


def clear_history():
    save_history([])
