#!/usr/bin/env python3
"""Check that the up-next list follows the audio on air, not the wall clock."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from radio_server.timeline_api import (
    _expand_item_rows,
    _on_air_elapsed,
    _on_air_row_index,
    _project_row_times,
)
from radio_server.timeline_scheduler import Timeline, TimelineItem, TimelineScheduler


def _song(title: str, artist: str, duration: float) -> SimpleNamespace:
    """Build a minimal song for row expansion checks."""
    return SimpleNamespace(title=title, artist=artist, duration=duration)


def _item(
    item_id: str, start: datetime, status: str, content=None
) -> TimelineItem:
    """Build a minimal timeline item for ordering checks."""
    return TimelineItem(
        timeline_id=item_id,
        item_type="song_block",
        content=content,
        scheduled_start=start,
        estimated_duration=200.0,
        status=status,
    )


def test_playback_order() -> None:
    """The list starts at the on-air item, even when items were queued ahead."""
    base = datetime(2026, 8, 28, 16, 0, 0)
    # The broadcast loop queues ahead, so "done" and "ahead" are already marked
    # played while the audio for "on_air" is what listeners actually hear.
    items = [
        _item("done", base, "completed"),
        _item("on_air", base + timedelta(seconds=200), "playing"),
        _item("ahead", base + timedelta(seconds=400), "playing"),
        _item("later", base + timedelta(seconds=600), "ready"),
    ]
    timeline = Timeline(
        timeline_id="t",
        dj_id="dj",
        show_start=base,
        show_end=base + timedelta(hours=1),
        items=items,
        created_at=base,
        last_updated=base,
    )
    scheduler = TimelineScheduler(advanced_scheduler=None)

    # One already-played item is kept in front so a listener running behind the
    # live edge can still be shown what they are hearing.
    order = scheduler.get_playback_order(timeline, count=10, on_air_id="on_air")
    assert [i.timeline_id for i in order] == ["done", "on_air", "ahead", "later"], order

    order = scheduler.get_playback_order(
        timeline, count=10, on_air_id="on_air", lookbehind=0
    )
    assert [i.timeline_id for i in order] == ["on_air", "ahead", "later"], order

    assert scheduler.get_current_item(timeline, "on_air").timeline_id == "on_air"
    # Without a tag from the producer, fall back to the first in-flight item.
    assert scheduler.get_current_item(timeline).timeline_id == "on_air"

    # Without an anchor, completed items never reappear.
    assert [i.timeline_id for i in scheduler.get_playback_order(timeline)] == [
        "on_air",
        "ahead",
        "later",
    ]


def test_song_rows() -> None:
    """A song block becomes one row per song, anchored on the song on air."""
    base = datetime(2026, 8, 28, 16, 0, 0)
    block = SimpleNamespace(
        songs=[
            _song("Kiss From a Rose", "Seal", 300.0),
            _song("Back to Black", "Amy Winehouse", 0.0),
            _song("Mercy", "Duffy", 200.0),
        ],
        intro_style="warm",
        outro_style="tease",
    )
    rows = _expand_item_rows(_item("block", base, "playing", block))

    assert [r["title"] for r in rows] == [
        "Kiss From a Rose",
        "Back to Black",
        "Mercy",
    ]
    # DJ talk is mixed into the first and last songs of the block.
    assert rows[0]["dj_intro"] == "warm" and rows[0]["dj_outro"] is None
    assert rows[-1]["dj_outro"] == "tease"
    # A song with no measured duration falls back to a share of the estimate.
    assert rows[1]["duration"] == 200.0 / 3

    # The crossfaded mix republishes metadata per song, so the live track name
    # says which song of the block is playing — earlier songs are dropped.
    on_air = {"title": "Back to Black", "artist": "amy winehouse"}
    assert _on_air_row_index(rows, on_air) == 1
    # An unmatched track falls back to the start of the block.
    assert _on_air_row_index(rows, {"title": "Nope", "artist": "Nobody"}) == 0

    # Non-song items stay a single row.
    talk = SimpleNamespace(
        talk_type="dj_talk_types", prompt_style="trivia", content="Up next is Duffy"
    )
    talk_item = _item("talk", base, "ready", talk)
    talk_item.item_type = "dj_talk"
    talk_rows = _expand_item_rows(talk_item)
    assert len(talk_rows) == 1
    assert talk_rows[0]["title"] == "DJ talk"
    # talk_type is the same for every segment, so the style is what identifies it.
    assert talk_rows[0]["style"] == "trivia"
    assert talk_rows[0]["item_type"] == "dj_talk"


def test_row_times() -> None:
    """Row times are anchored on how much of the on-air song has played."""
    now = datetime(2026, 8, 28, 16, 0, 0, tzinfo=UTC)
    on_air = {"started_at": (now - timedelta(seconds=30)).isoformat()}
    assert _on_air_elapsed(on_air, now) == 30.0
    # A producer that has not stamped a start time must not shift the list.
    assert _on_air_elapsed({}, now) == 0.0
    assert _on_air_elapsed({"started_at": "not a time"}, now) == 0.0

    # Row 0 is lookbehind — already played — and row 1 is on air.
    rows = [
        {"duration": 60.0},
        {"duration": 200.0},
        {"duration": 100.0},
        {"duration": 50.0},
    ]
    # DJ talk mixed into the file means the song ends 40s out, not at its
    # tagged duration — the producer's number wins.
    _project_row_times(rows, 1, now, 30.0, 40.0)

    starts = [datetime.fromisoformat(row["projected_start"]) for row in rows]
    ends = [datetime.fromisoformat(row["projected_end"]) for row in rows]
    # The on-air row is stamped with when it actually started, so the client can
    # show its progress; the rest follow on from when it really ends.
    assert starts[1] == now - timedelta(seconds=30)
    assert ends[1] == now + timedelta(seconds=40)
    assert starts[2] == ends[1]
    assert ends[2] == starts[2] + timedelta(seconds=100)
    assert starts[3] == ends[2]
    # The already-played row is laid out backwards from the on-air row's start,
    # so a listener behind the live edge can still find what they are hearing.
    assert ends[0] == starts[1]
    assert starts[0] == starts[1] - timedelta(seconds=60)


if __name__ == "__main__":
    test_playback_order()
    test_song_rows()
    test_row_times()
    print("✅ playback order, song rows and times follow the on-air audio")
