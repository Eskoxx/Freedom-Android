import io
import os
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.request

_DESKTOP_REPO = "Eskoxx/Freedom"
_ANDROID_REPO = "Eskoxx/Freedom-Android"
_ANDROID_ASSET_PREFIX = "app/src/main/assets/anime_watch"
_ANDROID_DATA_DIR = "/data/data/io.freedom"

_UA = "Freedom/2.0 (+auto-update)"


def _is_android() -> bool:
    return os.environ.get("ANDROID_ROOT") is not None or os.path.isdir(_ANDROID_DATA_DIR)


def _pkg_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _repo() -> str:
    if _is_android():
        return _ANDROID_REPO
    return _DESKTOP_REPO


def _asset_prefix() -> str:
    if _is_android():
        return _ANDROID_ASSET_PREFIX
    return "anime_watch"


def _tarball_url() -> str:
    return f"https://codeload.github.com/{_repo()}/tar.gz/refs/heads/main"


def _raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{_repo()}/main/{_asset_prefix()}/{path}"


def _parse_version(text: str) -> int:
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else 0


def _read_local_version() -> int:
    marker = os.path.join(_pkg_dir(), ".update-version")
    try:
        with open(marker, "r", encoding="utf-8") as f:
            return _parse_version(f.read())
    except (OSError, ValueError):
        return 0


def _write_local_version(remote_text: str) -> None:
    marker = os.path.join(_pkg_dir(), ".update-version")
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(remote_text.strip() + "\n")
    except OSError:
        pass


def fetch_remote_version(timeout: float = 5.0) -> str:
    req = urllib.request.Request(_raw_url(".update-version"), headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _download_tarball(timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(_tarball_url(), headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _strip_top_dir(tarinfo) -> tarfile.TarInfo:
    parts = tarinfo.name.split("/", 1)
    if len(parts) == 2:
        tarinfo.name = parts[1]
    return tarinfo


def _apply_tarball(data: bytes) -> int:
    pkg = _pkg_dir()
    prefix = _asset_prefix()
    tmp = tempfile.mkdtemp(prefix="freedom-update-")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                rel = _strip_top_dir(member).name
                if rel == "" or not rel.startswith(prefix + "/"):
                    continue
                dest_rel = rel[len(prefix) + 1:]
                dest = os.path.join(tmp, dest_rel)
                if member.isdir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        count = 0
        for root, _dirs, files in os.walk(tmp):
            for fname in files:
                if fname.endswith((".pyc", ".pyo")) or "__pycache__" in root:
                    continue
                src = os.path.join(root, fname)
                rel = os.path.relpath(src, tmp)
                dest = os.path.join(pkg, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                count += 1
        return count
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _apply_update_async(remote_text: str) -> None:
    def run():
        try:
            data = _download_tarball()
            count = _apply_tarball(data)
            if count > 0:
                _write_local_version(remote_text)
                notice = os.path.join(_pkg_dir(), ".update-pending")
                try:
                    with open(notice, "w", encoding="utf-8") as f:
                        f.write(remote_text.strip() + "\n")
                except OSError:
                    pass
        except Exception:
            pass

    t = threading.Thread(target=run, daemon=True, name="freedom-updater")
    t.start()


def check_for_updates(timeout: float = 5.0, apply: bool = True) -> bool:
    """Check remote for a newer version. Returns True if an update is pending/applying.

    Fail-open: any error (offline, HTTP, parse) is swallowed and returns False.
    """
    try:
        remote_text = fetch_remote_version(timeout)
    except Exception:
        return False
    remote = _parse_version(remote_text)
    local = _read_local_version()
    if remote <= local:
        return False
    if apply:
        _apply_update_async(remote_text)
    return True


def pending_update_notice() -> str:
    notice = os.path.join(_pkg_dir(), ".update-pending")
    try:
        with open(notice, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def clear_pending_notice() -> None:
    notice = os.path.join(_pkg_dir(), ".update-pending")
    try:
        os.unlink(notice)
    except OSError:
        pass
