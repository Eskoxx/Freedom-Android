import os, sys
sys.path.insert(0, os.path.dirname(__file__) + "/..")

def _progress_bar(done: int, total: int, width: int = 18) -> str:
    frac = done / total if total else 0.0
    filled = int(width * frac)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {done}/{total}"

def _check_updates_terminal() -> None:
    from anime_watch.updater import (
        apply_update_sync, fetch_remote_version, _parse_version, _read_local_version,
    )
    print("Checking for updates…")
    try:
        remote_text = fetch_remote_version()
    except Exception:
        print("Could not reach GitHub — continuing with the installed version.")
        return
    remote = _parse_version(remote_text)
    local = _read_local_version()
    if remote <= local:
        return

    print(f"New version v{remote_text.strip()} available (current v{local}).")
    print("Downloading from GitHub…")

    def _on_progress(done: int, total: int, rel: str) -> None:
        sys.stderr.write(f"\r\033[K  {_progress_bar(done, total)}  {rel}")
        sys.stderr.flush()
        if done == total:
            sys.stderr.write("\n")

    if apply_update_sync(remote_text, progress_cb=_on_progress):
        print(f"Update installed (v{remote_text.strip()}).")
    else:
        print("Update failed — continuing with the installed version.")

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "plugin":
        from anime_watch.plugin.cli import run_plugin_cli
        return run_plugin_cli(sys.argv[2:])
    _check_updates_terminal()
    from anime_watch.tui.app import run_app
    run_app()
    return 0

if __name__ == "__main__":
    sys.exit(main())
