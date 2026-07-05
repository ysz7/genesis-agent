"""Phase 27a: secret redaction — ``.env`` values never reach the model or answer.

The filesystem sandbox deliberately does not constrain ``run_shell`` (a shell
can ``cd`` anywhere), so ``cat .env`` — typed by a user, or induced by prompt
injection in fetched web content — would put raw API keys into the model
context and, from there, into answers, transcripts, and provider logs. This
module closes that hole at the *content* level: the VALUES loaded from the
agent's ``.env`` are replaced with ``[secret:NAME]`` in every tool's output
(wrapped in ``engine.registry``) and in the final answer (an output validator
in ``engine.factory``).

ON by default — the template's defaults are the product. Opt out with
``redact_secrets: false`` in ``settings.yaml`` (trusted setups only).

What counts as a secret: every ``.env`` value except the known non-secret
config keys (``PROVIDER`` / ``MODEL`` / ``BASE_URL``), values shorter than 8
chars (too short to be a credential, too likely to over-redact), and plain
URLs without embedded credentials. Redacting a non-secret like a numeric
owner id is harmless; leaking a key is not — the filter errs on redaction.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

#: Replacement for a leaked value; the NAME tells the model (and the user)
#: what was redacted without revealing anything.
PLACEHOLDER = "[secret:{name}]"

#: ``.env`` keys that are configuration, not credentials.
NONSECRET_KEYS = {"PROVIDER", "MODEL", "BASE_URL"}

#: Values shorter than this are never treated as secrets.
MIN_LEN = 8


def enabled(settings: dict) -> bool:
    """True unless ``redact_secrets: false`` is set (ON by default)."""
    return settings.get("redact_secrets", True) is not False


def collect_secrets(root: Path) -> dict[str, str]:
    """Read ``<root>/.env`` and return ``{NAME: value}`` for its secret values."""
    path = Path(root) / ".env"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for key, value in (dotenv_values(path) or {}).items():
        if not key or not value:
            continue
        v = value.strip()
        if len(v) < MIN_LEN or key.upper() in NONSECRET_KEYS:
            continue
        low = v.lower()
        # A plain endpoint URL (BASE_URL-style, under any name) isn't a secret —
        # unless it embeds credentials (user:pass@host).
        if low.startswith(("http://", "https://")) and "@" not in v:
            continue
        out[key] = v
    return out


def redact_text(text: str, secrets: dict[str, str]) -> str:
    """Replace every secret value in *text* with its placeholder.

    Longer values are replaced first so a secret that contains another (e.g. a
    token and its prefix) never leaves a recognizable remnant behind.
    """
    for name in sorted(secrets, key=lambda n: len(secrets[n]), reverse=True):
        value = secrets[name]
        if value in text:
            text = text.replace(value, PLACEHOLDER.format(name=name))
    return text


def redact_value(value, secrets: dict[str, str]):
    """Redact strings inside *value* (str · list · dict); other types pass through."""
    if not secrets:
        return value
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_value(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(v, secrets) for k, v in value.items()}
    return value
