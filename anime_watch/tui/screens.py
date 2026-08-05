import asyncio, json, os, re, shutil, subprocess, threading
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import Screen
from textual.widgets import Input, Static, Button
from textual.binding import Binding

from anime_watch.models import SearchResultGroup, TorrentResult
from anime_watch.providers import ANIME_PROVIDERS, CONFIGURED_PROVIDERS, CONFIGURED_SITES, MOVIE_PROVIDERS, TORRENT_PROVIDERS, search_configured, get_episodes
from anime_watch.tui.widgets import LogoWidget, RuleWidget, SidebarWidget, ResultsPanel, DownloadsPanel, HistoryPanel, FooterHints, C, SD, SA, SA_B, ST, SG, SW, ICO
from anime_watch.history import HistoryEntry, add_entry as add_history_entry, get_continue_watching, get_history
from anime_watch.tui.player import PlaybackHandler
from anime_watch.tui.downloader import DownloadHandler

_request_log_ctx = threading.local()

def _current_version() -> str:
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".update-version")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""

def _ensure_meta(torrent, dest):
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, ".meta.json"), "w") as f:
        json.dump({"info_hash": torrent.info_hash, "magnet": torrent.magnet, "name": torrent.name}, f)

def _remove_meta(dest):
    p = os.path.join(dest, ".meta.json")
    if os.path.exists(p):
        os.remove(p)

def _check_alive(url: str, timeout: int = 5) -> bool | None:
    try:
        import requests
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code < 500
    except Exception:
        return False

def _check_server_alive(provider, episode, link_id: str) -> bool | None:
    try:
        from anime_watch.core import SESSION, SCRAPE_TIMEOUT
        if hasattr(provider, 'url') and getattr(provider, 'slug', '') == 'anikoto':
            resp = SESSION.get(
                f"{provider.url}/ajax/server?get={link_id}",
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": episode.url},
                timeout=8,
            )
            if resp.status_code == 200:
                body = resp.json()
                return body.get("status") == 200 and bool(body.get("result", {}).get("url"))
        return None
    except Exception:
        return False

class SplashScreen(Screen):
    BINDINGS = [
        Binding("escape", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("h", "view_history", "History"),
    ]

    def compose(self) -> ComposeResult:
        with Container(classes="splash-root"):
            with Vertical(classes="splash-center"):
                yield LogoWidget()
                yield Static("", classes="spacer")
                with Horizontal(classes="splash-toggle-row"):
                    yield Button("Anime", id="splash-cat-anime", classes="cat-btn")
                    yield Button("Movies", id="splash-cat-movies", classes="cat-btn")
                    yield Button("Torrent", id="splash-cat-torrent", classes="cat-btn")
                with Horizontal(id="torrent-branch", classes="branch-row"):
                    yield Button("├── Anime", id="splash-sub-anime", classes="branch-btn")
                    yield Button("└── Movies", id="splash-sub-movies", classes="branch-btn")
                with Horizontal(id="provider-row", classes="branch-row"):
                    yield Static("Pick a provider", id="provider-label", classes="branch-btn")
                yield Input(placeholder="❯ Search anime…", id="splash-search")
                yield Static("", classes="spacer-sm")
                with Horizontal(classes="splash-hints-row"):
                    yield Static("↵", classes="hint-key")
                    yield Static(" search  ·  ", classes="hint-text")
                    yield Button("downloads", id="splash-downloads-btn", classes="hint-btn")
                    yield Static("  ·  ", classes="hint-text")
                    yield Button("history", id="splash-history-btn", classes="hint-btn")
                    yield Static("  ·  ", classes="hint-text")
                    yield Static("^c", classes="hint-key")
                    yield Static(" quit", classes="hint-text")
                yield Static("", classes="spacer-sm")
                yield Container(id="continue-watching", classes="continue-box")
                yield Static(f"v{_current_version()}", classes="splash-hint-text")

    def on_mount(self):
        self._set_category(self.app.search_category)
        self.query_one("#splash-search", Input).focus()
        self._refresh_continue_watching()

    def _populate_providers(self, cat: str):
        from anime_watch.providers import ANIME_PROVIDERS, MOVIE_PROVIDERS, TORRENT_PROVIDERS
        row = self.query_one("#provider-row")
        if cat in ("torrent", "torrent-anime", "torrent-movies"):
            providers = TORRENT_PROVIDERS
        elif cat == "anime":
            providers = ANIME_PROVIDERS
        else:
            providers = MOVIE_PROVIDERS
        existing = {c.id for c in row.children if c.id and c.id.startswith("sp-prov-")}
        need = {f"sp-prov-{slug}" for slug in providers}
        if existing == need:
            return
        for child in list(row.children):
            child.remove()
        for slug in sorted(providers.keys()):
            btn = Button(providers[slug].name, id=f"sp-prov-{slug}", classes="branch-btn")
            row.mount(btn)
        row.add_class("visible")

    def _set_category(self, cat: str):
        self.app.search_category = cat
        has_branch = cat in ("torrent", "torrent-anime", "torrent-movies")
        branch = self.query_one("#torrent-branch")
        if has_branch:
            branch.add_class("visible")
        else:
            branch.remove_class("visible")
        if cat == "torrent":
            self.query_one("#splash-search", Input).placeholder = "❯ Select a torrent type…"
        else:
            self.query_one("#splash-search", Input).placeholder = f"❯ Search {cat.replace('torrent-', 'torrent ')}…"
        for btn_id in ["splash-cat-anime", "splash-cat-movies", "splash-cat-torrent"]:
            self.query_one(f"#{btn_id}").remove_class("active")
        self.query_one("#splash-sub-anime").remove_class("active")
        self.query_one("#splash-sub-movies").remove_class("active")
        if cat in ("anime", "movies"):
            self.query_one(f"#splash-cat-{cat}").add_class("active")
        elif cat == "torrent-anime":
            self.query_one("#splash-cat-torrent").add_class("active")
            self.query_one("#splash-sub-anime").add_class("active")
        elif cat == "torrent-movies":
            self.query_one("#splash-cat-torrent").add_class("active")
            self.query_one("#splash-sub-movies").add_class("active")
        elif cat == "torrent":
            self.query_one("#splash-cat-torrent").add_class("active")
        self._populate_providers(cat)

    def _refresh_continue_watching(self):
        cw = get_continue_watching(limit=7)
        box = self.query_one("#continue-watching", Container)
        for child in list(box.children):
            child.remove()
        if not cw:
            return
        title = Static(Text(" Continue Watching", style=SA_B))
        box.mount(title)
        for entry in cw:
            pct = entry.progress_pct
            label = f"  {entry.display}  [{pct:.0f}%]"
            safe_id = "cw-" + re.sub(r'[^a-zA-Z0-9_-]', '_', entry.anime_name)[:50]
            btn = Button(label, id=safe_id, classes="cw-btn")
            btn._cw_entry = entry
            box.mount(btn)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "splash-cat-anime":
            self._set_category("anime")
        elif event.button.id == "splash-cat-movies":
            self._set_category("movies")
        elif event.button.id == "splash-cat-torrent":
            self._set_category("torrent")
        elif event.button.id == "splash-sub-anime":
            self._set_category("torrent-anime")
            self.query_one("#splash-search", Input).focus()
        elif event.button.id == "splash-sub-movies":
            self._set_category("torrent-movies")
            self.query_one("#splash-search", Input).focus()
        elif event.button.id and event.button.id.startswith("sp-prov-"):
            slug = event.button.id.replace("sp-prov-", "")
            from anime_watch.providers import set_target_provider
            set_target_provider(slug)
            for btn in self.query("#provider-row .branch-btn"):
                btn.remove_class("active")
            event.button.add_class("active")
            if slug == "netmirror":
                provider = CONFIGURED_PROVIDERS.get("netmirror")
                if provider and hasattr(provider, '_cookies_valid') and not provider._cookies_valid():
                    self.app.push_screen(NetmirrorLoginPrompt())
            self.query_one("#splash-search", Input).focus()
        elif event.button.id == "splash-downloads-btn":
            self.app.push_screen(DownloadsScreen())
        elif event.button.id == "splash-history-btn":
            self.app.push_screen(HistoryScreen())
        elif event.button.id and event.button.id.startswith("cw-"):
            entry = getattr(event.button, "_cw_entry", None)
            if entry:
                self._continue_watching(entry)

    def _continue_watching(self, entry: HistoryEntry):
        from anime_watch.models import Episode
        data = entry.data.copy()
        if entry.progress > 0:
            data["_resume_at"] = entry.progress
        ep = Episode(
            title=entry.episode_title,
            url=entry.url,
            number=entry.episode_number,
            site_name=entry.site_name,
            anime_name=entry.anime_name,
            data=data,
        )
        self.app.search_query = entry.anime_name
        self.app._direct_play_episode = ep
        self.app.switch_screen(BrowserScreen())

    def on_input_submitted(self, event: Input.Submitted):
        q = event.value.strip()
        if not q:
            return
        cat = self.app.search_category
        if cat == "torrent":
            self.query_one("#splash-search", Input).placeholder = "❯ Pick Anime or Movies above first"
            return
        self.app.search_query = q
        self.app.switch_screen(BrowserScreen())

    def action_quit(self):
        self.app.exit()

    def action_view_history(self):
        self.app.push_screen(HistoryScreen())

class DownloadsScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("up", "move_up", "", priority=True),
        Binding("down", "move_down", "", priority=True),
        Binding("k", "move_up", ""),
        Binding("j", "move_down", ""),
        Binding("enter", "activate", ""),
        Binding("p", "pause_resume", ""),
        Binding("x", "cancel", ""),
        Binding("d", "delete_file", "Delete"),
        Binding("w", "watch_file", ""),
    ]

    def __init__(self):
        super().__init__()
        self._current_path = ""

    def compose(self) -> ComposeResult:
        with Container(classes="browser-root"):
            with Horizontal(classes="top-bar"):
                yield LogoWidget()
                yield Static("  Downloads Library", id="status-bar", classes="status")
            yield RuleWidget(classes="rule")
            with Horizontal(classes="body"):
                with Vertical(classes="content-area"):
                    with Container(classes="panel-wrap"):
                        yield DownloadsPanel(id="downloads-list")
                    with Horizontal(id="dl-actions"):
                        yield Button("Pause", id="dl-pause", classes="dl-btn")
                        yield Button("Watch", id="dl-watch", classes="dl-btn")
                        yield Button("Cancel", id="dl-cancel", classes="dl-btn")
            yield FooterHints(id="footer")

    def on_mount(self):
        self._update_footer()
        self.refresh_downloads()
        dl_panel = self.query_one("#downloads-list", DownloadsPanel)
        dl_panel.focus()
        self.set_interval(2.0, self.refresh_downloads)

    def _dl_dir(self):
        return os.path.join("downloads", self._current_path).rstrip("/")

    def refresh_downloads(self):
        import os
        items = []
        base = self._dl_dir()
        seen_hashes = set()
        seen_folders = set()

        # Ongoing section
        ongoing_added = False

        # Active torrents (in-memory)
        if not self._current_path:
            for info_hash, meta in list(getattr(self.app, "torrents", {}).items()):
                if not ongoing_added:
                    items.append({"type": "section_header", "title": "Ongoing Downloads"})
                    ongoing_added = True
                title = meta.get("name", info_hash[:8])
                status = meta.get("status", "Downloading")
                if meta.get("paused"):
                    status = f"⏸ PAUSED — {status}"
                safe = re.sub(r'[^a-zA-Z0-9 _-]', '', title)[:60]
                dest = os.path.join("downloads", "torrents", safe)
                items.append({"title": title, "status": status, "info_hash": info_hash, "dest": dest, "type": "torrent"})
                seen_hashes.add(info_hash)

            # Stale / resumable torrents (disk meta, no active process)
            meta_dir = os.path.join("downloads", "torrents")
            if os.path.isdir(meta_dir):
                for entry in sorted(os.listdir(meta_dir)):
                    full = os.path.join(meta_dir, entry)
                    meta_file = os.path.join(full, ".meta.json")
                    if not (os.path.isdir(full) and os.path.isfile(meta_file)):
                        continue
                    try:
                        with open(meta_file) as f:
                            m = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        continue
                    info_hash = m.get("info_hash", "")
                    if info_hash in seen_hashes:
                        continue
                    seen_hashes.add(info_hash)
                    seen_folders.add(entry)
                    if not ongoing_added:
                        items.append({"type": "section_header", "title": "Ongoing Downloads"})
                        ongoing_added = True
                    items.append({
                        "title": m.get("name", entry),
                        "status": "Paused (exit → resume)",
                        "info_hash": info_hash,
                        "magnet": m.get("magnet", ""),
                        "dest": full,
                        "type": "resumable",
                    })

            for ep_title, prog_str in getattr(self.app, "downloads", {}).items():
                if not ongoing_added:
                    items.append({"type": "section_header", "title": "Ongoing Downloads"})
                    ongoing_added = True
                items.append({"title": ep_title, "status": prog_str, "path": None, "type": "dl"})

        # Completed section
        if os.path.isdir(base):
            entries = sorted(os.listdir(base))
            completed = []
            for entry in entries:
                if entry in seen_folders or entry == ".meta.json":
                    continue
                full = os.path.join(base, entry)
                if os.path.isdir(full):
                    completed.append({"title": entry, "status": "Folder", "path": full, "type": "folder"})
                elif entry.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
                    completed.append({"title": entry, "status": "Completed", "path": os.path.abspath(full), "type": "file"})
            if completed:
                items.append({"type": "section_header", "title": "Completed"})
                items.extend(completed)

        dl_panel = self.query_one("#downloads-list", DownloadsPanel)
        prev_id = None
        if dl_panel._items and 0 <= dl_panel.cursor < len(dl_panel._items):
            prev = dl_panel._items[dl_panel.cursor]
            prev_id = prev.get("info_hash") or prev.get("title")

        dl_panel.set_items(items)

        if prev_id:
            for i, item in enumerate(items):
                if item.get("info_hash") == prev_id or (item.get("title") == prev_id and item.get("type") != "section_header"):
                    dl_panel.cursor = i
                    break
        dl_panel._fix_cursor()
        self._update_actions()

    def _update_actions(self):
        actions = self.query_one("#dl-actions", Horizontal)
        item = self._get_current_dl_item()
        if self._current_path or not item or item.get("type") not in ("torrent", "resumable"):
            actions.remove_class("visible")
            return
        actions.add_class("visible")
        pause_btn = self.query_one("#dl-pause", Button)
        watch_btn = self.query_one("#dl-watch", Button)
        if item.get("type") == "resumable":
            pause_btn.label = "Resume"
            watch_btn.display = True if self._find_video_in_dest(item.get("dest", "")) else False
            return
        watch_btn.display = True if self._find_video_in_dest(item.get("dest", "")) else False
        meta = self.app.torrents.get(item.get("info_hash", ""), {})
        pause_btn.label = "Resume" if meta.get("paused") else "Pause"

    def _find_video_in_dest(self, dest):
        if not dest or not os.path.isdir(dest):
            return None
        videos = []
        for root, dirs, files in os.walk(dest):
            for f in files:
                if f.lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov", ".ts", ".m4v")):
                    videos.append(os.path.join(root, f))
        if not videos:
            return None
        return max(videos, key=os.path.getsize)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "dl-watch":
            self._watch_torrent_file()
            self._update_actions()
            return
        item = self._get_current_dl_item()
        if item and item.get("type") == "resumable":
            if event.button.id == "dl-pause":
                self._resume_torrent(item)
            elif event.button.id == "dl-cancel":
                self.action_cancel()
            self._update_actions()
            return
        if event.button.id == "dl-pause":
            self.action_pause_resume()
        elif event.button.id == "dl-cancel":
            self.action_cancel()
        self._update_actions()

    def _watch_torrent_file(self):
        item = self._get_current_dl_item()
        if not item:
            return
        dest = item.get("dest", "")
        path = self._find_video_in_dest(dest)
        if not path:
            return
        sb = self.query_one("#status-bar", Static)
        title = item.get("title", "Torrent")[:40]
        sb.update(f"  Playing: {title}")
        self.app.run_worker(self._play_local(path, item.get("title", "")))

    def _update_footer(self):
        footer = self.query_one("#footer", FooterHints)
        if self._current_path:
            footer.set_hints([
                ("↑↓", "Navigate"), ("↵", "Play"),
                ("d", "Delete"),
                ("esc", "Back to Folders"), ("q", "Quit"),
            ])
        else:
            item = self._get_current_dl_item()
            if item and item.get("type") == "resumable":
                footer.set_hints([
                    ("↑↓", "Navigate"), ("↵", "Resume Download"),
                    ("w", "Watch Partial"), ("p", "Resume"), ("x", "Remove"),
                    ("d", "Delete Files"),
                    ("esc", "Back to Search"), ("q", "Quit"),
                ])
            elif item and item.get("type") in ("torrent",):
                footer.set_hints([
                    ("↑↓", "Navigate"), ("↵", "Open Folder / Play"),
                    ("w", "Watch Partial"), ("p", "Pause/Resume"), ("x", "Cancel"),
                    ("d", "Delete Files"),
                    ("esc", "Back to Search"), ("q", "Quit"),
                ])
            elif item and item.get("type") in ("file", "folder"):
                footer.set_hints([
                    ("↑↓", "Navigate"), ("↵", "Play"),
                    ("d", "Delete"),
                    ("esc", "Back to Folders"), ("q", "Quit"),
                ])
            else:
                footer.set_hints([
                    ("↑↓", "Navigate"), ("↵", "Open Folder / Play"),
                    ("p", "Pause/Resume"), ("x", "Cancel Download"),
                    ("d", "Delete Files"),
                    ("esc", "Back to Search"), ("q", "Quit"),
                ])

    def _get_current_dl_item(self):
        dl_panel = self.query_one("#downloads-list", DownloadsPanel)
        idx = dl_panel.cursor
        items = dl_panel._items
        if idx < len(items):
            return items[idx]
        return None

    def on_base_list_panel_activated(self, event):
        self.action_activate()

    def action_back(self):
        if self._current_path:
            self._current_path = os.path.dirname(self._current_path.rstrip("/"))
            label = os.path.basename(self._current_path) if self._current_path else "Downloads Library"
            sb = self.query_one("#status-bar", Static)
            sb.update(f"  {label}")
            self.refresh_downloads()
            self._update_footer()
            self._update_actions()
        else:
            self.app.pop_screen()

    def action_quit(self):
        self.app.exit()

    def action_move_up(self):
        self.query_one("#downloads-list", DownloadsPanel).move_up()
        self._update_actions()

    def action_move_down(self):
        self.query_one("#downloads-list", DownloadsPanel).move_down()
        self._update_actions()

    def action_activate(self):
        dl_panel = self.query_one("#downloads-list", DownloadsPanel)
        idx = dl_panel.cursor
        items = dl_panel._items
        if idx >= len(items):
            return
        item = items[idx]
        typ = item.get("type")
        if typ == "section_header":
            return
        if typ == "folder":
            name = item["title"]
            self._current_path = os.path.join(self._current_path, name) if self._current_path else name
            sb = self.query_one("#status-bar", Static)
            sb.update(f"  {self._current_path}")
            self.refresh_downloads()
            self._update_footer()
        elif typ == "file":
            path = item.get("path")
            if path:
                self.app.run_worker(self._play_local(path, item["title"]))
        elif typ == "resumable":
            self._resume_torrent(item)
        self._update_actions()

    def _resume_torrent(self, item):
        magnet = item.get("magnet", "")
        info_hash = item.get("info_hash", "")
        name = item.get("title", "Unknown")
        if not magnet or not info_hash:
            return
        from anime_watch.models import TorrentResult
        t = TorrentResult(name=name, magnet=magnet, info_hash=info_hash, source="", seeders=0, leechers=0, size_bytes=0)
        for screen in self.app.screen_stack:
            if hasattr(screen, '_start_watch_and_download'):
                screen._start_watch_and_download(t)
                return
        # Fallback: push a BrowserScreen to handle the download
        from anime_watch.tui.screens import BrowserScreen
        bs = BrowserScreen()
        self.app.push_screen(bs)
        bs._start_watch_and_download(t)

    def action_pause_resume(self):
        if self._current_path:
            return
        item = self._get_current_dl_item()
        if not item or item.get("type") not in ("torrent", "resumable"):
            return
        if item.get("type") == "resumable":
            self._resume_torrent(item)
            return
        info_hash = item.get("info_hash")
        if not info_hash:
            return
        from anime_watch.torrent.engine import get_engine
        engine = get_engine()
        meta = self.app.torrents.get(info_hash)
        if not meta:
            return
        if meta.get("paused"):
            engine.resume(info_hash)
            meta["paused"] = False
        else:
            engine.pause(info_hash)
            meta["paused"] = True
        self.refresh_downloads()
        self._update_actions()

    def action_cancel(self):
        if self._current_path:
            return
        item = self._get_current_dl_item()
        if not item or item.get("type") not in ("torrent", "resumable"):
            return
        info_hash = item.get("info_hash")
        if not info_hash:
            return
        from anime_watch.torrent.engine import get_engine
        engine = get_engine()
        engine.stop(info_hash)
        self.app.torrents.pop(info_hash, None)
        self.app.torrent_downloads.pop(info_hash, None)
        dest = item.get("dest") or os.path.join("downloads", "torrents", info_hash)
        _remove_meta(dest)
        self.refresh_downloads()
        self._update_actions()

    def action_delete_file(self):
        if self._current_path:
            import shutil
            item = self._get_current_dl_item()
            if not item:
                return
            path = item.get("path")
            typ = item.get("type")
            if typ == "folder" and path and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                self.refresh_downloads()
            elif typ == "file" and path and os.path.isfile(path):
                os.remove(path)
                self.refresh_downloads()
            self._update_actions()
            self._update_footer()
            return
        item = self._get_current_dl_item()
        if not item:
            return
        info_hash = item.get("info_hash")
        typ = item.get("type")
        if typ in ("torrent",):
            from anime_watch.torrent.engine import get_engine
            engine = get_engine()
            engine.stop(info_hash)
            self.app.torrents.pop(info_hash, None)
            self.app.torrent_downloads.pop(info_hash, None)
        dest = item.get("dest")
        if dest and os.path.isdir(dest):
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
        dest_file = item.get("path")
        if dest_file and os.path.isfile(dest_file):
            os.remove(dest_file)
        _remove_meta(dest or "")
        self.refresh_downloads()
        self._update_actions()
        self._update_footer()

    def action_watch_file(self):
        if self._current_path:
            return
        self._watch_torrent_file()

    async def _play_local(self, path, title):
        import asyncio
        import os
        import subprocess
        sb = self.query_one("#status-bar", Static)
        sb.update(f"  Playing: {title[:40]}")
        _ANDROID = os.environ.get("ANDROID_ROOT") is not None or os.path.isdir("/data/data/io.freedom")
        if _ANDROID:
            import shutil
            _am_cmd = ["termux-am", "start"]
            if shutil.which("termux-am") is None:
                _am_cmd = ["am", "start"]
            subprocess.check_call(_am_cmd + [
                "-n", "io.freedom/is.xyz.mpv.VideoPlayerActivity",
                "--es", "url", path,
                "--es", "title", title,
            ])
            sb.update(f"  {self._current_path}" if self._current_path else "  Downloads Library")
            return
        args = ["mpv", "--no-terminal", "--ontop", path]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
            sb.update(f"  {self._current_path}" if self._current_path else "  Downloads Library")
        except FileNotFoundError:
            sb.update("  Error: mpv not found.")


class OperationOverlay(Screen):
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("escape", "close", "Close"),
        Binding("n", "next_episode", "Next"),
    ]

    _SPINNER = ["◜", "◝", "◞", "◟"]

    def __init__(self, title: str, kill_callback=None, next_callback=None):
        super().__init__()
        self._op_title = title
        self._kill = kill_callback
        self._next_cb = next_callback
        self._spinner_task = None
        self._spinner_base = title

    def compose(self) -> ComposeResult:
        with Container(classes="op-root"):
            with Vertical(classes="op-card"):
                yield Static(self._op_title, id="op-title", classes="op-title")
                yield ScrollableContainer(id="op-log", classes="op-log")
                with Horizontal(classes="op-buttons"):
                    yield Button("Next Episode", id="op-next-btn", classes="op-btn")
                    yield Button("Stop", id="op-stop-btn", classes="op-btn")

    def on_mount(self):
        self._start_spinner(self._spinner_base)

    def _stop_spinner(self):
        if self._spinner_task:
            self._spinner_task.cancel()
            self._spinner_task = None

    def _start_spinner(self, base: str):
        self._stop_spinner()
        self._spinner_base = base
        idx = 0
        async def _spin():
            nonlocal idx
            while True:
                try:
                    self.query_one("#op-title", Static).update(f"{self._spinner_base} {self._SPINNER[idx % len(self._SPINNER)]}")
                except Exception:
                    break
                idx += 1
                await asyncio.sleep(0.3)
        self._spinner_task = asyncio.create_task(_spin())

    def set_base(self, text: str):
        self._spinner_base = text

    def stage(self, title: str, log_message: str | None = None):
        self._start_spinner(title)
        if log_message:
            self.add_log(f"\u25b6 {log_message}")

    def add_log(self, text: str):
        log = self.query_one("#op-log", ScrollableContainer)
        line = Static(text, classes="op-log-line", markup=False)
        log.mount(line)
        self.app.call_after_refresh(lambda: log.scroll_end(animate=False))

    def show_playing(self, episode_title: str, has_next: bool = True):
        self.set_base(f"\u25b6 {episode_title}")
        self.query_one("#op-next-btn").display = has_next
        self.query_one("#op-stop-btn").display = True

    def show_ended(self):
        self.set_base("Playback Ended")
        self.query_one("#op-next-btn").display = bool(self._next_cb)
        self.query_one("#op-stop-btn").display = True

    def fail(self):
        self.set_base("Extraction Failed")
        self.add_log("\u2716 Stream could not be extracted")
        self.query_one("#op-next-btn").display = False
        self.query_one("#op-stop-btn").display = True

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "op-next-btn":
            self.action_next_episode()
        elif event.button.id == "op-stop-btn":
            self.action_close()

    def action_next_episode(self):
        if not self._next_cb:
            return
        self._stop_spinner()
        if self._kill:
            self._kill()
        self.query_one("#op-next-btn").display = False
        self.query_one("#op-stop-btn").display = False
        log = self.query_one("#op-log", ScrollableContainer)
        log.remove_children()
        self._start_spinner("Extracting Next Episode")
        self._next_cb()

    def action_quit(self):
        self._stop_spinner()
        if self._kill:
            self._kill()
        self.dismiss(None)
        self.app.exit()

    def action_close(self):
        self._stop_spinner()
        if self._kill:
            self._kill()
        self.dismiss(None)


class HistoryScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("up", "move_up", "", priority=True),
        Binding("down", "move_down", "", priority=True),
        Binding("k", "move_up", ""),
        Binding("j", "move_down", ""),
        Binding("enter", "activate", ""),
    ]

    def compose(self) -> ComposeResult:
        with Container(classes="browser-root"):
            with Horizontal(classes="top-bar"):
                yield LogoWidget()
                yield Static("  Watch History", id="status-bar", classes="status")
            yield RuleWidget(classes="rule")
            with Horizontal(classes="body"):
                with Vertical(classes="content-area"):
                    with Container(classes="panel-wrap"):
                        yield HistoryPanel(id="history-list")
            yield FooterHints(id="footer")

    def on_mount(self):
        from anime_watch.history import get_history
        entries = get_history(limit=100)
        panel = self.query_one("#history-list", HistoryPanel)
        panel.set_items(entries)
        panel.focus()
        self._update_footer(entries)

    def _update_footer(self, entries=None):
        footer = self.query_one("#footer", FooterHints)
        if entries and len(entries) > 0:
            footer.set_hints([
                ("↑↓", "Navigate"), ("↵", "Continue"), ("esc", "Back"),
                ("q", "Quit"),
            ])
        else:
            footer.set_hints([
                ("esc", "Back"), ("q", "Quit"),
            ])

    def on_base_list_panel_activated(self, event):
        self.action_activate()

    def action_back(self):
        self.app.pop_screen()

    def action_quit(self):
        self.app.exit()

    def action_move_up(self):
        self.query_one("#history-list", HistoryPanel).move_up()

    def action_move_down(self):
        self.query_one("#history-list", HistoryPanel).move_down()

    def action_activate(self):
        panel = self.query_one("#history-list", HistoryPanel)
        idx = panel.cursor
        if idx >= len(panel._items):
            return
        entry = panel._items[idx]
        from anime_watch.models import Episode
        data = entry.data.copy()
        if entry.progress > 0:
            data["_resume_at"] = entry.progress
        ep = Episode(
            title=entry.episode_title,
            url=entry.url,
            number=entry.episode_number,
            site_name=entry.site_name,
            anime_name=entry.anime_name,
            data=data,
        )
        self.app.search_query = entry.anime_name
        self.app._direct_play_episode = ep
        self.app.switch_screen(BrowserScreen())


class BrowserScreen(Screen):
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("escape", "back", "Back"),
        Binding("up", "move_up", "", priority=True),
        Binding("down", "move_down", "", priority=True),
        Binding("k", "move_up", ""),
        Binding("j", "move_down", ""),
        Binding("/", "search", "Search"),
        Binding("s", "toggle_sidebar", "Sidebar"),
        Binding("L", "view_downloads", "Downloads"),
        Binding("h", "view_history", "History"),
        Binding("left", "prev_category", "", priority=True),
        Binding("right", "next_category", "", priority=True),
        Binding("a", "toggle_audio", "Audio"),
        Binding("v", "toggle_quality", "Quality"),
        Binding("d", "download", "Download"),
    ]

    def __init__(self):
        super().__init__()
        self.results = []
        self.episodes = []
        self.downloads_list = []
        self.cursor = 0
        self.mode = "results"
        self.status = ""
        self._group_results = []
        self._episodes_backup = []
        self.servers = []
        self._server_episode = None
        self._provider_results = {}
        self._active_provider = "all"
        self._selected_torrent = None
        self._playback_episodes = []
        self._playback_idx = 0
        self._playback_episode = None
        self._playback_gen = 0
        self._playback_task = None

    def compose(self) -> ComposeResult:
        with Container(classes="browser-root"):
            with Horizontal(classes="top-bar"):
                yield LogoWidget()
                yield Static("", id="status-bar", classes="status")
            yield RuleWidget(classes="rule")
            with Horizontal(classes="body"):
                yield SidebarWidget(id="sidebar")
                with Vertical(classes="content-area"):
                    with Horizontal(classes="search-row"):
                        yield Input(placeholder="❯ Search anime…", id="browser-search")
                        yield Input(placeholder="Ep #", id="episode-jump")
                    with Horizontal(classes="provider-filter"):
                        yield Static("", classes="filter-btn")
                    with Container(classes="panel-wrap"):
                        yield ResultsPanel(id="results-list")
            yield FooterHints(id="footer")

    def on_mount(self):
        self._player = PlaybackHandler(self.app, self._update_content, self._update_footer)
        self._downloader = DownloadHandler(self.app, self._update_content)
        cat = getattr(self.app, "search_category", "anime")
        self.query_one("#browser-search", Input).placeholder = f"❯ Search {cat}…"
        self.action_search()
        self._update_footer()
        direct = getattr(self.app, '_direct_play_episode', None)
        if direct:
            self.app._direct_play_episode = None
            self._start_playback(direct)
            self._run_continue_watching_episodes(direct)
        else:
            q = getattr(self.app, 'search_query', '')
            if q:
                self._do_search(q)
        rl = self.query_one("#results-list", ResultsPanel)
        rl.focus()
        self.set_interval(2.0, self._refresh_sidebar)

    def _refresh_sidebar(self):
        sb = self.query_one("#sidebar", SidebarWidget)
        sb.refresh()

    def on_input_submitted(self, event: Input.Submitted):
        q = event.value.strip()
        if not q:
            self.focus_results()
            return
        if event.input.id == "episode-jump":
            self._jump_to_episode(q)
            self.focus_results()
            return
        self.app.search_query = q
        self._do_search(q)
        self.focus_results()

    def _do_search(self, query: str):
        self.mode = "results"
        self.results = []
        self._group_results = []
        self.cursor = 0
        self._update_content("Searching…")
        self._update_footer()
        self._run_search(query)

    @work(thread=True, exclusive=True)
    def _run_search(self, query: str):
        def on_progress(site, status):
            self.app.call_from_thread(
                self._update_content, f"Searching {site}: {status}"
            )
        cat = getattr(self.app, "search_category", "")
        all_results = search_configured(query, on_progress=on_progress, category=cat)
        provider_results = {}
        for r in all_results:
            key = r.source.lower() if isinstance(r, TorrentResult) else r.site_name.lower().strip()
            provider_results.setdefault(key, []).append(r)
        self.app.call_from_thread(self._show_results_with_filters, provider_results)

    def _show_results(self, results):
        self.results = results
        self.cursor = 0
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(results)
        self._update_content(f"{len(results)} result{'s' if len(results) != 1 else ''}" if results else "No results")
        self._update_search_input()
        self._update_footer()

    def _show_results_with_filters(self, provider_results: dict[str, list]):
        self._provider_results = provider_results
        self._active_provider = "all"
        all_res = []
        for lst in provider_results.values():
            all_res.extend(lst)
        if all_res and isinstance(all_res[0], TorrentResult):
            all_res.sort(key=lambda r: r.seeders, reverse=True)
        self._show_results(all_res)
        name_map = {k: p.name for k, p in CONFIGURED_PROVIDERS.items()}
        name_map.update({k: p.name for k, p in TORRENT_PROVIDERS.items()})
        filter_row = self.query_one(".provider-filter")
        existing = {b.id: b for b in filter_row.children}
        slugs = sorted(provider_results.keys())
        seen: set[str] = set()
        if "filter-all" not in existing:
            filter_row.mount(Button("All", id="filter-all", classes="filter-btn"), before=0)
            existing["filter-all"] = filter_row.query_one("#filter-all")
        seen.add("filter-all")
        for slug in slugs:
            bid = f"filter-{slug}"
            seen.add(bid)
            label = name_map.get(slug, slug.upper())
            if bid in existing:
                existing[bid].label = label
            else:
                filter_row.mount(Button(label, id=bid, classes="filter-btn"))
        for bid, btn in list(existing.items()):
            if bid not in seen:
                btn.remove()
        filter_row.add_class("visible")

    def _switch_provider_source(self, slug: str):
        self._active_provider = slug
        for btn in self.query(".filter-btn"):
            btn.remove_class("active")
        self.query_one(f"#filter-{slug}", Button).add_class("active")
        if slug == "all":
            all_res = []
            for lst in self._provider_results.values():
                all_res.extend(lst)
            if all_res and isinstance(all_res[0], TorrentResult):
                all_res.sort(key=lambda r: r.seeders, reverse=True)
            self._show_results(all_res)
        else:
            results = self._provider_results.get(slug, [])
            self._show_results(results)

    def _show_episodes(self, eps, title):
        self.episodes = eps
        self.cursor = 0
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(eps)
        self.mode = "episodes"
        self._update_content(f"{title} — {len(eps)} ep{'s' if len(eps) != 1 else ''}")
        self._update_search_input()
        self._update_footer()

    def _update_search_input(self):
        ep = self.query_one("#episode-jump", Input)
        if self.mode == "episodes":
            ep.add_class("visible")
        else:
            ep.remove_class("visible")
            ep.value = ""

    def _update_content(self, status_text=""):
        self.status = status_text
        try:
            sb = self.query_one("#status-bar", Static)
        except Exception:
            return
        if status_text:
            sb.update(Text(f"  {status_text}", style=SD))
        else:
            sb.update(Text("", style=SD))

    def _get_current_item(self):
        rl = self.query_one("#results-list", ResultsPanel)
        if self.mode in ("results", "providers", "servers", "torrent_options"):
            idx = rl.cursor
            if idx < len(self.results):
                return ("result", self.results[idx])
        elif self.mode == "episodes":
            item = rl.get_item_at_cursor()
            if item is not None:
                return ("episode", item)
        return (None, None)

    def _show_providers(self, group: SearchResultGroup):
        self._group_results = self.results
        self.results = group.results
        from anime_watch.providers import CONFIGURED_PROVIDERS
        for r in self.results:
            key = r.site_name.lower().strip()
            prov = CONFIGURED_PROVIDERS.get(key)
            if prov:
                site = prov.url
                r.data["alive"] = _check_alive(site, timeout=3) if site else None
            else:
                r.data["alive"] = None
        self.mode = "providers"
        self.cursor = 0
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(self.results)
        self._update_content(f"{group.title} — select provider")
        self._update_search_input()
        self._update_footer()

    def _start_server_pick(self, episode, for_download=False):
        episode.data["_server_pick_dl"] = for_download
        self._update_content(f"Loading servers for {episode.title[:40]}…")
        key = episode.site_name.lower().strip()
        from anime_watch.providers import CONFIGURED_PROVIDERS
        provider = CONFIGURED_PROVIDERS.get(key)
        if provider and getattr(provider, 'slug', '') == 'netmirror' and hasattr(provider, '_cookies_valid') and not provider._cookies_valid():
            self.app.push_screen(
                NetmirrorLoginPrompt(),
                callback=lambda r: self._run_server_fetch(episode) if r else None,
            )
            return
        self._run_server_fetch(episode)

    @work(thread=True, exclusive=True)
    def _run_server_fetch(self, episode):
        from anime_watch.providers import CONFIGURED_PROVIDERS
        key = episode.site_name.lower().strip()
        provider = CONFIGURED_PROVIDERS.get(key)
        servers = provider.get_servers(episode) if provider and hasattr(provider, 'get_servers') else []
        for sv in servers:
            link_id = sv.get("link_id", "")
            if link_id:
                sv["alive"] = _check_server_alive(provider, episode, link_id)
            else:
                sv["alive"] = None
        self.app.call_from_thread(self._show_servers, servers, episode)

    def _show_servers(self, servers, episode):
        if not servers:
            self._update_content("No servers available for this episode")
            return
        self._server_episode = episode
        self.servers = servers
        self._episodes_backup = self.episodes
        self._group_results = self.results
        from anime_watch.models import SearchResult
        self.results = [
            SearchResult(title=s["display"], url=s.get("link_id", ""),
                        site_name="", image="", data={"alive": s.get("alive")})
            for s in servers
        ]
        self.mode = "servers"
        self.cursor = 0
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(self.results)
        self._update_content(f"{episode.title[:40]} — select server")
        self._update_search_input()
        self._update_footer()

    def _pick_server(self, item):
        ep = self._server_episode
        if ep is None:
            return
        ep.data["server_name"] = item.title
        if "(" in item.title:
            label = item.title.split("(")[-1].rstrip(")").strip().lower()
            if label in ("sub", "dub", "hsub"):
                self.app.audio_pref = "sub" if label in ("sub", "hsub") else label
            elif len(label) in (2, 4) and label.isalpha():
                self.app.audio_pref = label
        is_dl = ep.data.pop("_server_pick_dl", False) or item.title.startswith("[DL]")
        self._restore_episodes()
        if is_dl:
            self._start_download(ep)
        else:
            self._start_playback(ep)

    def _restore_episodes(self):
        self.mode = "episodes"
        self.episodes = self._episodes_backup
        self.results = self._group_results
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(self.episodes)
        if self.episodes:
            self._update_content(f"{len(self.episodes)} ep{'s' if len(self.episodes) != 1 else ''}")
        self._update_search_input()
        self._update_footer()

    def _jump_to_episode(self, raw: str):
        self.query_one("#episode-jump", Input).value = ""
        try:
            target = int(raw)
        except ValueError:
            self._update_content(f"Not a number: {raw}")
            return
        rl = self.query_one("#results-list", ResultsPanel)
        for i, ep in enumerate(rl._items):
            try:
                if int(ep.number) == target:
                    rl.set_cursor(i)
                    self._update_content(f"Ep {target} — {ep.title[:50]}")
                    return
            except (ValueError, TypeError):
                continue
        self._update_content(f"Episode {target} not found")

    def action_move_up(self):
        if self.mode == "episodes":
            rl = self.query_one("#results-list", ResultsPanel)
            if not rl._items: return
        if self.mode in ("results", "providers", "torrent_options") and not self.results: return
        rl = self.query_one("#results-list", ResultsPanel)
        rl.move_up()

    def action_move_down(self):
        if self.mode == "episodes":
            rl = self.query_one("#results-list", ResultsPanel)
            if not rl._items: return
        if self.mode in ("results", "providers", "torrent_options") and not self.results: return
        rl = self.query_one("#results-list", ResultsPanel)
        rl.move_down()

    def action_next_category(self):
        if self.mode != "episodes":
            return
        rl = self.query_one("#results-list", ResultsPanel)
        rl.next_category()
        self._update_content_after_category(rl)

    def action_prev_category(self):
        if self.mode != "episodes":
            return
        rl = self.query_one("#results-list", ResultsPanel)
        rl.prev_category()
        self._update_content_after_category(rl)

    def _update_content_after_category(self, rl):
        if rl.category_count > 1:
            n = len(rl._items)
            cat = rl.active_category or "All"
            self._update_content(f"{cat} — {n} ep{'s' if n != 1 else ''}")

    def action_activate(self):
        typ, item = self._get_current_item()
        if typ == "result":
            if isinstance(item, TorrentResult):
                self._show_torrent_options(item)
            elif self.mode == "torrent_options":
                self._pick_torrent_mode(item)
            elif isinstance(item, SearchResultGroup):
                self._show_providers(item)
            elif self.mode == "servers":
                self._pick_server(item)
            else:
                self._start_episode_fetch(item)
        elif typ == "episode":
            from anime_watch.providers import CONFIGURED_PROVIDERS
            key = item.site_name.lower().strip()
            provider = CONFIGURED_PROVIDERS.get(key)
            if provider and hasattr(provider, 'get_servers'):
                self._start_server_pick(item)
            else:
                rl = self.query_one("#results-list", ResultsPanel)
                idx = rl.cursor
                self._start_playback(item, self.episodes, idx)

    def _start_episode_fetch(self, result):
        self._update_content(f"Loading episodes for {result.title[:40]}…")
        self._run_episode_fetch(result)

    @work(thread=True, exclusive=True)
    def _run_episode_fetch(self, result):
        eps = get_episodes(result)
        self.app.call_from_thread(self._show_episodes, eps, result.title[:40])

    def action_view_downloads(self):
        self.app.push_screen(DownloadsScreen())

    def action_view_history(self):
        self.app.push_screen(HistoryScreen())

    def action_download(self):
        typ, item = self._get_current_item()
        if isinstance(item, TorrentResult):
            self._start_torrent_download(item)
        elif self.mode == "servers":
            if self._server_episode:
                self._server_episode.data["_server_pick_dl"] = True
            self._pick_server(item)
        elif typ == "episode":
            from anime_watch.providers import CONFIGURED_PROVIDERS
            key = item.site_name.lower().strip()
            provider = CONFIGURED_PROVIDERS.get(key)
            if provider and hasattr(provider, 'get_servers'):
                self._start_server_pick(item, for_download=True)
            else:
                self._start_download(item)
        else:
            self._update_content("Select an episode to download.")

    def action_toggle_sidebar(self):
        sidebar = self.query_one("#sidebar", SidebarWidget)
        sidebar.toggle_class("visible")

    def _start_torrent_stream(self, torrent: TorrentResult):
        self._update_content(f"Starting torrent stream: {torrent.name[:40]}…")
        self._run_torrent_stream(torrent)

    @work(thread=True, exclusive=True)
    def _run_torrent_stream(self, torrent: TorrentResult):
        from anime_watch.torrent.engine import get_engine
        engine = get_engine()
        if not engine.is_available():
            self.app.call_from_thread(self._update_content, "webtorrent-cli not found")
            return

        def on_progress(msg):
            self.app.call_from_thread(self._update_content, f"Buffering: {msg}")

        asyncio.run(engine.stream_pipe(
            torrent.magnet, torrent.info_hash, on_progress
        ))
        self.app.call_from_thread(self._update_content, "Stream ended")

    def _show_torrent_options(self, torrent: TorrentResult):
        self._selected_torrent = torrent
        self._group_results = self.results
        from anime_watch.models import SearchResult
        self.results = [
            SearchResult(title="Watch & Download", url="", site_name="stream+save", image=""),
            SearchResult(title="Watch Only", url="", site_name="stream only", image=""),
        ]
        self.mode = "torrent_options"
        self.cursor = 0
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(self.results)
        self._update_content(f"{torrent.name[:40]} — select mode")
        self._update_footer()

    def _pick_torrent_mode(self, item):
        torrent = self._selected_torrent
        if not torrent:
            return
        if item.site_name == "stream+save":
            self._start_watch_and_download(torrent)
        else:
            self._start_torrent_stream(torrent)

    def _start_watch_and_download(self, torrent: TorrentResult):
        self._update_content(f"Watch & Download: {torrent.name[:40]}…")
        self._run_watch_and_download(torrent)

    @work(thread=True, exclusive=True)
    def _run_watch_and_download(self, torrent: TorrentResult):
        from anime_watch.torrent.engine import get_engine
        engine = get_engine()
        if not engine.is_available():
            self.app.call_from_thread(self._update_content, "webtorrent-cli not found")
            return

        safe = re.sub(r'[^a-zA-Z0-9 _-]', '', torrent.name)[:60]
        dest = os.path.join("downloads", "torrents", safe)
        save_path = os.path.join(dest, f"{safe}.mp4")

        self.app.torrents[torrent.info_hash] = {"name": torrent.name, "status": "Downloading", "paused": False}
        _ensure_meta(torrent, dest)

        def on_progress(msg):
            self.app.call_from_thread(self._update_content, f"Downloading: {msg}")
            if torrent.info_hash in self.app.torrents:
                self.app.torrents[torrent.info_hash]["status"] = msg

        self.app.call_from_thread(self._update_content, f"Playing & saving: {torrent.name[:40]}")
        asyncio.run(engine.stream_and_save(
            torrent.magnet, torrent.info_hash, save_path, on_progress
        ))
        self.app.call_from_thread(self._update_content, "Download complete")
        self.app.torrents.pop(torrent.info_hash, None)
        self.app.call_from_thread(_remove_meta, dest)
        # Clean up the .meta.json after full download since webtorrent is done
        # torrent sits on disk at save_path for the user

    def _start_torrent_download(self, torrent: TorrentResult):
        self._update_content(f"Downloading torrent: {torrent.name[:40]}…")
        self._run_torrent_download(torrent)

    @work(thread=True, exclusive=True)
    def _run_torrent_download(self, torrent: TorrentResult):
        from anime_watch.torrent.engine import get_engine
        engine = get_engine()
        if not engine.is_available():
            self.app.call_from_thread(self._update_content, "webtorrent-cli not found")
            return

        safe = re.sub(r'[^a-zA-Z0-9 _-]', '', torrent.name)[:60]
        dest = os.path.join("downloads", "torrents", safe)

        self.app.torrents[torrent.info_hash] = {"name": torrent.name, "status": "Starting", "paused": False}
        _ensure_meta(torrent, dest)

        def on_progress(msg):
            self.app.call_from_thread(self._update_content, f"Downloading {torrent.name[:30]} — {msg}")
            self.app.torrent_downloads[torrent.info_hash] = msg
            if torrent.info_hash in self.app.torrents:
                self.app.torrents[torrent.info_hash]["status"] = msg

        def on_complete(path):
            self.app.call_from_thread(self._update_content, f"Downloaded: {torrent.name[:40]}")
            self.app.torrent_downloads.pop(torrent.info_hash, None)
            self.app.torrents.pop(torrent.info_hash, None)
            self.app.call_from_thread(_remove_meta, dest)

        engine.download_sync(torrent.magnet, torrent.info_hash, dest, on_complete, on_progress)

    def _resume_torrent(self, item):
        magnet = item.get("magnet", "")
        info_hash = item.get("info_hash", "")
        name = item.get("title", "Unknown")
        dest = item.get("dest", "")
        if not magnet or not info_hash:
            return
        from anime_watch.models import TorrentResult
        t = TorrentResult(name=name, magnet=magnet, info_hash=info_hash, source="", seeders=0, leechers=0, size_bytes=0)
        self._start_watch_and_download(t)

    def _start_download(self, episode):
        self._update_content(f"Extracting stream for download: {episode.title[:30]}…")
        self._run_download(episode)

    @work(thread=True, exclusive=False)
    def _run_download(self, episode):
        from anime_watch.providers import extract_stream
        stream = extract_stream(episode, self.app.audio_pref, self.app.quality_pref)
        if stream and stream.url:
            self.app.call_from_thread(self._downloader._do_download, stream, episode)
        else:
            self.app.call_from_thread(self._update_content, "Could not extract stream for download")

    def _start_playback(self, episode, episodes=None, current_idx=0):
        self._playback_gen += 1
        if self._playback_task:
            self._playback_task.cancel()
            self._playback_task = None
        self._player.kill_current()
        self._playback_episodes = episodes or []
        self._playback_idx = current_idx
        has_next = current_idx + 1 < len(episodes or [])
        def _on_next():
            self._playback_idx += 1
            next_ep = self._playback_episodes[self._playback_idx]
            self._playback_episode = next_ep
            self._run_playback(next_ep, overlay)
        def _kill_and_mark():
            self._playback_gen += 1
            self._player.kill_current()
        overlay = OperationOverlay(
            "Extracting Stream",
            kill_callback=_kill_and_mark,
            next_callback=_on_next if has_next else None,
        )
        self._playback_episode = episode
        self.app.push_screen(overlay)
        self._run_playback(episode, overlay)

    @work(thread=True, exclusive=True)
    def _run_playback(self, episode, overlay):
        from anime_watch.providers import extract_stream

        import requests as _req
        _request_log_ctx.overlay = overlay
        if not hasattr(_req.Session, '_aw_patched'):
            _req.Session._aw_patched = True
            _orig = _req.Session.request
            def _logged(self, method, url, *args, **kwargs):
                ctx = getattr(_request_log_ctx, 'overlay', None)
                if ctx:
                    try:
                        ctx.add_log(f"  {method} {url}")
                    except Exception:
                        pass
                return _orig(self, method, url, *args, **kwargs)
            _req.Session.request = _logged

        self.app.call_from_thread(overlay.stage, "Contacting Provider", "Sending request...")

        try:
            stream = extract_stream(episode, self.app.audio_pref, self.app.quality_pref)
        except Exception as e:
            _request_log_ctx.overlay = None
            self.app.call_from_thread(overlay.stage, "Error", f"{type(e).__name__}: {str(e)[:80]}")
            self.app.call_from_thread(overlay.fail)
            return
        finally:
            _request_log_ctx.overlay = None

        has_next = self._playback_idx + 1 < len(self._playback_episodes)
        if stream and stream.url:
            self.app.call_from_thread(overlay.add_log, f"  Resolved [{stream.quality}] {stream.url[:70]}…")
            if stream.subtitles:
                self.app.call_from_thread(overlay.add_log, f"  Subtitles: {len(stream.subtitles)} track(s)")
            self.app.call_from_thread(overlay.show_playing, episode.title, has_next)
            self.app.call_from_thread(self._launch_mpv, stream, episode, overlay)
        else:
            self.app.call_from_thread(overlay.stage, "Failed", "No stream returned")
            self.app.call_from_thread(overlay.fail)

    def _launch_mpv(self, stream, episode, overlay):
        self._playback_gen += 1
        gen = self._playback_gen
        async def _run():
            await self._player._do_play(stream, episode, overlay)
            if gen != self._playback_gen:
                return
            try:
                self.app.call_from_thread(overlay.show_ended)
            except Exception:
                pass
        self._playback_task = asyncio.create_task(_run())

    @work(thread=True)
    def _run_continue_watching_episodes(self, episode):
        from anime_watch.models import SearchResult
        from anime_watch.providers import get_episodes
        result = SearchResult(
            title=episode.anime_name,
            url=episode.url,
            site_name=episode.site_name,
        )
        eps = get_episodes(result)
        if eps:
            self.app.call_from_thread(self._show_episodes, eps, episode.anime_name)

    def action_search(self):
        inp = self.query_one("#browser-search", Input)
        inp.focus()

    def action_back(self):
        if self.mode == "servers":
            self._restore_episodes()
        elif self.mode == "providers":
            self.results = self._group_results
            self.mode = "results"
            rl = self.query_one("#results-list", ResultsPanel)
            rl.set_items(self.results)
            self._update_content(f"{len(self.results)} result{'s' if len(self.results) != 1 else ''}" if self.results else "")
            self._update_search_input()
            self._update_footer()
        elif self.mode == "torrent_options":
            self.results = self._group_results
            self.mode = "results"
            rl = self.query_one("#results-list", ResultsPanel)
            rl.set_items(self.results)
            self._update_content(f"{len(self.results)} result{'s' if len(self.results) != 1 else ''}" if self.results else "")
            self._update_footer()
        elif self.mode == "episodes":
            if not self._group_results and not self.results:
                self.app.switch_screen(SplashScreen())
                return
            self.mode = "results"
            self.episodes = []
            if self._group_results:
                self.results = self._group_results
            rl = self.query_one("#results-list", ResultsPanel)
            rl.set_items(self.results)
            if self.results:
                self._update_content(f"{len(self.results)} result{'s' if len(self.results) != 1 else ''}")
            else:
                self._update_content("")
            self._update_search_input()
            self._update_footer()
        else:
            self.app.switch_screen(SplashScreen())

    def action_quit(self):
        self.app.exit()

    def on_button_pressed(self, event: Button.Pressed):
        eid = event.button.id
        if eid and eid.startswith("filter-"):
            slug = eid.replace("filter-", "")
            self._switch_provider_source(slug)

    def on_sidebar_widget_open_downloads(self, event):
        self.action_view_downloads()

    def on_base_list_panel_activated(self, event):
        self.action_activate()

    def focus_results(self):
        self.query_one("#results-list", ResultsPanel).focus()

    def _get_provider_for_current(self):
        if not CONFIGURED_SITES:
            return None
        key = CONFIGURED_SITES[0].name.lower().strip()
        return CONFIGURED_PROVIDERS.get(key)

    def action_toggle_audio(self):
        prov = self._get_provider_for_current()
        opts = prov.get_supported_audio() if prov else ["sub", "dub"]
        try:
            idx = opts.index(self.app.audio_pref)
        except ValueError:
            idx = 0
        self.app.audio_pref = opts[(idx + 1) % len(opts)]
        self._update_content(f"Audio preference set to: {self.app.audio_pref.upper()}")
        self._update_footer()

    def action_toggle_quality(self):
        prov = self._get_provider_for_current()
        qs = prov.get_supported_qualities() if prov else ["1080p", "720p", "360p", "best"]
        try:
            idx = qs.index(self.app.quality_pref)
        except ValueError:
            idx = 0
        self.app.quality_pref = qs[(idx + 1) % len(qs)]
        self._update_content(f"Quality preference set to: {self.app.quality_pref}")
        self._update_footer()

    def _update_footer(self):
        footer = self.query_one("#footer", FooterHints)
        ap = self.app.audio_pref.upper()
        qp = self.app.quality_pref
        cat = getattr(self.app, "search_category", "")

        if self.mode == "torrent_options":
            footer.set_hints([
                ("↑↓", "Navigate"), ("↵", "Select Mode"), ("esc", "Back"), ("q", "Quit"),
            ])
        elif cat == "torrent":
            if self.mode == "results":
                footer.set_hints([
                    ("↑↓", "Navigate"), ("/", "Search"), ("↵", "Stream"), ("d", "Download"),
                    ("s", "Sidebar"), ("esc", "Back"), ("q", "Quit"),
                ])
            else:
                footer.set_hints([
                    ("↑↓", "Navigate"), ("↵", "Play"), ("d", "Download"), ("s", "Sidebar"),
                    ("esc", "Back"), ("q", "Quit"),
                ])
        elif self.mode == "results":
            footer.set_hints([
                ("↑↓", "Navigate"), ("/", "Search"), ("↵", "Episodes"),
                ("s", "Sidebar"), ("L", "Library"), ("h", "History"), ("q", "Quit"),
            ])
        elif self.mode == "servers":
            footer.set_hints([
                ("↑↓", "Navigate"), ("↵", "Select"), ("d", "Download"),
                ("esc", "Back"), ("q", "Quit"),
            ])
        elif self.mode == "episodes":
            hints = [("↑↓", "Navigate"), ("↵", "Play"), ("/", "Ep #"), ("d", "Download")]
            try:
                rl = self.query_one("#results-list", ResultsPanel)
                if rl.category_count > 1:
                    hints.insert(1, ("←→", "Category"))
            except Exception:
                pass
            hints += [("s", "Sidebar"), ("esc", "Back"), ("h", "History"),
                      ("a", f"Audio({ap})"), ("v", f"Qual({qp})"), ("q", "Quit")]
            footer.set_hints(hints)
        else:
            footer.set_hints([
                ("↑↓", "Navigate"), ("↵", "Play"), ("d", "Download"), ("s", "Sidebar"),
                ("esc", "Back"), ("L", "Library"), ("h", "History"),
                ("a", f"Audio({ap})"), ("v", f"Qual({qp})"), ("q", "Quit"),
            ])

    def key_up(self, event=None):
        self.action_move_up()

    def key_down(self, event=None):
        self.action_move_down()


class NetmirrorLoginPrompt(Screen):
    BINDINGS = [
        Binding("escape", "close", "Close"),
    ]

    DEFAULT_CSS = """
    #nm-webview-btn, #nm-done-btn {
        border: none;
        background: transparent;
        color: $accent;
        min-width: 0;
        padding: 0 1;
        margin: 0 2;
    }
    #nm-webview-btn:hover, #nm-done-btn:hover,
    #nm-webview-btn:focus, #nm-done-btn:focus {
        color: $text;
        text-style: bold;
    }
    """
    def compose(self) -> ComposeResult:
        with Container(classes="op-root"):
            with Vertical(classes="op-card"):
                yield Static("NetMirror Login Required", id="nm-title", classes="op-title")
                yield Static(
                    "This provider needs login cookies from net77.cc.\n\n"
                    "  Select [bold]Open WebView[/] to launch the login screen.\n"
                    "  Sign in with your Google account.\n"
                    "  When done, come back and select [bold]Done[/].",
                    id="nm-text",
                )
                with Horizontal(classes="op-buttons"):
                    yield Button("Open WebView", id="nm-webview-btn")
                    yield Button("Done", id="nm-done-btn")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "nm-webview-btn":
            try:
                    _am = shutil.which("termux-am") or "/system/bin/am"
                    subprocess.Popen(
                        [_am, "start", "-n", "io.freedom/is.xyz.mpv.NetmirrorLoginActivity"],
                    )
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Launch WebView failed: {e}")
        elif event.button.id == "nm-done-btn":
            self.dismiss("ok")

    def action_close(self):
        self.dismiss(None)
