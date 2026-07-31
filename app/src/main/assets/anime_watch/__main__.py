import os, sys
sys.path.insert(0, os.path.dirname(__file__) + "/..")

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "plugin":
        from anime_watch.plugin.cli import run_plugin_cli
        return run_plugin_cli(sys.argv[2:])
    from anime_watch.tui.app import run_app
    run_app()
    return 0

if __name__ == "__main__":
    sys.exit(main())
