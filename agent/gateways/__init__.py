"""Messaging gateways — the fourth entrypoint (Phase 22).

One agent, many channels. A gateway is a thin adapter that turns an inbound
platform message into a ``build_agent`` run and sends the reply back, exactly the
way ``server`` adapts the agent to HTTP and ``console`` adapts it to a terminal.
The engine stays frozen; gateways live entirely at the edge.

Shipped in core (no extra, no heavy SDK): the Telegram / WhatsApp APIs are plain
HTTPS, driven through the ``httpx`` client we already have. A channel stays
dormant until its token is set and ``gateways.<name>.enabled`` is true.

This subpackage never imports ``rich`` — like ``server``, it must stay clean for
headless/Docker. Public pieces:

- :class:`Inbound` / :class:`Outbound` — the normalized message shapes.
- :class:`Pipeline` — the shared inbound→agent→outbound core (per-user threads).
- :class:`AccessControl` — deny-all allowlist with persisted ``/allow``.
- :class:`Gateway` — the base every channel subclasses.
- :func:`discover_gateways` — built-in + user ``gateways/*.py`` channels.
"""

from __future__ import annotations

from .base import (
    AccessControl,
    Gateway,
    Inbound,
    Outbound,
    Pipeline,
    Quota,
    any_gateway_enabled,
    gateway_enabled,
    gateway_settings,
    store_guard,
)
from .registry import discover_gateways, get_gateway, gateway_names

__all__ = [
    "AccessControl", "Gateway", "Inbound", "Outbound", "Pipeline", "Quota",
    "any_gateway_enabled", "gateway_enabled", "gateway_settings", "store_guard",
    "discover_gateways", "get_gateway", "gateway_names",
]
