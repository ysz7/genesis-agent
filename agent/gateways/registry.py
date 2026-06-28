"""Gateway discovery: built-in channels + user ``gateways/*.py`` (Phase 22a).

Mirrors ``engine.registry.discover_tools``: the built-in channels (Telegram,
WhatsApp) are always known, and a vertical can drop its own
``<root>/gateways/my_channel.py`` exposing a ``GATEWAY`` class to add a new one.
A channel module is imported lazily, so a missing optional one (not yet written)
or a broken user file never breaks startup — it's just skipped with a warning.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Any

from .base import Gateway

logger = logging.getLogger("agent.gateways")

# Built-in channels, by module name within this package. Each module exposes a
# ``GATEWAY`` attribute (a Gateway subclass). Loaded lazily and tolerantly.
_BUILTIN = ("telegram", "whatsapp")


def _builtin_gateways() -> dict[str, type[Gateway]]:
    out: dict[str, type[Gateway]] = {}
    for mod_name in _BUILTIN:
        try:
            module = importlib.import_module(f".{mod_name}", __package__)
        except Exception as exc:  # noqa: BLE001 - an unfinished/broken channel is skipped
            logger.debug("gateway %r not available: %s", mod_name, exc)
            continue
        cls = getattr(module, "GATEWAY", None)
        if isinstance(cls, type) and issubclass(cls, Gateway) and cls.name:
            out[cls.name] = cls
    return out


def _user_gateways(root: Path) -> dict[str, type[Gateway]]:
    """Load ``<root>/gateways/*.py`` channels (each exposing ``GATEWAY``)."""
    out: dict[str, type[Gateway]] = {}
    gw_dir = root / "gateways"
    if not gw_dir.is_dir():
        return out
    for py_file in sorted(gw_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        mod_name = f"_genesisagent_gateway_{py_file.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, py_file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't kill startup
            logger.warning("skipped gateways/%s: %s", py_file.name, exc)
            continue
        cls = getattr(module, "GATEWAY", None)
        if isinstance(cls, type) and issubclass(cls, Gateway) and cls.name:
            out[cls.name] = cls
    return out


def discover_gateways(config: Any) -> dict[str, type[Gateway]]:
    """All available channel classes by name (built-in first, user overrides win)."""
    out = _builtin_gateways()
    out.update(_user_gateways(config.root))
    return out


def gateway_names(config: Any) -> list[str]:
    """Names of all discoverable channels (for the menu / CLI listing)."""
    return sorted(discover_gateways(config).keys())


def get_gateway(config: Any, name: str, deps: Any) -> Gateway:
    """Instantiate the channel *name*. Raises ``KeyError`` if unknown."""
    classes = discover_gateways(config)
    cls = classes.get(name)
    if cls is None:
        known = ", ".join(sorted(classes)) or "(none)"
        raise KeyError(f"unknown gateway {name!r}; available: {known}")
    return cls(config, deps)
