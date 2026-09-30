#!/usr/bin/env python3
"""Checks that SKEW completion lines report the producer's real on-air duration
rather than the span from the queue timestamp, which runs minutes early."""

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from radio_server.radio_station import _log_skew_at_completion

SCHEDULED = datetime.now().replace(microsecond=0)


def _item(queue_lead_seconds: float, estimated: float) -> SimpleNamespace:
    """Build an item queued `queue_lead_seconds` before its scheduled start."""
    actual_start = SCHEDULED - timedelta(seconds=queue_lead_seconds)
    return SimpleNamespace(
        item_type="jingle",
        dj_id="test_dj",
        estimated_duration=estimated,
        scheduled_start=SCHEDULED,
        actual_start=actual_start,
        # Audio only reached the stream ~345s after queueing, so the queue-to-end
        # span is nothing like the item's duration.
        actual_end=actual_start + timedelta(seconds=345 + estimated),
    )


def _capture(item, **kwargs) -> str:
    """Return the single SKEW line emitted for `item`."""
    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logger = logging.getLogger("radio_server.radio_station")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        _log_skew_at_completion(item, **kwargs)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    assert len(records) == 1, records
    return records[0]


def test_on_air_duration_wins() -> None:
    """A real streamed duration is what gets compared to the estimate."""
    line = _capture(
        _item(queue_lead_seconds=8.0, estimated=18.07),
        source="audio_finished",
        on_air_seconds=18.11,
    )
    assert "actual=18.11s" in line, line
    assert "delta=+0.04s" in line, line
    assert "measured=on_air" in line, line
    assert "queue_drift=-8.00s" in line, line


def test_queue_span_only_as_fallback() -> None:
    """Without a measurement the line falls back, and says so."""
    line = _capture(_item(queue_lead_seconds=8.0, estimated=18.07), source="fallback")
    assert "measured=queue_to_end" in line, line
    assert "actual=363.07s" in line, line


def test_missing_timestamps_still_log() -> None:
    """An item that never recorded timestamps logs rather than raising."""
    item = _item(queue_lead_seconds=8.0, estimated=18.07)
    item.actual_start = None
    item.actual_end = None
    line = _capture(item, source="fallback")
    assert "actual=missing" in line, line


if __name__ == "__main__":
    test_on_air_duration_wins()
    test_queue_span_only_as_fallback()
    test_missing_timestamps_still_log()
    print("✅ skew logging checks passed")
