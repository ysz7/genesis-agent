"""Attachments: classifying files/docs for the REPL's /attach (and inlining)."""

from agent.runtime.attachments import (
    classify_attachment, inline_text_docs, build_user_prompt,
    is_attachable_path, extract_attachments,
)
from agent.console import display


def test_classify_text_doc_reads_and_inlines(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# Title\nbody text", encoding="utf-8")
    kind, val = classify_attachment(str(p))
    assert kind == "text"
    name, body = val
    assert name == "notes.md" and "body text" in body


def test_classify_image_is_media(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    kind, val = classify_attachment(str(p))
    assert kind == "media" and val.endswith("pic.png")


def test_classify_errors(tmp_path):
    assert classify_attachment("does_not_exist.md")[0] == "error"
    weird = tmp_path / "thing.xyz"
    weird.write_text("x", encoding="utf-8")
    assert classify_attachment(str(weird))[0] == "error"     # unsupported type


def test_classify_text_doc_respects_size_cap(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("A" * 50, encoding="utf-8")
    kind, val = classify_attachment(str(p), max_mb=0)        # 0 MB cap → truncated
    assert kind == "text" and "(truncated)" in val[1]


def test_inline_text_docs_delimits():
    out = inline_text_docs("question?", [("a.md", "AAA"), ("b.csv", "1,2")])
    assert "question?" in out
    assert "--- attached file: a.md ---" in out and "AAA" in out
    assert "--- attached file: b.csv ---" in out and "1,2" in out


def test_inline_no_docs_unchanged():
    assert inline_text_docs("just text", []) == "just text"


def test_is_attachable_path(tmp_path):
    md = tmp_path / "a.md"
    md.write_text("x", encoding="utf-8")
    png = tmp_path / "b.png"
    png.write_bytes(b"\x89PNG")
    bad = tmp_path / "c.zzz"
    bad.write_text("x", encoding="utf-8")
    assert is_attachable_path(str(md)) and is_attachable_path(f'"{png}"')
    assert not is_attachable_path(str(bad))           # unsupported type
    assert not is_attachable_path("nope.md")          # not a file


def test_extract_attachments_includes_text_docs(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("hi", encoding="utf-8")
    clean, paths = extract_attachments(f'see "{md}" thanks')
    assert [p for p in paths if p.endswith("notes.md")]
    assert clean == "see thanks"


def test_unique_placeholder_avoids_collision():
    store = {"[file: a.md]": "x"}
    assert display._unique(store, "[file: a.md]") == "[file: a.md (2)]"
    assert display._unique({}, "[file: a.md]") == "[file: a.md]"


def test_build_user_prompt_media_round_trip(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    prompt = build_user_prompt("see this", [str(img)], allow_local=True)
    assert isinstance(prompt, list) and prompt[0] == "see this"   # [text, *parts]
    assert len(prompt) == 2
