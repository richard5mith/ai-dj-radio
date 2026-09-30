#!/usr/bin/env python3
"""Checks for scheduler fixes: never-discard groups, committed-item shift
guards, schedule slot boundaries, and 24-hour schedule slots."""

import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace

from radio_server.advanced_program_scheduler import (
    AdvancedProgramScheduler,
    ScheduleSegment,
)
from radio_server.config_manager import ConfigManager
from radio_server.timeline_scheduler import (
    Timeline,
    TimelineItem,
    TimelineScheduler,
)

NOW = datetime.now().replace(microsecond=0)


def _item(item_id: str, start: datetime, status: str = "scheduled") -> TimelineItem:
    """Build a minimal 100-second timeline item."""
    return TimelineItem(
        timeline_id=item_id,
        item_type="song_block",
        content=None,
        scheduled_start=start,
        estimated_duration=100.0,
        status=status,
    )


def _timeline_with(*items: TimelineItem) -> Timeline:
    """Build a timeline around the given items."""
    return Timeline(
        timeline_id="t1",
        dj_id="test_dj",
        show_start=NOW,
        show_end=NOW + timedelta(hours=1),
        items=list(items),
        created_at=NOW,
        last_updated=NOW,
    )


def _schedulers() -> tuple[TimelineScheduler, AdvancedProgramScheduler]:
    """Build schedulers with no external services (paths not exercised here)."""
    advanced = AdvancedProgramScheduler.__new__(AdvancedProgramScheduler)
    advanced.weights = {}
    timeline = TimelineScheduler(advanced, config_manager=None)
    return timeline, advanced


def test_shift_never_moves_committed_items() -> None:
    """Queued items keep their scheduled_start; pending items still shift."""
    scheduler, _ = _schedulers()
    items = [
        _item("queued", NOW, status="queued"),
        _item("pending_a", NOW + timedelta(seconds=100)),
        _item("pending_b", NOW + timedelta(seconds=200)),
    ]
    timeline = _timeline_with(*items)

    scheduler._shift_timeline_from_index(timeline, 0, 30.0)

    assert items[0].scheduled_start == NOW
    assert items[1].scheduled_start == NOW + timedelta(seconds=130)
    assert items[2].scheduled_start == NOW + timedelta(seconds=230)


def test_extend_schedules_oversized_group_instead_of_discarding() -> None:
    """A group longer than the remaining window is appended, never dropped."""
    scheduler, _ = _schedulers()
    block = _item("existing", NOW, status="playing")
    timeline = _timeline_with(block)

    # 20-minute group requested while the extension window has ~60s left:
    # previously discarded, must now be scheduled.
    oversized = ScheduleSegment(
        segment_id="big_block",
        segment_type="song_block",
        content=SimpleNamespace(),
        start_time=NOW,
        estimated_duration=1200.0,
        dj_id="test_dj",
    )
    scheduler.advanced_scheduler.virtual_current_time = NOW
    calls = {"count": 0}

    def next_segment() -> list[ScheduleSegment]:
        calls["count"] += 1
        return [oversized] if calls["count"] == 1 else []

    scheduler.advanced_scheduler.get_next_segment = next_segment
    scheduler._insert_stings = lambda _timeline: None  # isolate from jingles

    previous_end = scheduler._find_timeline_end(timeline)
    scheduler.extend_timeline(timeline, extend_minutes=1)

    assert len(timeline.items) == 2
    assert timeline.items[-1].estimated_duration == 1200.0
    assert timeline.items[-1].scheduled_start == previous_end


def test_recalc_skips_committed_but_chains_past_them() -> None:
    """Duration recalc leaves queued items alone and chains pending after them."""
    scheduler, _ = _schedulers()
    changed = _item("changed", NOW)
    queued = _item("queued", NOW + timedelta(seconds=100), status="queued")
    # Pending item with a bogus gap: recalc must pull it up to the queued item's end.
    pending = _item("pending", NOW + timedelta(seconds=500))
    timeline = _timeline_with(changed, queued, pending)

    changed.estimated_duration = 150.0  # grew by 50s
    asyncio.run(scheduler._recalculate_timeline_times(timeline, changed))

    assert queued.scheduled_start == NOW + timedelta(seconds=100)
    assert pending.scheduled_start == queued.scheduled_end


def test_slot_at_previous_hour_end_is_still_due() -> None:
    """A :58 slot with 2-minute grace fires when checked at the top of the next hour."""
    _, advanced = _schedulers()
    base = datetime(2026, 8, 28, 15, 0, 0)

    slot = advanced._find_due_slot_time({"at_minutes": [58], "grace_minutes": 2}, base)
    assert slot == base.replace(hour=14, minute=58)

    # Same-hour behaviour unchanged
    slot = advanced._find_due_slot_time(
        {"at_minutes": [10]}, base.replace(minute=11, second=42)
    )
    assert slot == base.replace(minute=10)

    # Outside the grace window on both sides: nothing due
    slot = advanced._find_due_slot_time(
        {"at_minutes": [58], "grace_minutes": 2}, base.replace(minute=3)
    )
    assert slot is None


def test_equal_start_end_is_a_24_hour_slot() -> None:
    """start == end covers the whole day instead of matching nothing."""
    manager = ConfigManager.__new__(ConfigManager)
    assert manager._time_in_range(time(3, 0), time(0, 0), time(0, 0))
    # Normal semantics preserved: exclusive end boundary
    assert not manager._time_in_range(time(16, 0), time(14, 0), time(16, 0))


if __name__ == "__main__":
    test_shift_never_moves_committed_items()
    test_extend_schedules_oversized_group_instead_of_discarding()
    test_recalc_skips_committed_but_chains_past_them()
    test_slot_at_previous_hour_end_is_still_due()
    test_equal_start_end_is_a_24_hour_slot()
    print("✅ scheduler fix checks pass")
