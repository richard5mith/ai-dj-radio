"""Regression coverage for recency, handovers, and retained timeline items."""

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytz

from radio_server.advanced_program_scheduler import (
    AdvancedProgramScheduler,
    RecentTracker,
    Song,
    DJTalkSegment,
    JingleSegment,
    ScheduleSegment,
)
from radio_server.radio_station import RadioStation
from radio_server.continuous_audio_producer import ContinuousAudioProducer
from radio_server.timeline_scheduler import Timeline, TimelineItem, TimelineScheduler


class SchedulerReliabilityTests(unittest.TestCase):
    """Exercise real scheduling decisions without external audio services."""

    def setUp(self) -> None:
        """Create isolated history and a deterministic clock."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tracker = RecentTracker(
            state_file=str(Path(self.directory.name) / "history.json")
        )
        self.now = datetime.now(UTC)
        self.songs = [
            Song(str(i), str(i), "artist", "", 2000, "", 180) for i in range(30)
        ]

    def test_exhaustion_keeps_latest_other_dj_song_excluded(self) -> None:
        """Changing DJs must not release the most recent station play."""
        for song in self.songs:
            self.tracker.add_song(song.artist, song.title, "outgoing")
        available = self.tracker.get_available_songs(self.songs, "incoming")
        self.assertTrue(available)
        self.assertNotIn(self.songs[-1], available)

    def test_small_library_relaxes_oldest_exclusions_first(self) -> None:
        """A two-song library alternates instead of releasing its latest song."""
        for song in self.songs[:2]:
            self.tracker.add_song(song.artist, song.title, "dj")
        self.assertEqual(
            self.tracker.get_available_songs(self.songs[:2], "dj"), self.songs[:1]
        )

    def test_history_counts_play_events_globally(self) -> None:
        """Repeated plays and other DJs age old events out of the count window."""
        self.tracker.max_recent = 3
        self.tracker.add_song("artist", "old", "retired")
        for _ in range(3):
            self.tracker.add_song("artist", "repeat", "active")
        self.assertFalse(self.tracker.is_recent("artist", "old", "retired"))

    def test_history_expires_without_further_plays(self) -> None:
        """A retired DJ cannot keep songs excluded forever."""
        self.tracker.add_song(
            "artist", "old", "retired", played_at=self.now - timedelta(days=2)
        )
        self.assertFalse(self.tracker.is_recent("artist", "old", "retired"))

    def test_reservations_are_not_plays_and_survive_exhaustion(self) -> None:
        """Pending music stays excluded without being persisted as played."""
        self.tracker.reserve_songs("block", self.songs[:1])
        self.assertEqual(self.tracker.get_available_songs(self.songs[:1], "dj"), [])
        self.assertFalse(self.tracker.is_recent("artist", "0", "dj"))
        loaded = RecentTracker(state_file=str(self.tracker.state_file))
        self.assertEqual(
            loaded.get_available_songs(self.songs[:1], "dj"), self.songs[:1]
        )

    def make_timeline(self, offset: int) -> tuple[TimelineScheduler, Timeline]:
        """Build an outgoing show whose last song ends around its boundary."""
        boundary = pytz.UTC.localize(datetime(2026, 9, 5, 14))
        end = boundary + timedelta(seconds=offset)
        entry = SimpleNamespace(
            dj_name="incoming",
            start_time=boundary.time(),
            end_time=boundary.replace(hour=15).time(),
            music_folders=[],
        )
        config = SimpleNamespace(
            timezone=pytz.UTC,
            schedule=[entry],
            get_schedule_entry_at_time=lambda _: entry,
            get_dj_config=lambda _: None,
        )
        advanced = SimpleNamespace(
            virtual_current_time=end, get_next_segment=lambda: []
        )
        scheduler = TimelineScheduler(advanced, config_manager=config)
        scheduler._now = lambda: boundary - timedelta(minutes=10)
        scheduler._insert_stings = lambda _: None
        scheduler.on_dj_switch = lambda *args: None
        item = TimelineItem(
            "song",
            "song_block",
            None,
            end - timedelta(seconds=180),
            180,
            dj_id="outgoing",
        )
        return scheduler, Timeline(
            "t", "outgoing", boundary - timedelta(hours=4), boundary, [item], end, end
        )

    def test_handover_never_scheduled_early(self) -> None:
        """An early timeline tail must not trigger the next show."""
        scheduler, timeline = self.make_timeline(-240)
        boundary = timeline.show_end
        scheduler.extend_timeline(timeline)
        self.assertTrue(
            all(
                i.scheduled_start >= boundary
                for i in timeline.items
                if i.item_type.startswith("dj_transition")
            )
        )
        self.assertEqual(timeline.dj_id, "outgoing")

    def test_late_handover_keeps_both_announcements(self) -> None:
        """Recovery cannot silently replace a late show's handover."""
        scheduler, timeline = self.make_timeline(360)
        original = timeline.items[0]
        scheduler.extend_timeline(timeline)
        self.assertIs(timeline.items[0], original)
        self.assertEqual(
            [i.item_type for i in timeline.items[1:3]],
            ["dj_transition_end", "dj_transition_start"],
        )

    def test_failed_item_remains_next_and_retries_preparation(self) -> None:
        """Preparation failure must not let later items overtake the failure."""
        scheduler, timeline = self.make_timeline(0)
        item = timeline.items[0]
        item.status = "failed"
        self.assertIs(scheduler.get_next_unplayed_item(timeline), item)
        scheduler._prepare_item = AsyncMock(return_value=False)

        async def prepare() -> None:
            """Give the scheduled preparation task a turn."""
            await scheduler.prepare_timeline_items(timeline, item.scheduled_start)
            await asyncio.sleep(0)

        asyncio.run(prepare())
        scheduler._prepare_item.assert_awaited_once()

    def test_empty_library_does_not_create_zero_length_block(self) -> None:
        """An asynchronous library load needs the planning loop to yield."""
        scheduler = AdvancedProgramScheduler.__new__(AdvancedProgramScheduler)
        scheduler.music_library = SimpleNamespace(get_all_tracks=lambda: [])
        scheduler.recent_tracker = self.tracker
        scheduler._determine_block_length = lambda *args, **kwargs: 1
        scheduler._get_weights_for_dj = lambda *args: {"silent_block": 1}
        scheduler._weighted_choice = lambda weights: next(iter(weights))
        scheduler._determine_crossover_behavior = lambda *args: "no_crossover"
        scheduler.radio_station = SimpleNamespace(get_listener_count=lambda: 0)
        self.assertIsNone(scheduler._create_song_block("dj", 3600))

    def test_shorter_audio_cannot_pull_handover_before_boundary(self) -> None:
        """Prepared durations must respect the handover's hard time floor."""
        scheduler, timeline = self.make_timeline(0)
        boundary = timeline.show_end
        scheduler.extend_timeline(timeline)
        timeline.items[0].estimated_duration = 10
        asyncio.run(scheduler._recalculate_timeline_times(timeline, timeline.items[0]))
        self.assertGreaterEqual(timeline.items[1].scheduled_start, boundary)

    def make_station(self, timeline: Timeline) -> RadioStation:
        """Build a station around an isolated timeline and mock audio output."""
        station = RadioStation.__new__(RadioStation)
        station.timeline_scheduler = SimpleNamespace(current_timeline=timeline)
        station.program_scheduler = SimpleNamespace(recent_tracker=self.tracker)
        station.config_manager = SimpleNamespace(
            timezone=pytz.UTC, get_dj_config=lambda _: SimpleNamespace(name="DJ")
        )
        station.video_stream = None
        station.cached_listener_count = 0
        station._active_timeline_queue_id = None
        station.continuous_audio = SimpleNamespace(
            queue_audio_file=AsyncMock(), queue_audio_files=AsyncMock()
        )
        return station

    def test_handover_plays_even_with_no_listeners(self) -> None:
        """A scheduled goodbye cannot be cancelled by a listener-count change."""
        _, timeline = self.make_timeline(0)
        station = self.make_station(timeline)
        item = timeline.items[0]
        item.item_type = "dj_transition_end"
        item.content = DJTalkSegment(
            "end",
            "show_transitions",
            "show_end",
            "Goodbye",
            audio_file="goodbye.mp3",
            duration=10,
        )
        asyncio.run(station._handle_transition_end_item(item))
        station.continuous_audio.queue_audio_file.assert_awaited_once()
        self.assertNotEqual(item.status, "completed")

    def test_actual_song_metadata_records_once_and_not_dj_speech(self) -> None:
        """Mixed-song metadata must count each song once, after it goes on air."""
        _, timeline = self.make_timeline(0)
        station = self.make_station(timeline)
        item = timeline.items[0]
        item.content = SimpleNamespace(songs=self.songs[:2], block_id="block")
        metadata = {
            "timeline_id": item.timeline_id,
            "content_type": "MUSIC",
            "artist": "artist",
            "title": "0",
        }
        asyncio.run(station._handle_metadata_update(metadata))
        asyncio.run(station._handle_metadata_update(metadata))
        asyncio.run(
            station._handle_metadata_update(
                {**metadata, "content_type": "DJ_TALK", "title": "1"}
            )
        )
        self.assertTrue(self.tracker.is_recent("artist", "0", "outgoing"))
        self.assertFalse(self.tracker.is_recent("artist", "1", "outgoing"))
        self.assertEqual(len(self.tracker.plays), 1)

    def test_compound_item_finishes_after_last_audio_part(self) -> None:
        """The intro finishing cannot complete a block or release its songs."""
        _, timeline = self.make_timeline(0)
        station = self.make_station(timeline)
        item = timeline.items[0]
        item.status = "playing"
        item.actual_start = self.now
        item.remaining_audio_parts = 2
        item.content = SimpleNamespace(block_id="block")
        self.tracker.reserve_songs("block", self.songs[:1])
        station._on_audio_item_finished({"timeline_id": item.timeline_id}, 10)
        self.assertEqual(item.status, "playing")
        self.assertEqual(self.tracker.get_available_songs(self.songs[:1], "dj"), [])
        station._on_audio_item_finished({"timeline_id": item.timeline_id}, 180)
        self.assertEqual(item.status, "completed")
        self.assertTrue(self.tracker.get_available_songs(self.songs[:1], "dj"))

    def test_audio_batch_failure_does_not_queue_partial_item(self) -> None:
        """A missing second file must leave the entire item available for retry."""
        producer = ContinuousAudioProducer.__new__(ContinuousAudioProducer)
        producer.is_streaming = True
        producer.audio_queue = asyncio.Queue()
        producer._get_audio_duration = AsyncMock(return_value=10)
        valid = Path(self.directory.name) / "valid.mp3"
        valid.touch()
        with self.assertRaises(FileNotFoundError):
            asyncio.run(
                producer.queue_audio_files(
                    [
                        {"file_path": str(valid), "metadata": {}},
                        {
                            "file_path": str(valid.with_name("missing.mp3")),
                            "metadata": {},
                        },
                    ]
                )
            )
        self.assertTrue(producer.audio_queue.empty())

    def test_producer_retries_failed_audio_before_next_item(self) -> None:
        """A streaming error retries the same queued file instead of losing it."""
        producer = ContinuousAudioProducer.__new__(ContinuousAudioProducer)
        producer.is_streaming = True
        producer.is_playing_audio = False
        producer.audio_queue = asyncio.Queue()
        producer.playback_finished_event = asyncio.Event()
        producer._mark_queue_activity = lambda: None
        producer._cancel_active_metadata_tasks = AsyncMock()
        producer._update_metadata = AsyncMock()
        producer._schedule_metadata_updates = AsyncMock()
        producer.on_item_finished = None
        path = Path(self.directory.name) / "audio.mp3"
        path.touch()
        calls = []

        async def stream(file_path: str, duration: float) -> None:
            """Fail the first attempt, then stop after the retry succeeds."""
            calls.append((file_path, duration))
            if len(calls) == 1:
                raise OSError("temporary pipe failure")
            producer.is_streaming = False

        producer._stream_file_as_pcm = stream
        producer.audio_queue.put_nowait(
            {"file_path": str(path), "metadata": {}, "duration": 10}
        )

        async def run() -> None:
            """Bound the test so a discarded item fails without hanging."""
            await asyncio.wait_for(producer._process_audio_queue(), timeout=3)

        asyncio.run(run())
        self.assertEqual(calls, [(str(path), 10), (str(path), 10)])
        self.assertEqual(producer.audio_queue._unfinished_tasks, 0)

    def test_producer_waits_for_handover_boundary(self) -> None:
        """Queueing ahead must not make a handover audible before its boundary."""
        producer = ContinuousAudioProducer.__new__(ContinuousAudioProducer)
        producer.is_streaming = True
        producer._generate_silence_chunk = AsyncMock()
        producer._mark_queue_activity = lambda: None
        boundary = datetime.now(UTC) + timedelta(milliseconds=30)
        asyncio.run(
            producer._wait_for_audio_boundary({"not_before": boundary.isoformat()})
        )
        self.assertGreaterEqual(datetime.now(UTC), boundary)

    def test_scheduled_stings_are_never_deleted(self) -> None:
        """Adjacent jingles already in the timeline must both remain scheduled."""
        scheduler, timeline = self.make_timeline(0)
        for index in range(2):
            timeline.items.append(
                TimelineItem(
                    str(index),
                    "jingle",
                    JingleSegment(str(index), "sting_x", "x.mp3", "X", 10),
                    timeline.show_end + timedelta(seconds=index * 10),
                    10,
                )
            )
        original = list(timeline.items)
        scheduler._insert_stings = TimelineScheduler._insert_stings.__get__(scheduler)
        scheduler._insert_stings(timeline)
        self.assertEqual(timeline.items, original)

    def test_scheduled_speech_generation_ignores_listener_drop(self) -> None:
        """A zero listener count cannot cancel an already scheduled speech item."""
        scheduler = AdvancedProgramScheduler.__new__(AdvancedProgramScheduler)
        generator = AsyncMock(
            return_value=SimpleNamespace(success=False, error="retry")
        )
        scheduler.radio_station = SimpleNamespace(get_listener_count=lambda: 0)
        scheduler.config_manager = SimpleNamespace(
            get_dj_config=lambda _: SimpleNamespace()
        )
        scheduler.dj_ai = SimpleNamespace(
            unified_generator=SimpleNamespace(generate_dj_talk=generator)
        )
        talk = DJTalkSegment("talk", "show_transitions", "show_start", "")
        segment = ScheduleSegment(
            "talk", "dj_transition_start", talk, self.now, 10, "incoming"
        )
        asyncio.run(scheduler.prepare_audio_for_segment(segment))
        generator.assert_awaited_once()

    def test_overlay_switches_on_air_not_when_handover_is_queued(self) -> None:
        """The visible DJ must stay with audible content during queue-ahead."""
        _, timeline = self.make_timeline(0)
        station = self.make_station(timeline)
        station.video_stream = SimpleNamespace(
            update_dj=AsyncMock(), handle_metadata_update=AsyncMock()
        )
        item = timeline.items[0]
        item.item_type = "dj_transition_start"
        item.content = DJTalkSegment(
            "start",
            "show_transitions",
            "show_start",
            "Hello",
            audio_file="intro.mp3",
            duration=10,
        )
        asyncio.run(station._play_timeline_item(item))
        station.video_stream.update_dj.assert_not_awaited()
        asyncio.run(
            station._handle_metadata_update(
                {"timeline_id": item.timeline_id, "content_type": "DJ_TALK"}
            )
        )
        station.video_stream.update_dj.assert_awaited_once_with("outgoing")

    def test_missing_speech_file_cannot_be_silently_omitted(self) -> None:
        """A missing outro must fail the whole staged item for retry."""
        _, timeline = self.make_timeline(0)
        station = self.make_station(timeline)
        with self.assertRaises(FileNotFoundError):
            asyncio.run(
                station._play_dj_audio(str(Path(self.directory.name) / "missing.mp3"))
            )

    def test_cleanup_protects_pending_and_playing_audio(self) -> None:
        """Late scheduled audio must not be deleted by the hourly cleanup."""
        _, timeline = self.make_timeline(0)
        station = self.make_station(timeline)
        timeline.items[0].content = SimpleNamespace(
            prepared_mix_file="/app/temp-audio/mix.mp3",
            intro_audio_files=["/app/temp-audio/intro.mp3"],
        )
        producer = ContinuousAudioProducer.__new__(ContinuousAudioProducer)
        producer.audio_queue = asyncio.Queue()
        producer.audio_queue.put_nowait({"file_path": "/app/temp-audio/queued.mp3"})
        producer._pending_audio_item = {"file_path": "/app/temp-audio/playing.mp3"}
        producer.current_file_path = "/app/temp-audio/playing.mp3"
        station.continuous_audio = producer
        self.assertEqual(
            station._protected_audio_names(),
            {"mix.mp3", "intro.mp3", "queued.mp3", "playing.mp3"},
        )

    def test_zero_duration_group_does_not_spin_the_extension_loop(self) -> None:
        """Keep a scheduled group for preparation without looping on zero time."""
        scheduler, timeline = self.make_timeline(-240)
        calls = []

        def next_segment() -> list[ScheduleSegment]:
            """Fail promptly if a second zero-duration planning pass occurs."""
            calls.append(1)
            if len(calls) > 1:
                raise AssertionError("Repeated planning without time advancing")
            return [ScheduleSegment("zero", "dj_talk", None, self.now, 0, "outgoing")]

        scheduler.advanced_scheduler.get_next_segment = next_segment
        scheduler.extend_timeline(timeline)
        self.assertEqual(len(timeline.items), 2)
        self.assertEqual(len(calls), 1)

    def test_deleted_cached_speech_is_regenerated(self) -> None:
        """Retry must regenerate missing cached speech instead of trusting its path."""
        scheduler = AdvancedProgramScheduler.__new__(AdvancedProgramScheduler)
        generator = AsyncMock(
            return_value=SimpleNamespace(success=False, error="retry")
        )
        scheduler.config_manager = SimpleNamespace(
            get_dj_config=lambda _: SimpleNamespace()
        )
        scheduler.dj_ai = SimpleNamespace(
            unified_generator=SimpleNamespace(generate_dj_talk=generator)
        )
        talk = DJTalkSegment(
            "talk",
            "show_transitions",
            "show_start",
            "",
            audio_file=str(Path(self.directory.name) / "deleted.mp3"),
        )
        segment = ScheduleSegment(
            "talk", "dj_transition_start", talk, self.now, 10, "incoming"
        )
        asyncio.run(scheduler.prepare_audio_for_segment(segment))
        generator.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
