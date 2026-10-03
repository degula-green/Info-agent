from app.cells import block_range, format_range, parse_range, parse_ref, to_ref


def test_parse_ref_reads_letters_and_row() -> None:
    assert parse_ref("A1") == (1, 1)
    assert parse_ref("C12") == (12, 3)
    assert parse_ref("AA10") == (10, 27)


def test_to_ref_is_the_inverse_of_parse_ref() -> None:
    for ref in ("A1", "B2", "Z26", "AA27", "AB100"):
        assert to_ref(*parse_ref(ref)) == ref


def test_parse_range_normalizes_order() -> None:
    assert parse_range("C4:A1") == (1, 1, 4, 3)
    assert parse_range("B2") == (2, 2, 2, 2)


def test_block_range_covers_the_whole_block() -> None:
    assert block_range("A5", 2, 3) == "A5:C6"
    assert format_range(1, 1, 1, 1) == "A1:A1"
