"""Source adapters: one module per database, all producing the staging schema.

`get_adapter` resolves the `adapter` field of a source of `sources.toml` to
the `build` function of the module of the same name.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

from ..config import Source
from .base import AdapterContext, AdapterResult

AdapterBuild = Callable[[Source, AdapterContext], AdapterResult]

ADAPTER_MODULES = ("openfoodfacts", "usda_fdc", "ciqual", "swiss", "cnf", "cofid")


def get_adapter(name: str) -> AdapterBuild:
    """Return the build function of an adapter.

    Args:
        name: Adapter name, as written in the `adapter` field of a source.

    Returns:
        The `build(source, context)` function of the adapter module.

    Raises:
        KeyError: When no adapter has that name.
    """
    if name not in ADAPTER_MODULES:
        raise KeyError(f"unknown adapter: {name!r} (known: {', '.join(ADAPTER_MODULES)})")
    return import_module(f"{__name__}.{name}").build


__all__ = ["ADAPTER_MODULES", "AdapterBuild", "AdapterContext", "AdapterResult", "get_adapter"]
