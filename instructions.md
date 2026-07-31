# Freedom (Termux Fork) — AI Development Guide

This document tells AI agents how the project works, how to build, deploy, and debug it on an Android device using ADB, and what conventions to follow when making changes.

---

## 1. What is This Project

A fork of **[Termux](https://github.com/termux/termux-app)** (Android terminal emulator) rebranded as **"Freedom"** (`io.freedom`).

It bundles a **Python text‑based streaming app** (`anime_watch`) that lets the user search, browse, stream, and download anime/movies from online providers and torrents. The app works both on the desktop (Linux/macOS) and on **Android**, where it uses a **pre‑packaged Node.js webtorrent‑cli** and a **native MPV video player** (`is.xyz.mpv.VideoPlayerActivity`).

---

## 2. Project Layout

```
termux-app/
├── app/src/main/
│   ├── assets/
│   │   ├── anime_watch/         ← The Python streaming TUI (core logic)
│   │   │   ├── __main__.py      ← Entry point
│   │   │   ├── tui/             ← Textual screens, widgets, player
│   │   │   ├── torrent/         ← Torrent engine (webtorrent wrapper)
│   │   │   ├── providers/       ← Scraper providers (anikoto, bingr, etc.)
│   │   │   └── history.py, models.py, core.py, ...
│   │   ├── debs/                ← 100+ pre-built .deb packages (mpv, node, ffmpeg, python3.14, ...)
│   │   ├── setup_freedom.sh     ← Bootstrap script (first-run + upgrade)
│   │   └── webtorrent-bundle.tar ← Pre-packaged webtorrent-cli node_modules
│   ├── java/com/termux/app/     ← Main Termux Java code (TermuxActivity, TermuxInstaller, etc.)
│   ├── java/is/xyz/mpv/         ← Kotlin + Compose MPV video player
│   └── cpp/termux-bootstrap.c   ← Native JNI: embeds bootstrap .zip blob
├── termux-shared/               ← Shared library (constants, shell, file utils)
├── terminal-emulator/           ← Pure terminal emulation C/Java
├── terminal-view/               ← Terminal View Android widget
├── build.gradle                 ← Root Gradle (AGP 8.13.2, Kotlin 2.1.0)
├── settings.gradle              ← 4 modules: :app, :termux-shared, :terminal-emulator, :terminal-view
└── gradle.properties            ← sdk/ndk/minSdk/targetSdk versions
```

---

## 3. Building the APK

**Requirements:**

- Android SDK (compileSdk 36)
- NDK 28.0.13004108
- Java 17
- Gradle 9.2.1 (wrapper included)

**Commands:**

```bash
# Debug build (produces split APKs per ABI)
cd termux-app
./gradlew :app:assembleDebug

# The debug APK is signed with testkey_untrusted.jks (shared debug key)
# Output: app/build/outputs/apk/debug/freedom-debug_arm64-v8a.apk  (and others for x86_64, armeabi-v7a)
```

---

## 4. ADB Debugging Workflow

### 4.1 Install the APK

```bash
# Install/upgrade the debug APK
adb install -r app/build/outputs/apk/debug/freedom-debug_arm64-v8a.apk

# If installation fails (e.g., conflicting signature), uninstall first:
adb uninstall io.freedom
adb install app/build/outputs/apk/debug/freedom-debug_arm64-v8a.apk
```

### 4.2 Restart the App (force-stop + launch)

```bash
adb shell am force-stop io.freedom
adb shell monkey -p io.freedom 1    # launches the main activity
```

Wait at least **20 seconds** after launch before testing — the app runs `updateFreedomAssets()` and `copyWebtorrentBundle()` in background threads on `onServiceConnected()`. These copy and extract assets from the APK to the data directory.

### 4.3 Key ADB Commands for Debugging

#### App Data Directory Access

The app's private data is at `/data/data/io.freedom/` (or symlinked `/data/user/0/io.freedom/`).

```bash
# List app directory
adb shell run-as io.freedom ls -la

# List Termux prefix (binaries, libraries)
adb shell run-as io.freedom ls -la files/usr/bin/

# Read the Python engine.py on device
adb shell run-as io.freedom cat files/home/anime_watch/torrent/engine.py
```

**IMPORTANT:** `run-as` runs as the app's UID but the filesystem is **read-only** from `run-as`. You CANNOT write files via `run-as`. To modify files on the device, modify the Java source and rebuild the APK, or modify the Python assets (which get deployed on every restart via `updateFreedomAssets()`).

#### Running Commands in the Termux Environment

Use `adb exec-out` (not `adb shell`) — it properly passes through stdout and stderr:

```bash
# Check node version
adb exec-out run-as io.freedom sh -c 'files/usr/bin/node --version'

# Run Python
adb exec-out run-as io.freedom sh -c 'files/usr/bin/python3.14 -c "print(\"hello\")"'

# Test webtorrent
adb exec-out run-as io.freedom sh -c 'files/usr/bin/node files/usr/lib/node_modules/webtorrent-cli/bin/cmd.js --version'
```

> ⚠️ `adb shell run-as io.freedom command` sometimes swallows stdout on certain Android versions. Always use `adb exec-out run-as ...` instead.

#### Test Python imports

```bash
adb exec-out run-as io.freedom sh -c 'P=/data/data/io.freedom/files/usr; \
  $P/bin/python3.14 -c "
import sys
sys.path.insert(0,\"/data/data/io.freedom/files/home\")
from anime_watch.torrent.engine import get_engine
e = get_engine()
print(\"available:\", e.is_available())
"'
```

#### Checking Asset Deployment (engine.py on device)

```bash
# Check key methods exist
adb shell run-as io.freedom grep -n "def _construct_webtorrent_url\|def download_to_dir_sync\|def _launch_android_player\|_ANDROID" files/home/anime_watch/torrent/engine.py
```

#### Check uint8-util patch (arr2hex fix)

```bash
adb exec-out run-as io.freedom sh -c 'P=/data/data/io.freedom/files/usr; \
  grep "typeof data" $P/lib/node_modules/uint8-util/dist/src/node.js'
```

Expected output:
```
export const arr2base = (data) => typeof data === 'string' ? data : Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString('base64');
export const arr2hex = (data) => typeof data === 'string' ? data : Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString('hex');
```

#### Check Logcat for App Errors

```bash
# Filter app logs
adb logcat -s *:E | grep -i freedom

# Or watch all app logs
adb logcat --pid=$(adb shell pidof -s io.freedom)

# Check specific tag
adb logcat -s TermuxInstaller TermuxActivity
```

#### Take Screenshot of Video Player

```bash
adb shell screencap /sdcard/screenshot.png
adb pull /sdcard/screenshot.png
```

---

## 5. How Assets are Deployed at Runtime

The app deploys assets from the APK to the device's private data directory **on every startup**, not just the first install. This ensures Python files stay in sync with rebuilds.

### 5.1 Asset Copy Flow

1. **`TermuxActivity.onServiceConnected()`** calls:
   - `TermuxInstaller.updateFreedomAssets(context)` — copies `anime_watch/` and `setup_freedom.sh` from APK assets to `$HOME/`
   - `TermuxInstaller.copyWebtorrentBundle(context)` — extracts `webtorrent-bundle.tar` to `$PREFIX/lib/node_modules/` and creates the `webtorrent` wrapper at `$PREFIX/bin/webtorrent`

2. **Both methods run in background threads.** The files are replaced on every startup.

### 5.2 Where Assets Land

| APK Asset Path | Device Path |
|---|---|
| `assets/anime_watch/` | `/data/data/io.freedom/files/home/anime_watch/` |
| `assets/setup_freedom.sh` | `/data/data/io.freedom/files/home/setup_freedom.sh` |
| `assets/webtorrent-bundle.tar` | → extracted to `/data/data/io.freedom/files/usr/lib/node_modules/` |
| `assets/debs/` | `/data/data/io.freedom/files/home/debs/` (first-run only) |

### 5.3 When Assets are NOT Updated

- `debs/` are only installed on first run (by `setup_freedom.sh`)
- The `webtorrent-bundle.tar` is only re-extracted if the `webtorrent` binary or `webtorrent-cli` directory is missing (the Java code checks `exists()`)

If you need to force re-extraction, either:
- Delete the marker file: `adb shell run-as io.freedom rm files/home/.freedom_setup_done`
- Or manually remove the target and restart: `adb shell run-as io.freedom rm -rf files/usr/lib/node_modules/webtorrent-cli files/usr/bin/webtorrent`

---

## 6. The Torrent Engine (Android)

### 6.1 Architecture

```
User picks torrent
  → TUI (screens.py) calls engine.stream_and_save() or engine.stream_pipe()
    → Android branch in engine.py (if _ANDROID is True):
      1. download_to_dir_sync() starts:  subprocess.Popen(["webtorrent", "download", magnet, "--out", dest_dir])
      2. Thread reads stdout for "Server running at: http://localhost:PORT/"
      3. After getting base_url, constructs file URL:  http://localhost:PORT/webtorrent/<infoHash>/<filename>
      4. Polls until the URL returns HTTP 200 (up to 10s timeout)
      5. Returns the file URL
      6. _launch_android_player(url) sends:  am start -n io.freedom/is.xyz.mpv.VideoPlayerActivity --es url <URL>
```

### 6.2 Critical Knowledge

- **`/webtorrent/` URL prefix is REQUIRED.** The webtorrent HTTP server (`lib/server.js`) has `this.pathname = '/webtorrent'` and destroys requests that don't start with it. The `_construct_webtorrent_url` method must include `/webtorrent/` in the path.
- **`download_to_dir_sync()` returns the file URL or `None`.** It does NOT return a file path. It waits up to 30 seconds for the "Server running at:" message from webtorrent, then constructs the playable HTTP URL.
- **`track=False`** is used for streaming (so the process survives `stop_all` until app exit).
- **`arr2hex`/`arr2base` in `uint8-util`** are patched to handle string inputs because `parse-torrent` v9+ returns `infoHash` as a hex string, not a Buffer. Without this patch, webtorrent crashes on startup.
- **The webtorrent binary is a bash wrapper** (not a symlink) because Android lacks `/usr/bin/env`.

### 6.3 Known Dependencies and Versions

| Component | Version | Path on Device |
|---|---|---|
| Node.js | 26.4.0 | `files/usr/bin/node` |
| Python | 3.14 | `files/usr/bin/python3.14` |
| webtorrent-cli | 6.0.0 | `files/usr/lib/node_modules/webtorrent-cli/` |
| webtorrent | 2.8.5 | `files/usr/lib/node_modules/webtorrent/` |
| parse-torrent | 9.1.5 | `files/usr/lib/node_modules/parse-torrent/` |
| uint8-util | 2.3.2 | `files/usr/lib/node_modules/uint8-util/` |
| bash | (Termux) | `files/usr/bin/bash` |

---

## 7. Common Pitfalls

### 7.1 `run-as` Read-Only Filesystem

Modern Android (14+) makes the app private directory read-only even via `run-as`. You CANNOT write files with `run-as`. To modify files on-device, change the Java source and rebuild the APK — the `updateFreedomAssets()` method copies them on next restart.

### 7.2 `run-as` Swallows stdout

Always use `adb exec-out run-as io.freedom ...` instead of `adb shell run-as io.freedom ...`. The `exec-out` variant properly forwards both stdin and stderr.

### 7.3 `adb shell` vs `adb exec-out` for grep

`run-as` uses `/system/bin/sh` which is a minimal shell:
- No `grep` with `\|` alternation — use `-E` and `|` instead, or run multiple greps
- No `sed`/`awk` with extended features
- Write scripts to files (via the build/Java layer) rather than inline

### 7.4 webtorrent Needs Network

The webtorrent download command fetches metadata from DHT/trackers before starting its HTTP server. If the device has no network or the magnet has no seeders, `download_to_dir_sync` will time out (30s) and return `None`, so no player is launched. This is expected behavior.

### 7.5 The VideoPlayerActivity Launches via `am`

The Python code uses `termux-am start` (or `am start`) to launch `VideoPlayerActivity` with the stream URL. The `am` binary must be in PATH (it's at `$PREFIX/bin/am` — a Termux wrapper around Android's `/system/bin/am`).

---

## 8. Development Workflow

1. **Modify Python code** → change files in `app/src/main/assets/anime_watch/`
2. **Modify Java code** → change files in `app/src/main/java/`
3. **Build** → `./gradlew :app:assembleDebug`
4. **Install** → `adb install -r app/build/outputs/apk/debug/freedom-debug_arm64-v8a.apk`
5. **Restart + wait** → `adb shell am force-stop io.freedom && adb shell monkey -p io.freedom 1` (wait 20s)
6. **Verify** → use `adb exec-out run-as io.freedom ...` to check files and run tests

### 8.1 Quick Iteration (Python-only Changes)

If you only changed Python files in `assets/anime_watch/`, `assets/setup_freedom.sh`, or `assets/webtorrent-bundle.tar`, the app copies them on restart via `updateFreedomAssets()`/`copyWebtorrentBundle()`. So `rebuild → install → restart` is sufficient.

### 8.2 Modifying the webtorrent-bundle.tar

The bundle is pre-built:
1. Install `webtorrent-cli` with `--ignore-scripts` on a Linux arm64 machine
2. Create a stub for `node-datachannel` (pure JS mock — WebRTC is not needed)
3. Remove x86_64 native modules
4. Remove `node_modules/.cache` and test files
5. Tar + gzip the `node_modules` directory

The bundle is tracked in git (it's only ~16MB gzip). Update it when dependencies change.

### 8.3 Modifying uint8-util Patches

The `arr2hex`/`arr2base` patch is applied in `TermuxInstaller.copyWebtorrentBundle()` after extraction. If you update the webtorrent bundle and the `uint8-util` version changes, update the Java patch logic in `TermuxInstaller.java` accordingly.

---

## 9. Resetting the App State

```bash
# Full clean (uninstall → rebuild → install)
adb uninstall io.freedom
./gradlew :app:assembleDebug
adb install app/build/outputs/apk/debug/freedom-debug_arm64-v8a.apk

# Just clear app data (no rebuild needed)
adb shell pm clear io.freedom
adb shell monkey -p io.freedom 1
```

---

## 10. Architecture Overview

```
Freedom APK
├── Termux Core (Java/C++)
│   ├── Terminal emulator
│   ├── Package management (apt/dpkg via bootstrap)
│   └── Asset deployment
├── MPV Video Player (Kotlin/Compose)
│   └── VideoPlayerActivity — receives URL via Intent
├── Python Streaming TUI (assets/anime_watch/)
│   ├── tui/              — Textual screens, widgets, app
│   ├── providers/        — Scraper providers
│   ├── torrent/          — Torrent engine (webtorrent wrapper)
│   └── plugin/           — Plugin system scaffolding
└── Bootstrap Assets
    ├── debs/              — Pre-built .deb packages
    ├── setup_freedom.sh   — First-run installer script
    └── webtorrent-bundle.tar — Pre-packaged webtorrent-cli
```

4 Gradle modules:
- `:app` — Main Android application (Java + Kotlin/Compose)
- `:termux-shared` — Shared Termux constants, utilities
- `:terminal-emulator` — Pure terminal emulation (C/Java)
- `:terminal-view` — Terminal View Android widget

---

## 11. Development Workflow

### 11.1 Quick Iteration (Python-only Changes)

If you only changed Python files in `assets/anime_watch/`, `assets/setup_freedom.sh`, or `assets/webtorrent-bundle.tar`, the app copies them on restart via `updateFreedomAssets()`/`copyWebtorrentBundle()`. So `rebuild → install → restart` is sufficient.

```bash
make sync-assets  # copy Python source into APK assets
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/freedom-debug_arm64-v8a.apk
adb shell am force-stop io.freedom
adb shell monkey -p io.freedom 1   # wait 20s for asset deployment
```

Or push files directly without rebuild:
```bash
make push-python
```

### 11.2 Full Rebuild (Java/Kotlin/C++ changes)

```bash
./gradlew :app:assembleDebug && adb install -r app/build/outputs/apk/debug/freedom-debug_arm64-v8a.apk
adb shell am force-stop io.freedom && adb shell monkey -p io.freedom 1
```

---

## 12. The MPV Player

Located at `is.xyz.mpv.VideoPlayerActivity` (Kotlin/Compose). It receives the stream URL via Intent extra (`--es url <URL>`). No IPC port file needed — the Python engine launches it via `am start -n io.freedom/is.xyz.mpv.VideoPlayerActivity --es url <URL>`.

Gesture layer supports brightness/volume swipe controls. A transparent overlay was removed in the gesture layer (`VideoPlayerGestureLayer.kt`) to allow touch events to reach the player when controls are hidden.

---

## 13. Provider System

Providers are scrapers in `assets/anime_watch/providers/`. Each implements a `BaseProvider` with `search()`, `get_episodes()`, and `extract_stream()`.

Custom providers can be loaded via the plugin system (see `plugin/scaffold.py`). A minimal provider plugin looks like:

```python
# Place in ~/.config/anime_watch/plugins/my_provider.py
from anime_watch.providers.base import BaseProvider
from anime_watch.models import SearchResult, Episode, StreamSource

class MyProvider(BaseProvider):
    def search(self, query):
        return [SearchResult(title=f"{query} - found", url="https://...", site_name="MyProvider")]

    def get_episodes(self, result):
        return [Episode(title="Episode 1", url="https://...", number="1", site_name="MyProvider")]

    def extract_stream(self, episode, audio_pref="sub", quality_pref="best"):
        return StreamSource(url="https://...", headers={})
```

Drop a `.py` file into the plugins directory and it's picked up automatically on next launch.

---

## 14. Python Streaming TUI (Textual)

The TUI is built with [Textual](https://textual.textualize.io/), a Python TUI framework. Entry point: `python3 -m anime_watch`. Key modules:

- `tui/app.py` — Main Textual App with CSS styling
- `tui/screens.py` — Splash screen, browser, downloads screen
- `tui/player.py` — MPV integration and IPC management
- `tui/widgets.py` — Custom widgets (poster, logo, etc.)
- `torrent/engine.py` — Torrent downloading and streaming engine
- `core.py` — Core utilities (fetch, scrape, extract)
- `history.py` — Watch history (JSON-based, append-only log)
- `models.py` — Data models (SearchResult, Episode, StreamSource, TorrentResult)

---

## 15. Android-Specific Notes

- **bionic libc vs glibc**: Android uses bionic libc. Pre-compiled native Linux binaries (x86-64, arm64 glibc) like `opencode` or `claude` CLI do **not** work on Android. Node.js, Python, and other Termux packages are compiled for bionic.
- **run-as filesystem is read-only**: You cannot write files via `adb shell run-as io.freedom ...`. All file modifications must go through the Java asset deployment layer (rebuild APK or use `make push-python`).
- **Always use `adb exec-out`**: `adb shell run-as io.freedom ...` sometimes swallows stdout. Use `adb exec-out run-as io.freedom ...` instead.
- **No `/usr/bin/env`**: The webtorrent entry point is a bash wrapper with a hardcoded shebang.

---

## 16. Key Files to Know

| File | Purpose |
|---|---|
| `app/src/main/java/com/termux/app/TermuxInstaller.java` | Asset deployment (updateFreedomAssets, copyWebtorrentBundle, uint8-util patching) |
| `app/src/main/java/com/termux/app/TermuxActivity.java` | Main activity, triggers asset deployment on start |
| `app/src/main/java/is/xyz/mpv/` | MPV video player (Kotlin + Compose) |
| `app/src/main/assets/anime_watch/torrent/engine.py` | Torrent download/stream engine with Android branches |
| `app/src/main/assets/setup_freedom.sh` | Bootstrap installer + auto-launch |
| `app/src/main/assets/anime_watch/providers/` | All scraper providers |
| `app/src/main/assets/anime_watch/tui/screens.py` | TUI screens (Downloads screen with delete keybinding) |
| `app/src/main/assets/webtorrent-bundle.tar` | Pre-packaged node_modules for webtorrent-cli |
| `instructions.md` | Full AI agent development guide |

---

## 17. Gradle Configuration

- `applicationId`: `io.freedom` (rebranded from `com.termux`)
- `packageVariant`: `apt-android-7` (configurable via `TERMUX_PACKAGE_VARIANT` env var)
- Debug builds produce split APKs per ABI: `arm64-v8a`, `armeabi-v7a`, `x86_64`
- Signed with `testkey_untrusted.jks` (shared debug key)

---

## 18. Contributing

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
```
<type>: <description>

[optional body]
```
Types: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`. Breaking changes: add `!` before `:`.
