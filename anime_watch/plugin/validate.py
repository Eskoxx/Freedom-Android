from __future__ import annotations
import ast
import inspect
import os
from typing import Optional

from anime_watch.providers.base import BaseProvider

REQUIRED_ATTRS = ["name", "slug", "url", "category"]
REQUIRED_METHODS = ["search", "get_episodes", "extract_stream"]
OPTIONAL_METHODS = ["get_supported_qualities", "get_supported_audio"]


class ValidationResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def __str__(self) -> str:
        lines: list[str] = []
        if self.passed:
            lines.append("✓ Validation passed")
        else:
            lines.append(f"✗ Validation failed ({len(self.errors)} error(s))")
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  WARN:  {w}")
        return "\n".join(lines)


def validate_source(source_path: str) -> ValidationResult:
    result = ValidationResult()

    if not os.path.isfile(source_path):
        result.errors.append(f"File not found: {source_path}")
        return result

    if not source_path.endswith(".py"):
        result.errors.append("File must have a .py extension")
        return result

    # Parse syntax
    try:
        with open(source_path) as f:
            tree = ast.parse(f.read(), filename=source_path)
    except SyntaxError as e:
        result.errors.append(f"Syntax error: {e}")
        return result

    # Find classes inheriting BaseProvider
    provider_classes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = (
                    base.id if isinstance(base, ast.Name)
                    else base.attr if isinstance(base, ast.Attribute)
                    else ""
                )
                if base_name == "BaseProvider":
                    provider_classes.append(node.name)

    if not provider_classes:
        result.errors.append(
            "No class inheriting from BaseProvider found. "
            "Your plugin must define a class like `class MyProvider(BaseProvider):`"
        )
        return result

    # Validate each provider class
    # We need to actually import the module to check methods properly
    module = _import_plugin(source_path)
    if module is None:
        result.errors.append("Failed to import plugin module (see traceback above)")
        return result

    for class_name in provider_classes:
        cls = getattr(module, class_name, None)
        if cls is None:
            result.errors.append(f"Class {class_name} not found after import")
            continue

        _validate_class(cls, result)

    return result


def _import_plugin(source_path: str):
    import importlib.util
    import sys

    mod_name = "_validate_" + os.path.splitext(os.path.basename(source_path))[0]
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    try:
        spec = importlib.util.spec_from_file_location(mod_name, source_path)
        if not spec or not spec.loader:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:
        print(f"  Import error: {exc}")
        return None


def _validate_class(cls: type, result: ValidationResult) -> None:
    # Required attributes
    for attr in REQUIRED_ATTRS:
        val = getattr(cls, attr, None)
        if not val or (isinstance(val, str) and not val.strip()):
            result.errors.append(
                f"{cls.__name__} missing required attribute '{attr}'"
            )

    # Required methods
    for method_name in REQUIRED_METHODS:
        method = getattr(cls, method_name, None)
        if not method or not callable(method):
            result.errors.append(
                f"{cls.__name__} missing required method '{method_name}(...)'"
            )
            continue
        # Check it's overridden (not the BaseProvider default)
        if _is_default_implementation(method, method_name):
            result.warnings.append(
                f"{cls.__name__}.{method_name}() uses BaseProvider default "
                "(returns empty [])"
            )

    # Category validation
    cat = getattr(cls, "category", "")
    if cat not in ("anime", "movies"):
        result.warnings.append(
            f"category should be 'anime' or 'movies', got {cat!r}"
        )


def _is_default_implementation(method, method_name: str) -> bool:
    try:
        source = inspect.getsource(method)
        if method_name == "search":
            return "return []" in source
        if method_name == "get_episodes":
            return "return []" in source
        if method_name == "extract_stream":
            return "return None" in source
    except (OSError, TypeError):
        pass
    return False
