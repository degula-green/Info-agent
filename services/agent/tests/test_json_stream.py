"""Boundary tests for the incremental JSON first-field extractor."""

from __future__ import annotations

import codecs

import pytest

from app.infrastructure.llm.json_stream import (
    FirstStringFieldExtractor,
    extract_string_field,
)


CASES: list[tuple[str, list[str], str]] = [
    ("simple", ['{"answer":"你好，世界","citations":[]}'], "你好，世界"),
    ("escape split", ['{"answer":"a\\', 'n b"}'], "a\n b"),
    ("unicode split", ['{"answer":"\\u4', 'f60\\u597d"}'], "你好"),
    ("surrogate split", ['{"answer":"\\ud83d', '\\ude00"}'], "😀"),
    ("raw emoji", ['{"answer":"😀"}'], "😀"),
    (
        "code fence and later field",
        ['```json\n{"citations":[],"answer":"hi"}\n```'],
        "hi",
    ),
    ("nested decoy", ['{"note":{"answer":"nope"},"answer":"yes"}'], "yes"),
    ("truncated value", ['{"answer":"partial'], "partial"),
    ("escaped quote", ['{"answer":"say \\"hi\\""}'], 'say "hi"'),
    ("field not found", ['{"foo":"bar"}'], ""),
    ("non-string value", ['{"answer":123,"reply":"x"}'], ""),
    (
        "citations first",
        ['{"citations":[{"x":"answer"}],"answer":"ok"}'],
        "ok",
    ),
]


@pytest.mark.parametrize("name,chunks,expected", CASES)
def test_extracts_field_across_chunk_boundaries(
    name: str, chunks: list[str], expected: str
) -> None:
    assert extract_string_field(chunks, "answer") == expected, name


@pytest.mark.parametrize("name,chunks,expected", CASES)
def test_extracts_field_when_every_character_is_its_own_chunk(
    name: str, chunks: list[str], expected: str
) -> None:
    """This covers every possible split point, including key/colon splits."""

    text = "".join(chunks)
    assert extract_string_field(list(text), "answer") == expected, name


def test_key_and_colon_split_are_held_until_resolved() -> None:
    extractor = FirstStringFieldExtractor("answer")
    assert extractor.feed('{"answer"') == ""
    assert extractor.matched is True
    assert extractor.feed(":") == ""
    assert extractor.feed('"hello"') == "hello"
    assert extractor.done is True


def test_colon_and_value_quote_split_are_held_until_resolved() -> None:
    extractor = FirstStringFieldExtractor("answer")
    assert extractor.feed('{"answer":') == ""
    assert extractor.feed('"hello"') == "hello"


def test_incomplete_escape_is_buffered_across_chunks() -> None:
    extractor = FirstStringFieldExtractor("answer")
    assert extractor.feed('{"answer":"a\\') == "a"
    assert extractor.feed("n b") == "\n b"
    assert extractor.feed('"}') == ""


def test_high_surrogate_is_held_until_low_surrogate_arrives() -> None:
    extractor = FirstStringFieldExtractor("answer")
    assert extractor.feed('{"answer":"\\ud83d') == ""
    assert extractor.feed('\\ude00"}') == "😀"


def test_lone_high_surrogate_is_emitted_only_at_finish() -> None:
    extractor = FirstStringFieldExtractor("answer")
    assert extractor.feed('{"answer":"\\ud83d') == ""
    assert extractor.finish() == "\ud83d"


def test_finish_keeps_partial_text_when_json_is_truncated() -> None:
    extractor = FirstStringFieldExtractor("answer")
    assert extractor.feed('{"answer":"partial') == "partial"
    assert extractor.finish() == ""


def test_incremental_utf8_decoding_keeps_split_emoji_intact() -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    raw = '{"answer":"😀"}'.encode("utf-8")
    chunks = [
        piece
        for piece in (decoder.decode(raw[index : index + 1]) for index in range(len(raw)))
        if piece
    ]
    tail = decoder.decode(b"", True)
    if tail:
        chunks.append(tail)
    assert extract_string_field(chunks, "answer") == "😀"


def test_real_dashscope_chunk_shape() -> None:
    """DashScope streams Chinese as \\uXXXX escapes and splits the key/colon."""

    chunks = [
        '{"',
        'answer": "',
        "\\u4f60\\u597d\\u3002\\u6ca1\\u6709",
        '\\u8bc1\\u636e\\u3002",',
        ' "citations": []}',
    ]
    assert extract_string_field(chunks, "answer") == "你好。没有证据。"


def test_reply_field_uses_the_same_extractor() -> None:
    assert extract_string_field(['{"reply":"晚上好"}'], "reply") == "晚上好"


def test_empty_field_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        FirstStringFieldExtractor(" ")


def test_empty_chunks_are_ignored() -> None:
    extractor = FirstStringFieldExtractor("answer")
    assert extractor.feed("") == ""
    assert extractor.feed('{"answer":"x"}') == "x"
