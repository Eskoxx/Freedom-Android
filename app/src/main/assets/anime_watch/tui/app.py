import os
import sys
from textual.app import App
from anime_watch.core import _which
from anime_watch.tui.screens import SplashScreen

_ANDROID = os.environ.get("ANDROID_ROOT") is not None or os.path.isdir("/data/data/io.freedom")

AW_CSS = '''
Screen { background: #0d0b14; }

.splash-root { align: center middle; width: 100%; height: 100%; }
.splash-center { align: center middle; width: 100%; height: auto; min-width: 50%; }
.splash-center > LogoWidget { text-align: center; }
.splash-center > .spacer { height: 2; }
.splash-center > .spacer-sm { height: 1; }
#splash-search { min-width: 60; width: 80w; max-width: 100; border: none; background: transparent; color: #a78bfa; }
.splash-hints-row { width: 100%; height: 1; align: center middle; }
.hint-text { width: auto; color: #6b6577; }
.hint-key { width: auto; color: #b9a7e6; }
.hint-btn { width: auto; height: 1; min-width: 1; padding: 0; border: none; background: transparent; color: #6b6577; }
.hint-btn:focus { color: #a78bfa; text-style: bold; }
.hint-btn:hover { color: #d8b4fe; }

.splash-toggle-row { width: 100%; height: 1; align: center middle; margin: 0 0 1 0; }
.cat-btn { width: auto; height: 1; min-width: 1; padding: 0 1; border: none; background: transparent; color: #6b6577; }
.cat-btn:hover { color: #d8b4fe; }
.cat-btn:focus { color: #a78bfa; text-style: bold; }
.cat-btn.active { color: #a78bfa; text-style: bold; }

.branch-row { height: 1; width: 100%; display: none; align: center middle; }
.branch-row.visible { display: block; }
.branch-btn { width: auto; height: 1; min-width: 1; padding: 0 1; border: none; background: transparent; color: #6b6577; }
.branch-btn:hover { color: #d8b4fe; }
.branch-btn:focus { color: #a78bfa; text-style: bold; }
.branch-btn.active { color: #a78bfa; text-style: bold; }

.browser-root { width: 100%; height: 100%; layout: vertical; }
.top-bar { height: 3; width: 100%; }
.top-bar > LogoWidget { width: auto; min-width: 40; }
.rule { height: 1; width: 100%; }
.body { height: 1fr; width: 100%; }
#sidebar { display: none; width: 22; height: 100%; padding: 1 0 0 1; }
#sidebar.visible { display: block; }
.content-area { height: 100%; width: 1fr; padding: 0 1 0 1; }
.search-row { height: 3; width: 100%; }
#browser-search { width: 1fr; border: none; background: transparent; color: #a78bfa; }
#episode-jump { width: 12; display: none; border: none; background: transparent; color: #a78bfa; }
#episode-jump.visible { display: block; }
.panel-wrap { height: 1fr; width: 100%; border: round #6b6577; padding: 0 0 0 1; }
#results-list { height: 100%; width: 100%; }
#downloads-list { height: 100%; width: 100%; }
#history-list { height: 100%; width: 100%; }

.provider-filter { height: 1; width: 100%; display: none; align: left middle; padding: 0 0 0 1; }
.provider-filter.visible { display: block; }
.filter-btn { width: auto; height: 1; min-width: 1; padding: 0 1; border: none; background: transparent; color: #6b6577; }
.filter-btn:hover { color: #d8b4fe; }
.filter-btn:focus { color: #a78bfa; text-style: bold; }
.filter-btn.active { color: #a78bfa; text-style: bold; }

.continue-box { width: 100%; height: auto; align: center middle; }
.cw-btn { width: 100%; height: 1; min-width: 1; padding: 0; border: none; background: transparent; color: #b9a7e6; }
.cw-btn:hover { color: #d8b4fe; }
.cw-btn:focus { color: #a78bfa; text-style: bold; }

.op-root { align: center middle; width: 100%; height: 100%; }
.op-card { width: 60; height: auto; padding: 1 2; border: round #6b6577; background: #0d0b14; }
.op-title { width: 100%; text-align: center; color: #a78bfa; text-style: bold; }
.op-log { width: 100%; min-height: 6; height: auto; max-height: 12; border: none; background: #0a0a0f; padding: 0 0 0 1; scrollbar-size: 0 0; }
.op-log-line { width: 100%; color: #c4b5fd; }
.op-buttons { width: 100%; height: auto; align: center middle; margin: 1 0 0 0; }
.op-btn { width: auto; height: 1; min-width: 1; padding: 0 1; margin: 0 1; border: none; background: transparent; color: #6b6577; display: none; }
.op-btn:hover { color: #d8b4fe; }
.op-btn:focus { color: #a78bfa; text-style: bold; }

#status-bar { height: 1; width: 1fr; }
#dl-actions { height: 3; width: 100%; align: center middle; display: none; }
#dl-actions.visible { display: block; }
.dl-btn { width: auto; height: 1; min-width: 1; padding: 0 1; border: none; background: transparent; color: #6b6577; }
.dl-btn:hover { color: #d8b4fe; }
.dl-btn:focus { color: #a78bfa; text-style: bold; }
#footer { height: 1; width: 100%; padding: 0 0 0 1; }

Input { border: none; background: transparent; }
*:focus { border: none; }
'''

class AnimeWatch(App):
    TITLE = "FREEDOM"
    CSS = AW_CSS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.search_query = ""
        self.audio_pref = "sub"
        self.quality_pref = "1080p"
        self.search_category = "anime"
        self.downloads = {}
        self.torrent_downloads: dict[str, str] = {}
        self.torrents: dict[str, dict] = {}

    def run(self, *args, **kwargs):
        try:
            return super().run(*args, **kwargs)
        finally:
            self._cleanup_webtorrent()

    def on_ready(self):
        self.push_screen(SplashScreen())

    def _cleanup_webtorrent(self):
        from anime_watch.torrent.engine import get_engine
        get_engine().stop_all()

def run_app():
    if not _ANDROID and not _which("mpv"):
        sys.stderr.write("mpv not found. Install it: apt install mpv / brew install mpv\n")
        sys.exit(1)
    app = AnimeWatch()
    app.run()
