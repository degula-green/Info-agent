from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.application.report.period import previous_week, resolve_week, week_of

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _local(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)


def test_week_of_starts_on_monday_midnight():
    window = week_of(_local("2026-10-07 15:30"))

    assert window.start == _local("2026-10-05 00:00")
    assert window.end == _local("2026-10-12 00:00")
    assert window.start_date == "2026-10-05"
    assert window.end_date == "2026-10-11"


def test_previous_week_is_the_last_complete_natural_week():
    window = previous_week(_local("2026-10-07 15:30"))

    assert window.start_date == "2026-09-28"
    assert window.end_date == "2026-10-04"
    assert window.label == "2026-09-28 至 2026-10-04"
    assert window.period_text == "上一周周报（2026-09-28 至 2026-10-04）"
    assert window.default_file_stem == "2026-09-28-2026-10-04"


def test_monday_midnight_starts_a_new_natural_week():
    before = week_of(_local("2026-10-04 23:59"))
    after = week_of(_local("2026-10-05 00:00"))

    assert before.start_date == "2026-09-28"
    assert after.start_date == "2026-10-05"
    # A report written on Monday morning still describes the week that ended.
    assert previous_week(_local("2026-10-05 00:00")).start_date == "2026-09-28"


def test_resolve_week_defaults_to_previous_week():
    now = _local("2026-10-07 15:30")

    for instruction in (None, "", "给我写一份周报", "按这个模板给他写周报"):
        window = resolve_week(instruction, now=now)
        assert window.start_date == "2026-09-28"
        assert window.end_date == "2026-10-04"


def test_resolve_week_honours_this_week_and_explicit_ranges():
    now = _local("2026-10-07 15:30")

    this_week = resolve_week("写本周的周报", now=now)
    assert (this_week.start_date, this_week.end_date) == ("2026-10-05", "2026-10-11")

    # "这一周" is how people say it in speech; it was only matching the
    # default (previous week) before.
    spoken_week = resolve_week("生成我这一周的周报", now=now)
    assert (spoken_week.start_date, spoken_week.end_date) == (
        "2026-10-05",
        "2026-10-11",
    )

    explicit = resolve_week("写 2026-09-21 到 2026-09-27 的周报", now=now)
    assert (explicit.start_date, explicit.end_date) == ("2026-09-21", "2026-09-27")


def test_window_iso_bounds_are_utc_and_cover_the_local_week():
    window = previous_week(_local("2026-10-07 15:30"))

    assert window.start_iso == "2026-09-27T16:00:00Z"
    assert window.end_iso == "2026-10-04T16:00:00Z"


def test_naive_timestamps_are_treated_as_utc_by_the_caller():
    naive = datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)
    window = previous_week(naive)

    # 2026-10-07 23:30 Shanghai is still Wednesday, so the week is unchanged.
    assert window.start_date == "2026-09-28"
    assert window.end - window.start == timedelta(days=7)


def test_resolve_week_reads_the_week_before_last():
    now = _local("2026-10-09 01:36")

    for instruction in (
        "生成我的上上一周的周报",
        "写上上周的周报",
        "写上上个星期的周报",
    ):
        window = resolve_week(instruction, now=now)
        assert (window.start_date, window.end_date) == ("2026-09-21", "2026-09-27")
        assert window.period_text == "上上周周报（2026-09-21 至 2026-09-27）"


def test_resolve_week_keeps_last_week_distinct_from_the_week_before_last():
    now = _local("2026-10-09 01:36")

    # Regression: "上一周" is a substring of "上上一周"; the longer phrase must
    # win, or every "上上一周" request silently reports the previous week.
    last_week = resolve_week("写上周的周报", now=now)
    assert (last_week.start_date, last_week.end_date) == ("2026-09-28", "2026-10-04")

    before_last = resolve_week("写上上一周的周报", now=now)
    assert (before_last.start_date, before_last.end_date) == (
        "2026-09-21",
        "2026-09-27",
    )


def test_resolve_week_counts_more_than_two_weeks_back():
    window = resolve_week("写上上上周的周报", now=_local("2026-10-09 01:36"))

    assert (window.start_date, window.end_date) == ("2026-09-14", "2026-09-20")
