from __future__ import annotations
import importlib.util
import os
import sys
import time
from typing import Optional


def load_plugin(filepath: str):
    """Import a plugin .py file and return the first BaseProvider instance."""
    mod_name = "_plugintest_" + os.path.splitext(os.path.basename(filepath))[0]
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, filepath)
    if not spec or not spec.loader:
        print("  ERROR: Could not load spec from file")
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Find BaseProvider subclass
    from anime_watch.providers.base import BaseProvider
    for val in vars(mod).values():
        if isinstance(val, type) and issubclass(val, BaseProvider) and val is not BaseProvider:
            return val()
    print("  ERROR: No BaseProvider subclass found in file")
    return None


def run_test(
    provider,
    query: str = "test",
    timeout: int = 15,
) -> bool:
    """Run a full test cycle: search → get_episodes → extract_stream."""
    print(f"\n  Provider: {provider.name}")
    print(f"  Slug:     {getattr(provider, 'slug', 'N/A')}")
    print(f"  URL:      {getattr(provider, 'url', 'N/A')}")
    print(f"  Category: {getattr(provider, 'category', 'N/A')}")
    print(f"  Query:    {query}")
    print()

    # 1. Test search
    print("  ── Step 1: search() ──")
    t0 = time.time()
    try:
        results = provider.search(query)
        elapsed = time.time() - t0
    except Exception as e:
        print(f"  FAILED: search() raised {type(e).__name__}: {e}")
        return False

    print(f"  Results: {len(results)}  ({elapsed:.1f}s)")
    if not results:
        print("  (no results — stopping test)")
        return False

    for i, r in enumerate(results[:5]):
        print(f"    [{i}] {r.title}")
        print(f"        url: {r.url}")
        print(f"        image: {r.image[:70] if r.image else '(none)'}")
    if len(results) > 5:
        print(f"    ... and {len(results) - 5} more")

    # 2. Test get_episodes
    print(f"\n  ── Step 2: get_episodes(result[0]) ──")
    first_result = results[0]
    t0 = time.time()
    try:
        episodes = provider.get_episodes(first_result)
        elapsed = time.time() - t0
    except Exception as e:
        print(f"  FAILED: get_episodes() raised {type(e).__name__}: {e}")
        return False

    print(f"  Episodes: {len(episodes)}  ({elapsed:.1f}s)")
    if not episodes:
        print("  (no episodes — stopping test)")
        return False

    for i, ep in enumerate(episodes[:3]):
        print(f"    [{i}] Ep {ep.number}: {ep.title}")
    if len(episodes) > 3:
        print(f"    ... and {len(episodes) - 3} more")

    # 3. Test extract_stream
    print(f"\n  ── Step 3: extract_stream(episode[0]) ──")
    first_ep = episodes[0]
    t0 = time.time()
    try:
        stream = provider.extract_stream(first_ep)
        elapsed = time.time() - t0
    except Exception as e:
        print(f"  FAILED: extract_stream() raised {type(e).__name__}: {e}")
        return False

    print(f"  Stream:  {elapsed:.1f}s")
    if stream:
        print(f"    URL: {stream.url[:100]}")
        print(f"    Direct: {stream.is_direct}")
        print(f"    Headers: {stream.headers}")
        print(f"    Subtitles: {bool(stream.subtitles)}")
    else:
        print("    (no stream returned)")
        return False

    print(f"\n  ✓ Full test cycle passed")
    return True
