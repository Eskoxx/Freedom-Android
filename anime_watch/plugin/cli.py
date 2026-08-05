"""Plugin developer CLI.

Usage:
  python -m anime_watch plugin new <name> [--url URL] [--category cat]
  python -m anime_watch plugin generate <config.json>
  python -m anime_watch plugin validate <file>
  python -m anime_watch plugin test <file> [--query Q]
  python -m anime_watch plugin install <file>
  python -m anime_watch plugin discover <url> [--query Q] [--output FILE] [--test]
"""

from __future__ import annotations
import argparse
import os
import shutil
import sys


def _user_dir() -> str:
    return os.path.expanduser("~/.config/anime-watch/providers")


def _project_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "user_providers")


def run_plugin_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m anime_watch plugin")
    sub = parser.add_subparsers(dest="command", required=True)

    # new
    p_new = sub.add_parser("new", help="Generate a new provider plugin")
    p_new.add_argument("name", help="Display name (e.g. 'My Provider')")
    p_new.add_argument("--url", default="https://example.com", help="Base URL of the site")
    p_new.add_argument("--category", default="anime", choices=["anime", "movies"],
                       help="Site category")
    p_new.add_argument("-o", "--output", help="Output file path (default: user_providers/<slug>.py)")

    # generate (from JSON config)
    p_gen = sub.add_parser("generate", help="Generate a working plugin from a config file")
    p_gen.add_argument("config", help="Path to JSON config file describing the site structure")
    p_gen.add_argument("-o", "--output", help="Output directory (default: user_providers/)")

    # generate-interactive
    p_gi = sub.add_parser("generate-interactive",
                           help="Walk through site structure questions to generate a plugin")
    p_gi.add_argument("-o", "--output", help="Output directory (default: user_providers/)")

    # validate
    p_val = sub.add_parser("validate", help="Check a plugin file for correctness")
    p_val.add_argument("file", help="Path to plugin .py file")

    # test
    p_test = sub.add_parser("test", help="Run a live test cycle against a plugin")
    p_test.add_argument("file", help="Path to plugin .py file")
    p_test.add_argument("--query", default="test", help="Search query to use for testing")

    # install
    p_install = sub.add_parser("install", help="Copy a plugin file into user_providers/")
    p_install.add_argument("file", help="Path to plugin .py file")

    # discover
    p_discover = sub.add_parser("discover", help="Auto-analyze a site and generate plugin config")
    p_discover.add_argument("url", help="Base URL of the site to analyze (e.g. https://mysite.com)")
    p_discover.add_argument("--query", default="naruto",
                            help="Test query for search discovery (default: naruto)")
    p_discover.add_argument("--name", default="",
                            help="Display name (auto-generated from URL if not set)")
    p_discover.add_argument("--slug", default="",
                            help="Provider slug (auto-generated from URL if not set)")
    p_discover.add_argument("-o", "--output", help="Save generated config to this file")
    p_discover.add_argument("--test", action="store_true",
                            help="After discovery, generate and test the plugin automatically")

    args = parser.parse_args(argv)

    if args.command == "new":
        return _cmd_new(args)
    elif args.command == "generate":
        return _cmd_generate(args)
    elif args.command == "generate-interactive":
        return _cmd_generate_interactive(args)
    elif args.command == "validate":
        return _cmd_validate(args)
    elif args.command == "test":
        return _cmd_test(args)
    elif args.command == "install":
        return _cmd_install(args)
    elif args.command == "discover":
        from .discover import run_discover
        return run_discover(args)
    return 1


def _cmd_new(args) -> int:
    from .scaffold import generate_plugin

    slug = args.name.lower().strip().replace(" ", "-")
    code = generate_plugin(
        name=args.name,
        slug=slug,
        url=args.url,
        category=args.category,
    )

    if args.output:
        out_path = args.output
    else:
        out_path = os.path.join(_project_dir(), f"{slug}.py")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    if os.path.exists(out_path):
        print(f"File already exists: {out_path}")
        return 1

    with open(out_path, "w") as f:
        f.write(code)

    print(f"Created plugin: {out_path}")
    print()
    _print_next_steps(out_path)
    return 0


def _cmd_validate(args) -> int:
    from .validate import validate_source

    result = validate_source(args.file)
    print(result)
    return 0 if result.passed else 1


def _cmd_test(args) -> int:
    from .test import load_plugin, run_test

    provider = load_plugin(args.file)
    if provider is None:
        return 1

    print(f"Testing plugin: {args.file}")
    ok = run_test(provider, query=args.query)
    return 0 if ok else 1


def _cmd_install(args) -> int:
    src = args.file
    if not os.path.isfile(src):
        print(f"File not found: {src}")
        return 1

    dest_dir = _user_dir()
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))

    if os.path.exists(dest):
        print(f"Already installed: {dest}")
        return 1

    shutil.copy2(src, dest)
    print(f"Installed to: {dest}")
    print("Restart the app — the provider will be loaded automatically.")
    return 0


def _cmd_generate(args) -> int:
    from .generate import save_plugin
    from .validate import validate_source

    out = save_plugin(args.config, args.output)
    print()
    print("Validating generated plugin...")
    result = validate_source(out)
    print(result)
    if result.passed:
        _print_next_steps(out)
        return 0
    return 1


def _cmd_generate_interactive(args) -> int:
    from .generate import generate_config_interactive, generate_from_config, save_plugin
    import json, tempfile

    config = generate_config_interactive()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config, f, indent=2)
        tmp = f.name
    out = save_plugin(tmp, args.output)
    os.unlink(tmp)
    print()
    print(f"Saved to: {out}")
    _print_next_steps(out)
    return 0


def _print_next_steps(out_path: str) -> None:
    basename = os.path.basename(out_path)
    print("Next steps:")
    print()
    print(f"  1. Edit {out_path} and fill in the TODO sections")
    print(f"  2. Validate: python -m anime_watch plugin validate {out_path}")
    print(f"  3. Test:     python -m anime_watch plugin test {out_path} --query 'One Piece'")
    print(f"  4. Install:  python -m anime_watch plugin install {out_path}")
    print(f"  5. Restart the app and search")
    print()
    print("Need ideas for sites to plugin?")
    print("  Movies: https://fmhy.net/video")
    print("  Anime:  https://everythingmoe.com/")
    print()
    print("Or for local development, just save directly to user_providers/:")
    print(f"     cp {out_path} user_providers/")
    print("  and restart the app.")
