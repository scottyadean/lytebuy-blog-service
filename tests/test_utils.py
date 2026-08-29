""" Pure-helper tests for utils.

Storage moved to S3, so the old connection-string templating (resolve_uri) is
gone. What matters now is that the storage-agnostic helpers behave correctly for
the JSON-in-S3 model, in particular that dates round-trip as iso strings.
"""
from datetime import datetime, timezone

from src.utils import format_timestamp, utc_now


def test_format_timestamp_passes_an_iso_string_through_unchanged():
    # Dates are stored in S3 as iso strings and read back as strings, so a
    # serializer must return them as-is rather than choke on a non-datetime.
    stored = "2024-08-28T14:30:00+00:00"
    assert format_timestamp(stored) == stored


def test_format_timestamp_renders_a_naive_datetime_as_utc():
    naive = datetime(2024, 8, 28, 14, 30, 0)
    assert format_timestamp(naive) == "2024-08-28T14:30:00+00:00"


def test_format_timestamp_normalises_an_aware_datetime_to_utc():
    aware = datetime(2024, 8, 28, 14, 30, 0, tzinfo=timezone.utc)
    assert format_timestamp(aware) == "2024-08-28T14:30:00+00:00"


def test_format_timestamp_leaves_none_alone():
    assert format_timestamp(None) is None


def test_utc_now_is_timezone_aware_and_millisecond_truncated():
    now = utc_now()
    assert now.tzinfo is not None
    # Truncated to milliseconds: no sub-millisecond microseconds remain.
    assert now.microsecond % 1000 == 0
