from __future__ import annotations
import asyncio
import os
import re
import shutil
import signal
import subprocess
import tempfile
from typing import Optional

from anime_watch.core import _which

_ANDROID = os.environ.get("ANDROID_ROOT") is not None and os.path.isdir("/data/data/io.freedom")

TMPDIR_PREFIX = "aw-torrent-"

# webtorrent-cli wraps its status UI in chalk ANSI escapes even when stdout is a pipe.
# Strip them before parsing the "Server running at:" URL, or mpv gets a garbage URL.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_DEBUG_PATH = "/data/data/io.freedom/cache/aw-torrent-debug.txt"

def _dbg(msg: str) -> None:
    if not _ANDROID:
        return
    try:
        with open(_DEBUG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

class TorrentEngine:
    def __init__(self):
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tmpdirs: dict[str, str] = {}
        self._monitor_tasks: dict[str, asyncio.Task] = {}

    def is_available(self) -> bool:
        return _which("webtorrent")

    async def stream_and_save(
        self,
        magnet: str,
        info_hash: str,
        save_path: str,
        on_progress: Optional[callable] = None,
    ) -> None:
        """Tee webtorrent stdout to both mpv (pipe) and a file on disk."""
        if _ANDROID:
            import threading as _threading
            dest_dir = os.path.dirname(save_path) or "."
            done_evt = _threading.Event()
            url = self.download_to_dir_sync(
                magnet, info_hash, dest_dir, on_progress, track=False,
                on_done=done_evt.set,
            )
            if url:
                self._launch_android_player(url)
                # Wait for the real download completion (100% progress or
                # webtorrent exit) so the UI reports actual progress instead of
                # declaring "Download complete" the moment the player launches.
                done_evt.wait()
            return

        import os as _os
        r_fd, w_fd = _os.pipe()

        webtorrent = await asyncio.create_subprocess_exec(
            "webtorrent", "download", magnet,
            "--stdout", "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        self._processes[info_hash] = webtorrent

        save_dir = _os.path.dirname(save_path) or "."
        _os.makedirs(save_dir, exist_ok=True)
        save_file = open(save_path, "wb")

        async def tee_output():
            try:
                while True:
                    chunk = await webtorrent.stdout.read(65536)
                    if not chunk:
                        break
                    save_file.write(chunk)
                    save_file.flush()
                    _os.write(w_fd, chunk)
            finally:
                save_file.close()
                try:
                    _os.close(w_fd)
                except OSError:
                    pass

        tee_task = asyncio.create_task(tee_output())

        mpv = await asyncio.create_subprocess_exec(
            "mpv", "--no-terminal", "--osd-level=0", "--vo=gpu",
            "--cache=yes", "--cache-secs=30",
            "-",
            stdin=r_fd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _os.close(r_fd)

        stderr_task = asyncio.create_task(
            self._pipe_stderr(webtorrent.stderr, info_hash, on_progress)
        )

        await mpv.wait()
        self.stop(info_hash)
        tee_task.cancel()
        stderr_task.cancel()
        self._close_stderr(webtorrent)

    def _launch_android_player(self, url: str) -> None:
        import shutil as _shutil
        _am_cmd = ["termux-am", "start"]
        if _shutil.which("termux-am") is None:
            _am_cmd = ["am", "start"]
        _dbg(f"launching player with {_am_cmd[0]}: {url[:70]}")
        try:
            subprocess.check_call(_am_cmd + [
                "-n", "io.freedom/is.xyz.mpv.VideoPlayerActivity",
                "--es", "url", url,
            ])
            _dbg("player launch OK")
        except Exception as e:
            _dbg(f"player launch FAILED: {e}")

    async def stream_pipe(
        self,
        magnet: str,
        info_hash: str,
        on_progress: Optional[callable] = None,
    ) -> None:
        if _ANDROID:
            tmpdir = tempfile.mkdtemp(prefix="aw-torrent-")
            url = self.download_to_dir_sync(magnet, info_hash, tmpdir, on_progress, track=False)
            if url:
                self._launch_android_player(url)
            return

        import os
        r_fd, w_fd = os.pipe()

        webtorrent = await asyncio.create_subprocess_exec(
            "webtorrent", "download", magnet,
            "--stdout", "--quiet",
            stdout=w_fd,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        os.close(w_fd)
        self._processes[info_hash] = webtorrent

        mpv = await asyncio.create_subprocess_exec(
            "mpv", "--no-terminal", "--osd-level=0", "--vo=gpu",
            "--cache=yes", "--cache-secs=30",
            "-",
            stdin=r_fd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        os.close(r_fd)

        stderr_task = asyncio.create_task(
            self._pipe_stderr(webtorrent.stderr, info_hash, on_progress)
        )

        await mpv.wait()
        self.stop(info_hash)
        stderr_task.cancel()
        self._close_stderr(webtorrent)

    async def download_to_dir(
        self,
        magnet: str,
        info_hash: str,
        dest_dir: str,
        on_progress: Optional[callable] = None,
    ) -> Optional[str]:
        """Download to a permanent directory. Returns file path when 50MB+ buffered, or None on timeout."""
        os.makedirs(dest_dir, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            "webtorrent", "download", magnet,
            "--out", dest_dir, "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        self._processes[info_hash] = proc

        stderr_task = asyncio.create_task(
            self._pipe_stderr(proc.stderr, info_hash, on_progress)
        )

        waited = 0
        while waited < 120:
            files = self._find_video_files(dest_dir)
            if files:
                largest = max(files, key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0)
                if (os.path.exists(largest) and os.path.getsize(largest) > 50 * 1024 * 1024
                        and self._file_has_header(largest)):
                    stderr_task.cancel()
                    self._close_stderr(proc)
                    return largest
            await asyncio.sleep(2)
            waited += 2

        stderr_task.cancel()
        self._close_stderr(proc)
        return None

    def _wait_until_serving(self, url: str, timeout: float = 15.0) -> bool:
        """HEAD-poll the webtorrent HTTP server until it serves the file (or timeout)."""
        import urllib.request
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if 200 <= getattr(resp, "status", 200) < 400:
                        return True
            except Exception:
                pass
            _time.sleep(0.5)
        return False

    def _construct_webtorrent_url(self, info_hash: str, dest_dir: str, base_url: Optional[str] = None) -> Optional[str]:
        import urllib.parse
        videos = self._find_video_files(dest_dir)
        if not videos:
            return None
        largest = max(videos, key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0)
        rel = os.path.relpath(largest, dest_dir)
        encoded = urllib.parse.quote(rel, safe="/")
        if base_url:
            return f"{base_url.rstrip('/')}/webtorrent/{info_hash}/{encoded}"
        return f"http://localhost:8000/webtorrent/{info_hash}/{encoded}"

    def download_to_dir_sync(
        self,
        magnet: str,
        info_hash: str,
        dest_dir: str,
        on_progress: Optional[callable] = None,
        track: bool = True,
        on_done: Optional[callable] = None,
    ) -> Optional[str]:
        import subprocess as _subprocess
        import threading as _threading
        import time as _time

        os.makedirs(dest_dir, exist_ok=True)
        _dbg(f"start hash={info_hash[:10]} dest={dest_dir}")

        proc = _subprocess.Popen(
            # --keep-seeding: webtorrent-cli exits on download completion when
            # nobody has connected to its HTTP server yet, killing the stream
            # URL right before the player connects.
            ["webtorrent", "download", magnet, "--out", dest_dir, "--keep-seeding"],
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
            # Stay in the app's process group on Android so a force-stop/crash
            # kills webtorrent too (setsid would orphan it and the download
            # would keep running after the app is gone).
            preexec_fn=os.setsid if not _ANDROID else None,
        )
        if track:
            self._processes[info_hash] = proc

        base_url: Optional[str] = None
        stop_event = _threading.Event()

        def _read_stdout():
            nonlocal base_url
            try:
                for line in iter(proc.stdout.readline, b""):
                    if stop_event.is_set():
                        break
                    text = _ANSI_RE.sub("", line.decode(errors="replace"))
                    if base_url is None and "Server running at:" in text:
                        idx = text.index("Server running at:")
                        candidate = text[idx + len("Server running at:"):].strip()
                        if candidate.startswith("http"):
                            base_url = candidate
            except ValueError:
                pass
            # stdout EOF = webtorrent exited; never wait forever for completion.
            if on_done:
                on_done()

        stdout_reader = _threading.Thread(target=_read_stdout, daemon=True)
        stdout_reader.start()

        def _read_stderr():
            try:
                for line in iter(proc.stderr.readline, b""):
                    if stop_event.is_set():
                        break
                    text = line.decode(errors="replace").strip()
                    if on_progress and text:
                        msg = self._parse_progress_sync(text)
                        if msg:
                            on_progress(msg)
                            if on_done and msg.startswith("100%"):
                                on_done()
            except ValueError:
                pass

        stderr_reader = _threading.Thread(target=_read_stderr, daemon=True)
        stderr_reader.start()

        # Wait for BOTH the server URL and at least one video file on disk.
        # Metadata via DHT can be slow; give it up to 120s instead of failing
        # after 30s and leaving webtorrent running with no player.
        waited = 0
        limit = 120
        while waited < limit and (base_url is None or not self._find_video_files(dest_dir)) and not stop_event.is_set():
            _time.sleep(1)
            waited += 1

        if base_url is None:
            base_url = "http://localhost:8000/"
        _dbg(f"base_url={base_url} after {waited:.0f}s")

        if base_url and "/webtorrent/" in base_url:
            # webtorrent-cli prints the FULL stream URL ("Server running at:
            # http://localhost:PORT/webtorrent/<hash>/<file>"), not a base.
            file_url = base_url
        else:
            file_url = self._construct_webtorrent_url(info_hash, dest_dir, base_url=base_url)
        if not file_url:
            _dbg("file_url=None (no video file found)")
            stop_event.set()
            self._abort_proc(proc)
            return None

        if not self._wait_until_serving(file_url, timeout=15):
            _dbg(f"serving timeout for {file_url}")
            file_url2 = self._construct_webtorrent_url(info_hash, dest_dir)
            if file_url2 and file_url2 != file_url and self._wait_until_serving(file_url2, timeout=5):
                file_url = file_url2
            else:
                _dbg("no alternate url either; aborting")
                stop_event.set()
                self._abort_proc(proc)
                return None
        _dbg(f"returning {file_url}")
        return file_url

    @staticmethod
    def _abort_proc(proc) -> None:
        """Kill a webtorrent process and close its pipes after a give-up."""
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.stderr.close()
        except OSError:
            pass

    def _parse_progress_sync(self, text: str) -> Optional[str]:
        m = re.search(r'(\d+\.?\d*)\s*%', text)
        if m:
            pct = m.group(1)
            m2 = re.search(r'([\d.]+)\s*(KB|MB|GB)/s', text)
            if m2:
                return f"{pct}% {m2.group(1)} {m2.group(2)}/s"
            return f"{pct}%"
        m = re.search(r'([\d.]+)\s*(KB|MB|GB)/s', text)
        if m:
            return f"{m.group(1)} {m.group(2)}/s"
        return None

    async def stream(
        self,
        magnet: str,
        info_hash: str,
        on_ready: callable,
        on_progress: Optional[callable] = None,
        cleanup_after: bool = True,
    ) -> None:
        tmpdir = tempfile.mkdtemp(prefix=TMPDIR_PREFIX)
        self._tmpdirs[info_hash] = tmpdir

        proc = await asyncio.create_subprocess_exec(
            "webtorrent", "download", magnet,
            "--out", tmpdir,
            "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        self._processes[info_hash] = proc

        monitor = asyncio.create_task(
            self._monitor_download(info_hash, tmpdir, on_ready, on_progress, cleanup_after)
        )
        self._monitor_tasks[info_hash] = monitor

        stdout_task = asyncio.create_task(self._pipe_stderr(proc.stderr, info_hash, on_progress))
        await proc.wait()
        stdout_task.cancel()
        self._close_stderr(proc)

        if cleanup_after and info_hash in self._tmpdirs:
            self._cleanup(info_hash)

    async def download(
        self,
        magnet: str,
        info_hash: str,
        dest_dir: str,
        on_complete: Optional[callable] = None,
        on_progress: Optional[callable] = None,
    ) -> None:
        os.makedirs(dest_dir, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            "webtorrent", "download", magnet,
            "--out", dest_dir,
            "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        self._processes[info_hash] = proc

        stdout_task = asyncio.create_task(self._pipe_stderr(proc.stderr, info_hash, on_progress))
        await proc.wait()
        stdout_task.cancel()
        self._close_stderr(proc)

        if on_complete:
            on_complete(dest_dir)

    def download_sync(
        self,
        magnet: str,
        info_hash: str,
        dest_dir: str,
        on_complete: Optional[callable] = None,
        on_progress: Optional[callable] = None,
    ) -> None:
        import subprocess as _subprocess
        import threading as _threading

        os.makedirs(dest_dir, exist_ok=True)

        proc = _subprocess.Popen(
            ["webtorrent", "download", magnet, "--out", dest_dir, "--quiet"],
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        self._processes[info_hash] = proc

        stop_event = _threading.Event()

        def _read_stderr():
            try:
                for line in iter(proc.stderr.readline, b""):
                    if stop_event.is_set():
                        break
                    text = line.decode(errors="replace").strip()
                    if on_progress and text:
                        msg = self._parse_progress_sync(text)
                        if msg:
                            on_progress(msg)
            except ValueError:
                pass

        reader = _threading.Thread(target=_read_stderr, daemon=True)
        reader.start()

        try:
            proc.wait()
        finally:
            stop_event.set()
            try:
                proc.stderr.close()
            except OSError:
                pass
            reader.join(timeout=2)

        if on_complete:
            on_complete(dest_dir)

    async def _monitor_download(
        self,
        info_hash: str,
        tmpdir: str,
        on_ready: callable,
        on_progress: Optional[callable] = None,
        cleanup_after: bool = True,
    ) -> None:
        waited = 0
        while waited < 120:
            files = self._find_video_files(tmpdir)
            if files:
                largest = max(files, key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0)
                if os.path.exists(largest) and os.path.getsize(largest) > 50 * 1024 * 1024:
                    on_ready(largest)
                    return
            if on_progress:
                size = self._dir_size(tmpdir)
                on_progress(f"Buffering {_format_bytes(size)}…")
            await asyncio.sleep(2)
            waited += 2

    def _find_video_files(self, directory: str) -> list[str]:
        found = []
        for root, dirs, files in os.walk(directory):
            for f in files:
                if f.lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov", ".ts", ".m4v", ".flv")):
                    found.append(os.path.join(root, f))
        return found

    def _dir_size(self, directory: str) -> int:
        total = 0
        for root, dirs, files in os.walk(directory):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def _file_has_header(self, path: str, min_nonzero: int = 1024) -> bool:
        try:
            with open(path, "rb") as f:
                data = f.read(4096)
            nonzero = sum(1 for b in data if b != 0)
            return nonzero >= min_nonzero
        except OSError:
            return False

    def _close_stderr(self, proc) -> None:
        if proc.stderr and hasattr(proc.stderr, '_transport') and proc.stderr._transport:
            try:
                proc.stderr._transport.close()
            except Exception:
                pass

    async def _pipe_stderr(self, stderr, info_hash: str, on_progress: Optional[callable]):
        if not stderr:
            return
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if on_progress and text:
                    pct = speed = None
                    m = re.search(r'(\d+\.?\d*)\s*%', text)
                    if m:
                        pct = m.group(1)
                    m = re.search(r'([\d.]+)\s*(KB|MB|GB)/s', text)
                    if m:
                        speed = f"{m.group(1)} {m.group(2)}/s"
                    if pct and speed:
                        on_progress(f"{pct}% {speed}")
                    elif pct:
                        on_progress(f"{pct}%")
                    elif speed:
                        on_progress(speed)
                    else:
                        m = re.search(r'\b(\d{1,3})\b', text)
                        if m:
                            val = int(m.group(1))
                            if 0 <= val <= 100:
                                on_progress(f"{val}%")
        except Exception:
            pass

    def stop(self, info_hash: str) -> None:
        proc = self._processes.pop(info_hash, None)
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        self._cleanup(info_hash)

    def pause(self, info_hash: str) -> None:
        proc = self._processes.get(info_hash)
        if proc and proc.returncode is None:
            try:
                os.kill(proc.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass

    def resume(self, info_hash: str) -> None:
        proc = self._processes.get(info_hash)
        if proc and proc.returncode is None:
            try:
                os.kill(proc.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass

    def _cleanup(self, info_hash: str) -> None:
        tmpdir = self._tmpdirs.pop(info_hash, None)
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)

    def stop_all(self) -> None:
        for info_hash in list(self._processes.keys()):
            self.stop(info_hash)
        import subprocess
        try:
            subprocess.run(
                ["pkill", "-9", "-f", "webtorrent"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass


def _format_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b //= 1024
    return f"{b:.1f} GB"


_engine_instance: Optional[TorrentEngine] = None

def get_engine() -> TorrentEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = TorrentEngine()
    return _engine_instance
