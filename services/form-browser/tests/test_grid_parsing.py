from app.grid import parse_tsv, to_tsv, trim_matrix


def test_parse_tsv_handles_crlf_and_a_trailing_newline() -> None:
    text = "学号\t姓名\r\n1000021\t金小獴\r\n"
    assert parse_tsv(text) == [["学号", "姓名"], ["1000021", "金小獴"]]


def test_parse_tsv_returns_nothing_for_an_empty_clipboard() -> None:
    assert parse_tsv("") == []
    assert parse_tsv("\r\n") == []


def test_to_tsv_round_trips() -> None:
    matrix = [["a", "b"], ["c", "d"]]
    assert parse_tsv(to_tsv(matrix)) == matrix


def test_trim_matrix_drops_scanned_padding() -> None:
    scanned = [
        ["学号", "姓名", "", ""],
        ["1000021", "金小獴", "", ""],
        ["", "", "", ""],
        ["", "", "", ""],
    ]
    assert trim_matrix(scanned) == [["学号", "姓名"], ["1000021", "金小獴"]]


def test_trim_matrix_keeps_interior_blanks() -> None:
    scanned = [
        ["学号", "姓名", "性别"],
        ["1000021", "", "男"],
    ]
    assert trim_matrix(scanned) == scanned


def test_trim_matrix_on_an_empty_scan() -> None:
    assert trim_matrix([]) == []
    assert trim_matrix([["", ""], ["", ""]]) == []
