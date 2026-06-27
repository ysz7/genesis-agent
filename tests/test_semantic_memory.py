"""Phase 19: semantic long-term memory — relevance recall with recency fallback."""

from types import SimpleNamespace

from pydantic_ai.usage import RunUsage

from agent.runtime import memory
from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.tools.selfimprove import remember, memory_digest

_ON = "self_improvement:\n  enabled: true\nmemory:\n  semantic: true\n"


def _fake_embed(deps, text, timeout=10.0):
    """Toy 2-D embedder: 'cat' topic → [1,0], 'tax' topic → [0,1]."""
    t = text.lower()
    if "cat" in t:
        return [1.0, 0.0]
    if "tax" in t:
        return [0.0, 1.0]
    return [0.5, 0.5]


def _deps(tmp_path):
    return build_deps(load_config(tmp_path))


def test_semantic_recall_prefers_relevant_over_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "embed", _fake_embed)
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    deps = _deps(tmp_path)
    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    try:
        remember(ctx, "to herd cats, use treats")    # relevant, older
        remember(ctx, "file taxes before April")     # irrelevant, newer
        digest = memory.semantic_recall(deps, "how do I handle my cats?", limit=1)
        assert "cats" in digest and "taxes" not in digest
        assert digest.startswith("Relevant lessons")
    finally:
        close_deps(deps)


def test_embed_failure_falls_back_to_recency(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "embed", _fake_embed)
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    deps = _deps(tmp_path)
    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    try:
        remember(ctx, "to herd cats, use treats")
        remember(ctx, "file taxes before April")
        # now the query can't be embedded → recency wins (newest = taxes)
        monkeypatch.setattr(memory, "embed", lambda deps, text, timeout=10.0: None)
        digest = memory.semantic_recall(deps, "anything about cats?", limit=1)
        assert "taxes" in digest and digest.startswith("Lessons from past")
    finally:
        close_deps(deps)


def test_disabled_is_recency_and_stores_no_embedding(tmp_path):
    # semantic OFF (Phase 11f behaviour): no embedding stored, recency digest.
    (tmp_path / "settings.yaml").write_text("self_improvement:\n  enabled: true\n", encoding="utf-8")
    assert memory.semantic_enabled({}) is False
    deps = _deps(tmp_path)
    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    try:
        remember(ctx, "alpha lesson")
        remember(ctx, "beta lesson")
        raw = (deps.memory_dir / "lessons.jsonl").read_text(encoding="utf-8")
        assert "embedding" not in raw           # no vectors written when off
        digest = memory_digest(deps.workspace, 5)
        assert "alpha lesson" in digest and "beta lesson" in digest
    finally:
        close_deps(deps)


def test_cosine_basic():
    assert memory._cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert memory._cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert memory._cosine([], [1.0]) == -1.0
