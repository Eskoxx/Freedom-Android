from __future__ import annotations
import asyncio
import os
import re
import subprocess
import requests
from anime_watch.models import Episode, StreamSource
from anime_watch.core import SESSION, SCRAPE_TIMEOUT


class DownloadHandler:
    def __init__(self, app, update_status):
        self.app = app
        self._update_content = update_status

    def _update_dl(self, ep_title: str, prog_str: str):
        self.app.downloads[ep_title] = prog_str

    def _remove_dl(self, ep_title: str):
        if ep_title in self.app.downloads:
            del self.app.downloads[ep_title]

    async def _do_download(self, stream: StreamSource, episode: Episode):
        url = stream.url
        qp = self.app.quality_pref
        if qp != "best":
            h = qp.replace("p", "")
            fmt = f"best[height<=?{h}]"
        else:
            fmt = "bestvideo+bestaudio/best"

        clean_anime = re.sub(r'[\\/*?:"<>|]', "", episode.anime_name).strip()
        clean_ep = re.sub(r'[\\/*?:"<>|]', "", episode.title).strip()
        display_title = f"{clean_anime} - {clean_ep}"

        self._update_dl(display_title, "[··········] 0%")
        self._update_content(f"Started downloading {display_title}")

        out_dir = os.path.join("downloads", clean_anime)
        os.makedirs(out_dir, exist_ok=True)

        audio_str = self.app.audio_pref.title()
        qual_str = self.app.quality_pref
        out_name = os.path.join(out_dir, f"{clean_ep} ({audio_str}, {qual_str}).%(ext)s")

        args = [
            "yt-dlp",
            "--no-warnings",
            "--newline",
            "-f", fmt,
            "-o", out_name,
        ]
        headers = getattr(stream, 'headers', None)
        if headers:
            for k, v in headers.items():
                args.append(f"--add-header={k}:{v}")
        args.append(url)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )

            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode('utf-8', errors='replace').strip()
                if "[download]" in line_str and "%" in line_str:
                    m = re.search(r'\[download\]\s+([\d\.]+)%.*?at\s+([^\s]+).*?ETA\s+([^\s]+)', line_str)
                    if m:
                        pct_str = m.group(1)
                        try:
                            p_val = float(pct_str)
                        except ValueError:
                            p_val = 0

                        bar_len = 10
                        filled = int((p_val / 100.0) * bar_len)
                        empty = bar_len - filled
                        bar = "█" * filled + "·" * empty

                        prog_str = f"[{bar}] {pct_str}%"
                        self._update_dl(display_title, prog_str)

            await proc.wait()
            self._remove_dl(display_title)
            if proc.returncode == 0:
                subs = getattr(stream, "subtitles", None) or []
                if subs:
                    sh = dict(headers) if headers else {}
                    sub_base = f"{clean_ep} ({audio_str}, {qual_str})"
                    self._embed_subtitles(subs, out_dir, sub_base, url, sh, display_title)
                self._update_content(f"Download complete: {display_title}")
            else:
                self._update_content(f"Download failed: {display_title} (code {proc.returncode})")
        except FileNotFoundError:
            self._remove_dl(display_title)
            self._update_content("Error: yt-dlp not found.")

    def _embed_subtitles(self, subtitles, out_dir, sub_base, stream_url, headers, display_title):
        video_path = None
        for f in os.listdir(out_dir):
            if f.startswith(sub_base) and not f.endswith(".vtt"):
                video_path = os.path.join(out_dir, f)
                break
        if not video_path:
            return

        ffmpeg_ok = subprocess.run(["ffmpeg", "-version"],
                                    capture_output=True).returncode == 0
        sub_paths = []
        for sub in subtitles:
            try:
                sub_url = sub["url"]
                if not sub_url.startswith("http"):
                    base = stream_url.rsplit("/", 1)[0] if stream_url else ""
                    sub_url = f"{base}/{sub_url.lstrip('/')}"
                r = SESSION.get(sub_url, headers=headers or {},
                                timeout=SCRAPE_TIMEOUT)
                if r.status_code != 200:
                    continue
                lang = sub.get("lang", "unknown").replace("/", "_").split("-")[0].strip().lower()
                path = os.path.join(out_dir, f"{sub_base}.{lang}.vtt")
                with open(path, "wb") as f:
                    f.write(r.content)
                sub_paths.append((path, lang))
            except (requests.RequestException, OSError):
                continue

        if not sub_paths:
            return

        if ffmpeg_ok:
            self._update_content(f"Merging subtitles into {display_title[:40]}…")
            mkv_path = os.path.join(out_dir, f"{sub_base}.mkv")
            inputs = [video_path] + [p for p, _ in sub_paths]
            cmd = ["ffmpeg"]
            for inp in inputs:
                cmd += ["-i", inp]
            cmd += ["-c", "copy", "-map", "0"]
            for i in range(len(sub_paths)):
                cmd += ["-map", str(i + 1)]
            for i, (_, lang) in enumerate(sub_paths):
                cmd += ["-metadata:s:s:" + str(i), f"language={lang}"]
            cmd += ["-y", mkv_path]

            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode == 0:
                os.remove(video_path)
                for sp, _ in sub_paths:
                    os.remove(sp)
                return

        fallback = os.path.join(out_dir, f"{sub_base}.vtt")
        try:
            os.rename(sub_paths[0][0], fallback)
        except OSError:
            pass
        for sp, _ in sub_paths[1:]:
            try:
                os.remove(sp)
            except OSError:
                pass
