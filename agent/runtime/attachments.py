"""Multimodal input (Phase 15) — attach images/documents to a run.

Turns user-provided files / URLs into Pydantic AI content parts so vision-capable
models can see them. The "user prompt" becomes ``[text, <part>, ...]`` instead of
a plain string; with no attachments it stays a plain string (behaviour unchanged).

Trust split: CLI/REPL attachments are the operator's own files (any local path is
fine — an explicit, trusted action). The HTTP server passes ``allow_local=False``
so it accepts **URLs only** — a local path from a remote caller would be an
arbitrary-file-read hole.

Pure stdlib here (plus Pydantic AI, already a dependency); nothing heavy.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
DOC_EXTS = {".pdf"}
ATTACH_EXTS = IMAGE_EXTS | DOC_EXTS                       # multimodal (vision) parts
# Text documents are read and inlined into the prompt as text, so they work on
# ANY model (no vision needed) — code, notes, data, configs.
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".env", ".log", ".xml", ".html", ".css",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".h", ".cpp", ".sh", ".bat", ".ps1", ".sql",
}
DEFAULT_MAX_MB = 10


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def max_mb_from(settings: dict) -> int:
    """The per-attachment size cap from settings (``attachments.max_mb``)."""
    return int((settings.get("attachments") or {}).get("max_mb", DEFAULT_MAX_MB))


def build_user_prompt(task: str, attachments, *, allow_local: bool, max_mb: int = DEFAULT_MAX_MB):
    """Return ``task`` unchanged if no attachments, else ``[text, *parts]``.

    Anything unreadable / oversized / unsupported is skipped with a note appended
    to the text rather than raising — a bad attachment shouldn't kill the run.
    """
    if not attachments:
        return task
    from pydantic_ai import BinaryContent, DocumentUrl, ImageUrl

    parts: list = []
    notes: list[str] = []
    for raw in attachments:
        item = str(raw).strip().strip('"').strip("'")
        if not item:
            continue
        ext = Path(item).suffix.lower()
        if _is_url(item):
            parts.append(DocumentUrl(url=item) if ext in DOC_EXTS else ImageUrl(url=item))
            continue
        if not allow_local:
            notes.append(f"(skipped '{item}': only URLs are accepted here)")
            continue
        path = Path(item)
        if not path.is_file():
            notes.append(f"(skipped '{item}': not a file)")
            continue
        if ext not in ATTACH_EXTS:
            notes.append(f"(skipped '{path.name}': unsupported type)")
            continue
        data = path.read_bytes()
        if len(data) > max_mb * 1024 * 1024:
            notes.append(f"(skipped '{path.name}': over {max_mb} MB)")
            continue
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(BinaryContent(data=data, media_type=media))

    text = task + (("\n" + " ".join(notes)) if notes else "")
    return [text, *parts] if parts else text


def classify_attachment(path_str: str, max_mb: int = DEFAULT_MAX_MB) -> tuple[str, object]:
    """Classify a local path for the REPL's ``/attach``.

    Returns one of:
      ``("media", path)``         — image/PDF → a multimodal part (needs vision)
      ``("text", (name, body))``  — a text document, read to inline into the prompt
      ``("error", message)``      — not a file / too big / unsupported type
    """
    item = path_str.strip().strip('"').strip("'")
    if not item:
        return ("error", "no path given")
    path = Path(item)
    if not path.is_file():
        return ("error", f"not a file: {item}")
    ext = path.suffix.lower()
    cap = max_mb * 1024 * 1024
    if ext in ATTACH_EXTS:
        if path.stat().st_size > cap:
            return ("error", f"'{path.name}' is over {max_mb} MB")
        return ("media", str(path))
    if ext in TEXT_EXTS:
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ("error", f"could not read '{path.name}': {exc}")
        if len(body) > cap:
            body = body[:cap] + "\n…(truncated)"
        return ("text", (path.name, body))
    return ("error", f"unsupported type: '{path.name}'")


def inline_text_docs(task: str, docs: list[tuple[str, str]]) -> str:
    """Append text documents to *task* as clearly delimited blocks."""
    out = task
    for name, body in docs:
        out += f"\n\n--- attached file: {name} ---\n{body}"
    return out


_ATTACHABLE = ATTACH_EXTS | TEXT_EXTS


def is_attachable_path(path_str: str) -> bool:
    """True if *path_str* points at an existing, attachable file (image/PDF/doc)."""
    item = path_str.strip().strip('"').strip("'")
    if not item:
        return False
    p = Path(item)
    return p.is_file() and p.suffix.lower() in _ATTACHABLE


def extract_attachments(line: str) -> tuple[str, list[str]]:
    """Pull dropped-file paths out of a typed REPL line (Claude-Code-style).

    A terminal inserts the file's path when you drag it in; we detect tokens that
    are real attachable files (images/PDF/text docs, quoted or bare), peel them
    out, and return ``(prompt_text, [paths])``. Requiring the file to actually
    exist means a word that merely looks like a path won't be misread.
    """
    candidates: list[str] = []
    for quoted in re.findall(r'"([^"]+)"|\'([^\']+)\'', line):
        candidates.append(quoted[0] or quoted[1])
    rest = re.sub(r'"[^"]+"|\'[^\']+\'', " ", line)
    candidates += rest.split()

    remaining = line
    attached: list[str] = []
    for cand in candidates:
        path = Path(cand.replace("\\ ", " "))
        if path.is_file() and path.suffix.lower() in _ATTACHABLE:
            attached.append(str(path))
            for token in (f'"{cand}"', f"'{cand}'", cand):
                remaining = remaining.replace(token, " ")
    return " ".join(remaining.split()).strip(), attached


def prompt_text(prompt) -> str:
    """The text part(s) of a prompt, for logging/monitor display (never the bytes)."""
    if isinstance(prompt, str):
        return prompt
    return " ".join(p for p in prompt if isinstance(p, str))


_VISION_MARKERS = (
    "image", "vision", "multimodal", "media_type", "media type", "modalit",
    "binarycontent", "imageurl", "documenturl",
)


def vision_hint(exc: Exception) -> str:
    """A hint to append to an error when it looks like an unsupported-input case."""
    msg = str(exc).lower()
    if any(m in msg for m in _VISION_MARKERS):
        return (
            " — this model may not support image/file input; try a vision-capable "
            "model (e.g. gpt-4.1, claude-*)"
        )
    return ""
