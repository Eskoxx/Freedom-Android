from __future__ import annotations
import importlib.util
import logging
import os
import sys
from typing import Optional

from anime_watch.models import Site
from .base import BaseProvider

logger = logging.getLogger(__name__)

USER_PROVIDER_DIRS = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "user_providers"),
    os.path.expanduser("~/.config/anime-watch/providers"),
]


def _find_provider_dirs() -> list[str]:
    return [d for d in USER_PROVIDER_DIRS if os.path.isdir(d)]


def _load_plugin_file(filepath: str) -> list[tuple[str, BaseProvider]]:
    mod_name = "_user_provider_" + os.path.splitext(os.path.basename(filepath))[0]
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    try:
        spec = importlib.util.spec_from_file_location(mod_name, filepath)
        if not spec or not spec.loader:
            return []
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        logger.warning("Failed to load plugin %s: %s", filepath, exc)
        return []

    providers: list[tuple[str, BaseProvider]] = []
    for val in vars(mod).values():
        if isinstance(val, type) and issubclass(val, BaseProvider) and val is not BaseProvider:
            instance = val()
            key = instance.name.lower().strip()
            if not key:
                logger.warning("Plugin %s has empty name, skipping", val.__name__)
                continue
            if not instance.slug:
                logger.warning("Plugin %s has empty slug, falling back to name key", val.__name__)
            if not instance.url:
                logger.warning("Plugin %s has empty url", val.__name__)
            for method in ("search", "get_episodes", "extract_stream"):
                impl = getattr(instance, method, None)
                if impl and impl.__func__ is getattr(BaseProvider, method, None):
                    logger.info("Plugin %s does not override %s (using default)", val.__name__, method)
            providers.append((key, instance))
    return providers


def discover_providers(extra_dirs: Optional[list[str]] = None) -> dict[str, tuple[BaseProvider, Site]]:
    dirs = _find_provider_dirs()
    if extra_dirs:
        dirs.extend(d for d in extra_dirs if os.path.isdir(d))

    result: dict[str, tuple[BaseProvider, Site]] = {}
    for plugin_dir in dirs:
        for fname in sorted(os.listdir(plugin_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            filepath = os.path.join(plugin_dir, fname)
            if not os.path.isfile(filepath):
                continue
            for key, instance in _load_plugin_file(filepath):
                if key in result:
                    logger.info("Skipping duplicate plugin %r from %s", key, filepath)
                    continue
                slug = getattr(instance, "slug", None) or key
                site = Site(
                    name=instance.name,
                    slug=slug,
                    url=getattr(instance, "url", ""),
                    rank=99,
                    category=getattr(instance, "category", "anime"),
                )
                result[key] = (instance, site)
    return result
