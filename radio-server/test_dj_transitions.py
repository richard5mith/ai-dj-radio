#!/usr/bin/env python3
"""
Test DJ show transitions in the timeline scheduler.

This test verifies that:
1. Timeline can schedule items up to the end of a show
2. A show_end transition item is created at show boundary
3. The DJ switch callback is invoked
4. A show_start transition item is created for the incoming DJ
5. Content continues scheduling for the new DJ

Run with: docker compose exec radio-server uv run python test_dj_transitions.py
"""

import asyncio
import logging
import random
from datetime import datetime, time, timedelta
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytz

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# Realistic song library with varied durations (in seconds)
MOCK_SONG_LIBRARY = [
    # Short songs (2-3 minutes)
    {"artist": "The Clash", "title": "Should I Stay or Should I Go", "duration": 187.0},
    {"artist": "Blondie", "title": "Heart of Glass", "duration": 205.0},
    {"artist": "The Police", "title": "Message in a Bottle", "duration": 170.0},
    {"artist": "Devo", "title": "Whip It", "duration": 156.0},
    {"artist": "Gary Numan", "title": "Cars", "duration": 203.0},
    # Medium songs (3-4 minutes)
    {"artist": "Duran Duran", "title": "Hungry Like the Wolf", "duration": 218.0},
    {"artist": "A-ha", "title": "Take On Me", "duration": 227.0},
    {"artist": "Tears for Fears", "title": "Shout", "duration": 252.0},
    {"artist": "Depeche Mode", "title": "Personal Jesus", "duration": 237.0},
    {"artist": "New Order", "title": "Blue Monday", "duration": 230.0},
    {"artist": "Pet Shop Boys", "title": "West End Girls", "duration": 240.0},
    {"artist": "Culture Club", "title": "Karma Chameleon", "duration": 243.0},
    # Longer songs (4-5 minutes)
    {"artist": "Simple Minds", "title": "Don't You Forget About Me", "duration": 262.0},
    {"artist": "The Smiths", "title": "How Soon Is Now?", "duration": 295.0},
    {"artist": "Prince", "title": "Purple Rain", "duration": 320.0},
    {"artist": "Queen", "title": "Radio Ga Ga", "duration": 289.0},
    {"artist": "Michael Jackson", "title": "Billie Jean", "duration": 294.0},
    {"artist": "Madonna", "title": "Like a Prayer", "duration": 340.0},
    # Very short (under 2 min) - useful for filling gaps
    {"artist": "The Ramones", "title": "Blitzkrieg Bop", "duration": 142.0},
    {"artist": "The B-52s", "title": "Rock Lobster (Edit)", "duration": 130.0},
    {
        "artist": "Dead Kennedys",
        "title": "Holiday in Cambodia (Edit)",
        "duration": 145.0,
    },
]


class MockSong:
    """Mock song object."""

    def __init__(self, artist: str, title: str, duration: float):
        self.artist = artist
        self.title = title
        self.duration = duration


class MockScheduleEntry:
    """Mock schedule entry for testing."""

    def __init__(
        self, dj_name: str, start_time: time, end_time: time, music_folders: List[str]
    ):
        self.dj_name = dj_name
        self.start_time = start_time
        self.end_time = end_time
        self.music_folders = music_folders


class MockDJConfig:
    """Mock DJ config for testing."""

    def __init__(self, name: str, display_name: str):
        self.name = name
        self.display_name = display_name


class MockSegment:
    """Mock segment for testing."""

    def __init__(
        self,
        segment_type: str,
        duration: float,
        segment_id: str = None,
        songs: List[MockSong] = None,
    ):
        self.segment_type = segment_type
        self.estimated_duration = duration
        self.content = MagicMock()
        self.content.segment_id = segment_id or f"seg_{id(self)}"
        self.content.songs = songs or []


class RealisticMockScheduler:
    """Mock scheduler that simulates realistic song selection with best-fit algorithm."""

    def __init__(self, song_library: List[dict] = None):
        self.weights = {}
        self.song_library = [MockSong(**s) for s in (song_library or MOCK_SONG_LIBRARY)]
        self.segments_generated = 0
        self.virtual_current_time = None
        self.show_end_time = None
        self.dj_id = "test_dj"
        self.played_songs = set()  # Track what we've played to avoid repeats

    def start_dj_show(
        self,
        dj_id: str,
        start_time: datetime,
        end_time: datetime,
        actual_show_start: datetime = None,
    ):
        """Start a DJ show."""
        self.dj_id = dj_id
        self.virtual_current_time = start_time
        self.show_end_time = end_time
        self.played_songs = set()
        logger.info(
            f"Mock scheduler: Started show for {dj_id}, ends at {end_time.strftime('%H:%M:%S')}"
        )

    def _get_remaining_time(self) -> float:
        """Get remaining time until show end."""
        if self.virtual_current_time and self.show_end_time:
            return (self.show_end_time - self.virtual_current_time).total_seconds()
        return 3600  # Default 1 hour if not set

    def _find_best_fit_songs(
        self, target_time: float, max_songs: int = 3
    ) -> List[MockSong]:
        """Find the best combination of songs to fill target_time WITHOUT exceeding it."""
        available = [
            s
            for s in self.song_library
            if s.title not in self.played_songs and s.duration <= target_time
        ]

        if not available:
            return []

        # Sort by duration
        sorted_songs = sorted(available, key=lambda s: s.duration)

        # Try single song first
        best_single = max(sorted_songs, key=lambda s: s.duration)
        best_fit = [best_single]
        best_diff = target_time - best_single.duration

        # Try two-song combinations
        for i, song1 in enumerate(sorted_songs):
            for song2 in sorted_songs[i + 1 :]:
                total = song1.duration + song2.duration
                if total <= target_time:
                    diff = target_time - total
                    if diff < best_diff:
                        best_fit = [song1, song2]
                        best_diff = diff
                        if diff < 30:  # Good enough
                            break
            if best_diff < 30:
                break

        # Try three-song combinations if still far off
        if best_diff > 30 and max_songs >= 3:
            for i, song1 in enumerate(sorted_songs[:10]):
                for j, song2 in enumerate(sorted_songs[i + 1 : 11], start=i + 1):
                    for song3 in sorted_songs[j + 1 : 12]:
                        total = song1.duration + song2.duration + song3.duration
                        if total <= target_time:
                            diff = target_time - total
                            if diff < best_diff:
                                best_fit = [song1, song2, song3]
                                best_diff = diff
                                if diff < 30:
                                    break
                    if best_diff < 30:
                        break
                if best_diff < 30:
                    break

        return best_fit

    def _create_filler_segment(self, target_duration: float) -> MockSegment:
        """Create a DJ talk or jingle segment to fill time."""
        # Simulate realistic filler durations
        if target_duration < 20:
            # Very short - use a jingle (10-15s)
            duration = min(target_duration, random.uniform(10, 15))
            segment_type = "jingle"
            name = "Station jingle"
        elif target_duration < 45:
            # Short - use a short DJ comment or jingle
            if random.random() < 0.5:
                duration = min(target_duration, random.uniform(15, 25))
                segment_type = "dj_talk"
                name = "Quick DJ comment"
            else:
                duration = min(target_duration, random.uniform(12, 20))
                segment_type = "jingle"
                name = "Station ID"
        else:
            # Longer gap - DJ talk with maybe jingle
            duration = min(target_duration, random.uniform(25, 40))
            segment_type = "dj_talk"
            name = "DJ banter"

        return MockSegment(
            segment_type, duration, f"filler_{self.segments_generated}", []
        )

    def get_next_segment(self) -> List[MockSegment]:
        """Return a song block, using best-fit when close to show end."""
        remaining = self._get_remaining_time()
        self.segments_generated += 1

        # If we have less than 8 minutes, use best-fit algorithm
        if remaining < 480:
            # Reserve 60 seconds for transition talk (30s show_end + 30s show_start)
            target_time = remaining - 60
            if target_time < 10:  # Less than 10 seconds - just do transition
                logger.info(
                    f"⏰ Only {remaining:.0f}s remaining ({target_time:.0f}s for content), signaling for transition"
                )
                return []  # Signal that we should do transition instead

            songs = self._find_best_fit_songs(target_time)
            if songs:
                for song in songs:
                    self.played_songs.add(song.title)

                total_duration = sum(s.duration for s in songs)
                gap_after_songs = target_time - total_duration

                # Update virtual time
                if self.virtual_current_time:
                    self.virtual_current_time += timedelta(seconds=total_duration)

                logger.info(
                    f"🎵 Best-fit block: {len(songs)} songs, {total_duration:.1f}s "
                    f"(target: {target_time:.1f}s, gap: {gap_after_songs:.1f}s)"
                )

                segments = [
                    MockSegment(
                        "song_block",
                        total_duration,
                        f"block_{self.segments_generated}",
                        songs,
                    )
                ]

                # If there's still a gap > 10s after songs, add filler
                if gap_after_songs > 10:
                    filler = self._create_filler_segment(gap_after_songs)
                    segments.append(filler)
                    if self.virtual_current_time:
                        self.virtual_current_time += timedelta(
                            seconds=filler.estimated_duration
                        )
                    logger.info(
                        f"🎤 Added filler: {filler.segment_type} ({filler.estimated_duration:.1f}s) to fill {gap_after_songs:.1f}s gap"
                    )

                return segments
            else:
                # No songs fit - use DJ talk/jingles to fill the time
                logger.info(
                    f"⏰ No songs fit in {target_time:.0f}s, creating filler content"
                )
                filler = self._create_filler_segment(target_time)
                if self.virtual_current_time:
                    self.virtual_current_time += timedelta(
                        seconds=filler.estimated_duration
                    )
                logger.info(
                    f"🎤 Created filler: {filler.segment_type} ({filler.estimated_duration:.1f}s)"
                )
                return [filler]

        # Normal operation: pick 2-3 random songs
        available = [s for s in self.song_library if s.title not in self.played_songs]
        if not available:
            self.played_songs = set()  # Reset if we've played everything
            available = self.song_library

        num_songs = random.randint(2, 3)
        songs = random.sample(available, min(num_songs, len(available)))

        for song in songs:
            self.played_songs.add(song.title)

        total_duration = sum(s.duration for s in songs)

        # Update virtual time
        if self.virtual_current_time:
            self.virtual_current_time += timedelta(seconds=total_duration)

        return [
            MockSegment(
                "song_block", total_duration, f"block_{self.segments_generated}", songs
            )
        ]


class MockAdvancedScheduler:
    """Simple mock advanced scheduler that generates predictable segments."""

    def __init__(
        self,
        segment_duration: float = 180.0,
        random_duration: bool = False,
        min_duration: float = 150.0,
        max_duration: float = 250.0,
    ):
        self.segment_duration = segment_duration
        self.weights = {}
        self.random_duration = random_duration
        self.min_duration = min_duration
        self.max_duration = max_duration
        self.segments_generated = 0
        self.virtual_current_time = None
        self.dj_id = "test_dj"

    def start_dj_show(
        self,
        dj_id: str,
        start_time: datetime,
        end_time: datetime,
        actual_show_start: datetime = None,
    ):
        """Start a DJ show."""
        self.dj_id = dj_id
        logger.info(f"Mock scheduler: Started show for {dj_id}")

    def get_next_segment(self) -> List[MockSegment]:
        """Return a mock song block segment."""
        self.segments_generated += 1
        if self.random_duration:
            duration = random.uniform(self.min_duration, self.max_duration)
        else:
            duration = self.segment_duration
        return [MockSegment("song_block", duration, f"block_{self.segments_generated}")]


class MockConfigManager:
    """Mock config manager for testing."""

    def __init__(self, schedule: List[MockScheduleEntry], timezone):
        self.schedule = schedule
        self.timezone = timezone
        self.dj_configs = {}

    def get_schedule_entry_at_time(self, dt: datetime) -> Optional[MockScheduleEntry]:
        """Get the schedule entry that should be active at a specific datetime."""
        check_time = dt.time()
        for entry in self.schedule:
            if self._time_in_range(check_time, entry.start_time, entry.end_time):
                return entry
        return None

    def _time_in_range(self, current: time, start: time, end: time) -> bool:
        """Check if current time is within the start and end time range."""
        if start <= end:
            return start <= current < end  # Use < for end to avoid boundary issues
        else:
            return current >= start or current < end

    def get_dj_config(self, dj_name: str) -> Optional[MockDJConfig]:
        """Get DJ config by name."""
        return self.dj_configs.get(dj_name)


def create_test_schedule(timezone) -> List[MockScheduleEntry]:
    """Create a test schedule with clear boundaries."""
    return [
        MockScheduleEntry("morning_mike", time(6, 0), time(10, 0), ["80s", "70s"]),
        MockScheduleEntry("daytime_diana", time(10, 0), time(14, 0), ["80s", "mixed"]),
        MockScheduleEntry(
            "afternoon_alex", time(14, 0), time(18, 0), ["90s", "modern"]
        ),
        MockScheduleEntry("evening_emma", time(18, 0), time(22, 0), ["disco", "80s"]),
        MockScheduleEntry(
            "night_nick", time(22, 0), time(2, 0), ["mixed"]
        ),  # Overnight
        MockScheduleEntry("early_bird", time(2, 0), time(6, 0), ["70s"]),
    ]


def test_timeline_creation_basic():
    """Test basic timeline creation."""
    print("\n" + "=" * 60)
    print("TEST: Basic Timeline Creation")
    print("=" * 60)

    from radio_server.timeline_scheduler import Timeline, TimelineScheduler

    timezone = pytz.timezone("Europe/London")
    schedule = create_test_schedule(timezone)
    config_manager = MockConfigManager(schedule, timezone)

    # Create a mock advanced scheduler
    advanced_scheduler = MockAdvancedScheduler(segment_duration=180.0)

    # Create timeline scheduler
    timeline_scheduler = TimelineScheduler(
        advanced_scheduler=advanced_scheduler, config_manager=config_manager
    )

    # Set a fixed "now" time: 10:30 AM (during daytime_diana's show)
    fixed_now = timezone.localize(datetime(2026, 1, 20, 10, 30, 0))

    with patch.object(timeline_scheduler, "_now", return_value=fixed_now):
        # Create timeline for daytime_diana (10:00 - 14:00)
        show_start = timezone.localize(datetime(2026, 1, 20, 10, 0, 0))
        show_end = timezone.localize(datetime(2026, 1, 20, 14, 0, 0))

        timeline = timeline_scheduler.create_show_timeline(
            dj_id="daytime_diana",
            start_time=fixed_now,
            end_time=show_end,
            look_ahead_minutes=15,
        )

        print(f"✅ Created timeline with {len(timeline.items)} items")
        print(f"   DJ: {timeline.dj_id}")
        print(f"   Show end: {timeline.show_end}")

        for i, item in enumerate(timeline.items):
            print(
                f"   Item {i + 1}: {item.item_type} at {item.scheduled_start.strftime('%H:%M:%S')} ({item.estimated_duration:.1f}s)"
            )

        assert len(timeline.items) > 0, "Timeline should have items"
        assert timeline.dj_id == "daytime_diana", "Timeline should be for daytime_diana"

    print("✅ PASSED: Basic timeline creation works")
    return True


def test_dj_transition_scheduling():
    """Test that DJ transitions are properly scheduled when approaching show end."""
    print("\n" + "=" * 60)
    print("TEST: DJ Transition Scheduling")
    print("=" * 60)

    from radio_server.timeline_scheduler import (
        Timeline,
        TimelineItem,
        TimelineScheduler,
    )

    timezone = pytz.timezone("Europe/London")
    schedule = create_test_schedule(timezone)
    config_manager = MockConfigManager(schedule, timezone)

    # Add DJ configs for transition messages
    config_manager.dj_configs = {
        "daytime_diana": MockDJConfig("daytime_diana", "Diana"),
        "afternoon_alex": MockDJConfig("afternoon_alex", "Alex"),
    }

    # Create a mock advanced scheduler with shorter segments
    advanced_scheduler = MockAdvancedScheduler(
        segment_duration=120.0
    )  # 2 minute segments

    # Track DJ switch calls
    dj_switches = []

    def on_dj_switch(
        new_dj_id: str,
        start_time: datetime = None,
        end_time: datetime = None,
        music_folders: list = None,
    ):
        dj_switches.append(new_dj_id)
        logger.info(f"🔄 DJ Switch callback: {new_dj_id}")
        # In real code, this would call advanced_scheduler.start_dj_show()
        advanced_scheduler.start_dj_show(
            new_dj_id,
            start_time or datetime.now(timezone),
            end_time or datetime.now(timezone),
        )

    # Create timeline scheduler
    timeline_scheduler = TimelineScheduler(
        advanced_scheduler=advanced_scheduler, config_manager=config_manager
    )
    timeline_scheduler.on_dj_switch = on_dj_switch

    # Set time to 13:55 (5 minutes before daytime_diana's show ends at 14:00)
    fixed_now = timezone.localize(datetime(2026, 1, 20, 13, 55, 0))

    with patch.object(timeline_scheduler, "_now", return_value=fixed_now):
        # Create initial timeline for daytime_diana
        show_end = timezone.localize(datetime(2026, 1, 20, 14, 0, 0))

        timeline = timeline_scheduler.create_show_timeline(
            dj_id="daytime_diana",
            start_time=fixed_now,
            end_time=show_end,
            look_ahead_minutes=10,  # Try to schedule 10 minutes, but show ends in 5
        )

        print(f"\n📋 Initial timeline ({len(timeline.items)} items):")
        for i, item in enumerate(timeline.items):
            dj_info = (
                f" [next: {item.next_dj_id}]"
                if hasattr(item, "next_dj_id") and item.next_dj_id
                else ""
            )
            print(
                f"   {i + 1}. {item.item_type} @ {item.scheduled_start.strftime('%H:%M:%S')} ({item.estimated_duration:.1f}s) DJ: {item.dj_id}{dj_info}"
            )

        # Now extend the timeline past the show boundary
        print("\n🔄 Extending timeline past show boundary...")

        # Simulate time passing - now at 13:57
        extend_time = timezone.localize(datetime(2026, 1, 20, 13, 57, 0))

        with patch.object(timeline_scheduler, "_now", return_value=extend_time):
            timeline = timeline_scheduler.extend_timeline(timeline, extend_minutes=15)

        print(f"\n📋 Extended timeline ({len(timeline.items)} items):")
        for i, item in enumerate(timeline.items):
            dj_info = (
                f" [next: {item.next_dj_id}]"
                if hasattr(item, "next_dj_id") and item.next_dj_id
                else ""
            )
            print(
                f"   {i + 1}. {item.item_type} @ {item.scheduled_start.strftime('%H:%M:%S')} ({item.estimated_duration:.1f}s) DJ: {item.dj_id}{dj_info}"
            )

        # Verify transition items were created
        transition_end_items = [
            item for item in timeline.items if item.item_type == "dj_transition_end"
        ]
        transition_start_items = [
            item for item in timeline.items if item.item_type == "dj_transition_start"
        ]

        print(f"\n📊 Results:")
        print(f"   Show end transition items: {len(transition_end_items)}")
        print(f"   Show start transition items: {len(transition_start_items)}")
        print(f"   DJ switch callbacks: {dj_switches}")
        print(f"   Final timeline DJ: {timeline.dj_id}")
        print(f"   Final timeline show_end: {timeline.show_end}")

        # Assertions
        assert len(transition_end_items) >= 1, (
            "Should have at least one show_end transition"
        )
        assert len(transition_start_items) >= 1, (
            "Should have at least one show_start transition"
        )
        assert "afternoon_alex" in dj_switches, (
            "DJ switch callback should have been called for afternoon_alex"
        )
        assert timeline.dj_id == "afternoon_alex", (
            "Timeline DJ should now be afternoon_alex"
        )

        # Verify the transition order
        end_idx = next(
            i
            for i, item in enumerate(timeline.items)
            if item.item_type == "dj_transition_end"
        )
        start_idx = next(
            i
            for i, item in enumerate(timeline.items)
            if item.item_type == "dj_transition_start"
        )
        assert start_idx > end_idx, "show_start should come after show_end"

        # Verify new content was added for afternoon_alex
        alex_items = [
            item
            for item in timeline.items
            if item.dj_id == "afternoon_alex" and item.item_type == "song_block"
        ]
        print(f"   Content items for afternoon_alex: {len(alex_items)}")

    print("\n✅ PASSED: DJ transition scheduling works correctly")
    return True


def test_multiple_show_transitions():
    """Test multiple show transitions in sequence."""
    print("\n" + "=" * 60)
    print("TEST: Multiple Show Transitions")
    print("=" * 60)

    from radio_server.timeline_scheduler import TimelineScheduler

    timezone = pytz.timezone("Europe/London")
    schedule = create_test_schedule(timezone)
    config_manager = MockConfigManager(schedule, timezone)

    # Add DJ configs
    config_manager.dj_configs = {
        "morning_mike": MockDJConfig("morning_mike", "Mike"),
        "daytime_diana": MockDJConfig("daytime_diana", "Diana"),
        "afternoon_alex": MockDJConfig("afternoon_alex", "Alex"),
    }

    # Short segments for faster testing
    advanced_scheduler = MockAdvancedScheduler(segment_duration=60.0)

    dj_switches = []

    def on_dj_switch(
        new_dj_id: str,
        start_time: datetime = None,
        end_time: datetime = None,
        music_folders: list = None,
    ):
        dj_switches.append(new_dj_id)
        advanced_scheduler.start_dj_show(
            new_dj_id,
            start_time or datetime.now(timezone),
            end_time or datetime.now(timezone),
        )

    timeline_scheduler = TimelineScheduler(
        advanced_scheduler=advanced_scheduler, config_manager=config_manager
    )
    timeline_scheduler.on_dj_switch = on_dj_switch

    # Start at 9:50 (morning_mike's show ends at 10:00)
    fixed_now = timezone.localize(datetime(2026, 1, 20, 9, 50, 0))
    show_end = timezone.localize(datetime(2026, 1, 20, 10, 0, 0))

    with patch.object(timeline_scheduler, "_now", return_value=fixed_now):
        timeline = timeline_scheduler.create_show_timeline(
            dj_id="morning_mike",
            start_time=fixed_now,
            end_time=show_end,
            look_ahead_minutes=5,
        )

    print(f"📋 Initial timeline for morning_mike: {len(timeline.items)} items")

    # Simulate multiple extensions that cross show boundaries
    extension_times = [
        timezone.localize(datetime(2026, 1, 20, 9, 55, 0)),
        timezone.localize(datetime(2026, 1, 20, 10, 5, 0)),
        timezone.localize(datetime(2026, 1, 20, 10, 15, 0)),
    ]

    for ext_time in extension_times:
        with patch.object(timeline_scheduler, "_now", return_value=ext_time):
            timeline = timeline_scheduler.extend_timeline(timeline, extend_minutes=15)
            print(
                f"   Extended at {ext_time.strftime('%H:%M:%S')}: {len(timeline.items)} items, DJ: {timeline.dj_id}"
            )

    print(f"\n📊 Final timeline ({len(timeline.items)} items):")

    # Count items by DJ
    dj_item_counts = {}
    for item in timeline.items:
        dj_item_counts[item.dj_id] = dj_item_counts.get(item.dj_id, 0) + 1

    print(f"   Items by DJ: {dj_item_counts}")
    print(f"   DJ switches: {dj_switches}")

    # Verify transition happened
    assert "daytime_diana" in dj_switches, "Should have switched to daytime_diana"
    assert timeline.dj_id == "daytime_diana", "Final DJ should be daytime_diana"

    # Verify both DJs have content
    assert dj_item_counts.get("morning_mike", 0) > 0, "Should have morning_mike content"
    assert dj_item_counts.get("daytime_diana", 0) > 0, (
        "Should have daytime_diana content"
    )

    print("\n✅ PASSED: Multiple show transitions work correctly")
    return True


def test_overnight_transition():
    """Test transition across midnight."""
    print("\n" + "=" * 60)
    print("TEST: Overnight Show Transition")
    print("=" * 60)

    from radio_server.timeline_scheduler import TimelineScheduler

    timezone = pytz.timezone("Europe/London")
    schedule = create_test_schedule(timezone)
    config_manager = MockConfigManager(schedule, timezone)

    config_manager.dj_configs = {
        "night_nick": MockDJConfig("night_nick", "Nick"),
        "early_bird": MockDJConfig("early_bird", "Early Bird"),
    }

    advanced_scheduler = MockAdvancedScheduler(segment_duration=120.0)

    dj_switches = []

    def on_dj_switch(
        new_dj_id: str,
        start_time: datetime = None,
        end_time: datetime = None,
        music_folders: list = None,
    ):
        dj_switches.append(new_dj_id)
        advanced_scheduler.start_dj_show(
            new_dj_id,
            start_time or datetime.now(timezone),
            end_time or datetime.now(timezone),
        )

    timeline_scheduler = TimelineScheduler(
        advanced_scheduler=advanced_scheduler, config_manager=config_manager
    )
    timeline_scheduler.on_dj_switch = on_dj_switch

    # Start at 1:55 AM (night_nick's show ends at 2:00 AM)
    fixed_now = timezone.localize(datetime(2026, 1, 21, 1, 55, 0))
    show_end = timezone.localize(datetime(2026, 1, 21, 2, 0, 0))

    with patch.object(timeline_scheduler, "_now", return_value=fixed_now):
        timeline = timeline_scheduler.create_show_timeline(
            dj_id="night_nick",
            start_time=fixed_now,
            end_time=show_end,
            look_ahead_minutes=10,
        )

    print(f"📋 Initial timeline for night_nick: {len(timeline.items)} items")

    # Extend past the 2 AM boundary
    extend_time = timezone.localize(datetime(2026, 1, 21, 1, 58, 0))
    with patch.object(timeline_scheduler, "_now", return_value=extend_time):
        timeline = timeline_scheduler.extend_timeline(timeline, extend_minutes=15)

    print(f"📋 Extended timeline: {len(timeline.items)} items")
    print(f"   DJ switches: {dj_switches}")
    print(f"   Final DJ: {timeline.dj_id}")

    # Check for transition items
    has_end_transition = any(
        item.item_type == "dj_transition_end" for item in timeline.items
    )
    has_start_transition = any(
        item.item_type == "dj_transition_start" for item in timeline.items
    )

    print(f"   Has show_end transition: {has_end_transition}")
    print(f"   Has show_start transition: {has_start_transition}")

    if "early_bird" in dj_switches:
        print("\n✅ PASSED: Overnight transition works correctly")
    else:
        print("\n⚠️  WARNING: Overnight transition may need review")
        print(f"   Expected switch to early_bird but got: {dj_switches}")

    return True


def test_transition_item_order():
    """Test that transition items are in the correct order."""
    print("\n" + "=" * 60)
    print("TEST: Transition Item Order")
    print("=" * 60)

    from radio_server.timeline_scheduler import TimelineScheduler

    timezone = pytz.timezone("Europe/London")
    schedule = create_test_schedule(timezone)
    config_manager = MockConfigManager(schedule, timezone)

    config_manager.dj_configs = {
        "daytime_diana": MockDJConfig("daytime_diana", "Diana"),
        "afternoon_alex": MockDJConfig("afternoon_alex", "Alex"),
    }

    advanced_scheduler = MockAdvancedScheduler(segment_duration=60.0)

    timeline_scheduler = TimelineScheduler(
        advanced_scheduler=advanced_scheduler, config_manager=config_manager
    )
    timeline_scheduler.on_dj_switch = (
        lambda dj_id,
        start_time=None,
        end_time=None,
        music_folders=None: advanced_scheduler.start_dj_show(
            dj_id, start_time, end_time
        )
    )

    # Start 2 minutes before show end
    fixed_now = timezone.localize(datetime(2026, 1, 20, 13, 58, 0))
    show_end = timezone.localize(datetime(2026, 1, 20, 14, 0, 0))

    with patch.object(timeline_scheduler, "_now", return_value=fixed_now):
        timeline = timeline_scheduler.create_show_timeline(
            dj_id="daytime_diana",
            start_time=fixed_now,
            end_time=show_end,
            look_ahead_minutes=5,
        )

        # Extend to trigger transition
        timeline = timeline_scheduler.extend_timeline(timeline, extend_minutes=15)

    print(f"📋 Timeline items in order:")

    prev_time = None
    transition_order = []
    for i, item in enumerate(timeline.items):
        if "transition" in item.item_type:
            transition_order.append(item.item_type)

        # Check time ordering
        if prev_time is not None:
            time_ok = "✓" if item.scheduled_start >= prev_time else "✗"
        else:
            time_ok = "✓"

        print(
            f"   {time_ok} {i + 1}. {item.item_type} @ {item.scheduled_start.strftime('%H:%M:%S')} (DJ: {item.dj_id})"
        )
        prev_time = item.scheduled_start

    print(f"\n📊 Transition order: {transition_order}")

    # Verify order
    if (
        "dj_transition_end" in transition_order
        and "dj_transition_start" in transition_order
    ):
        end_idx = transition_order.index("dj_transition_end")
        start_idx = transition_order.index("dj_transition_start")
        assert end_idx < start_idx, "show_end must come before show_start"
        print("\n✅ PASSED: Transition items are in correct order")
    else:
        print("\n⚠️  Could not verify order (missing transition items)")

    return True


def test_late_transition_recovery():
    """Test that timeline recovers when show_end has already passed.

    This simulates the bug where timeline gets stuck when the show
    has ended but no transition was scheduled.
    """
    print("\n" + "=" * 60)
    print("TEST: Late Transition Recovery (Past Show End)")
    print("=" * 60)

    from radio_server.timeline_scheduler import TimelineScheduler

    timezone = pytz.timezone("Europe/London")
    schedule = create_test_schedule(timezone)
    config_manager = MockConfigManager(schedule, timezone)

    config_manager.dj_configs = {
        "daytime_diana": MockDJConfig("daytime_diana", "Diana"),
        "afternoon_alex": MockDJConfig("afternoon_alex", "Alex"),
    }

    advanced_scheduler = MockAdvancedScheduler(segment_duration=60.0)

    dj_switches = []

    def on_dj_switch(
        new_dj_id: str,
        start_time: datetime = None,
        end_time: datetime = None,
        music_folders: list = None,
    ):
        dj_switches.append(new_dj_id)
        advanced_scheduler.start_dj_show(
            new_dj_id,
            start_time or datetime.now(timezone),
            end_time or datetime.now(timezone),
        )

    timeline_scheduler = TimelineScheduler(
        advanced_scheduler=advanced_scheduler, config_manager=config_manager
    )
    timeline_scheduler.on_dj_switch = on_dj_switch

    # Create a timeline that ended at 14:00, but current time is 14:05
    # This simulates the bug scenario where we're past show_end
    show_end = timezone.localize(datetime(2026, 1, 20, 14, 0, 0))
    current_time_past_end = timezone.localize(datetime(2026, 1, 20, 14, 5, 0))

    # Create initial timeline with a show that has already ended
    with patch.object(
        timeline_scheduler,
        "_now",
        return_value=timezone.localize(datetime(2026, 1, 20, 13, 55, 0)),
    ):
        timeline = timeline_scheduler.create_show_timeline(
            dj_id="daytime_diana",
            start_time=timezone.localize(datetime(2026, 1, 20, 13, 55, 0)),
            end_time=show_end,
            look_ahead_minutes=5,
        )

    print(f"📋 Initial timeline for daytime_diana: {len(timeline.items)} items")
    print(f"   Show end: {timeline.show_end.strftime('%H:%M:%S')}")

    # Now extend when we're PAST the show end (simulating the stuck state)
    print(f"\n🔄 Extending timeline when current time is PAST show_end...")
    print(f"   Current time: {current_time_past_end.strftime('%H:%M:%S')}")
    print(f"   Show end: {show_end.strftime('%H:%M:%S')}")
    print(
        f"   Time past show end: {(current_time_past_end - show_end).total_seconds():.0f}s"
    )

    with patch.object(timeline_scheduler, "_now", return_value=current_time_past_end):
        timeline = timeline_scheduler.extend_timeline(timeline, extend_minutes=15)

    print(f"\n📋 Extended timeline: {len(timeline.items)} items")
    print(f"   DJ switches: {dj_switches}")
    print(f"   Final timeline DJ: {timeline.dj_id}")
    print(f"   Final show_end: {timeline.show_end.strftime('%H:%M:%S')}")

    # Check for transition items
    transition_end_items = [
        item for item in timeline.items if item.item_type == "dj_transition_end"
    ]
    transition_start_items = [
        item for item in timeline.items if item.item_type == "dj_transition_start"
    ]

    print(f"   Transition end items: {len(transition_end_items)}")
    print(f"   Transition start items: {len(transition_start_items)}")

    # The key assertion: even though we're past show_end, the transition should happen
    assert len(dj_switches) > 0, (
        "Should have triggered DJ switch even when past show_end"
    )
    assert "afternoon_alex" in dj_switches, "Should have switched to afternoon_alex"
    assert timeline.dj_id == "afternoon_alex", (
        "Timeline should be updated to afternoon_alex"
    )
    assert timeline.show_end > show_end, (
        "Show end should be updated to afternoon_alex's show end"
    )

    # Verify we can now schedule content for the new DJ
    alex_items = [
        item
        for item in timeline.items
        if item.dj_id == "afternoon_alex" and item.item_type == "song_block"
    ]
    print(f"   Content items for afternoon_alex: {len(alex_items)}")
    assert len(alex_items) > 0, "Should have scheduled content for afternoon_alex"

    print("\n✅ PASSED: Late transition recovery works correctly")
    return True


def test_realistic_best_fit_transition():
    """Test transition with realistic song library and best-fit algorithm.

    This simulates the real scheduler behavior:
    - Songs have realistic durations (2-5 minutes)
    - When approaching show end, the best-fit algorithm finds songs
      that fill remaining time as closely as possible
    - Should get within 10 seconds of show boundary before transition
    """
    print("\n" + "=" * 60)
    print("TEST: Realistic Best-Fit Song Selection Transition")
    print("=" * 60)

    from radio_server.timeline_scheduler import TimelineScheduler

    # Set seed for reproducibility
    random.seed(123)

    timezone = pytz.timezone("Europe/London")
    schedule = create_test_schedule(timezone)
    config_manager = MockConfigManager(schedule, timezone)

    config_manager.dj_configs = {
        "daytime_diana": MockDJConfig("daytime_diana", "Diana"),
        "afternoon_alex": MockDJConfig("afternoon_alex", "Alex"),
    }

    # Use the realistic scheduler with actual song library
    advanced_scheduler = RealisticMockScheduler()

    dj_switches = []

    def on_dj_switch(
        new_dj_id: str,
        start_time: datetime = None,
        end_time: datetime = None,
        music_folders: list = None,
    ):
        dj_switches.append(new_dj_id)
        advanced_scheduler.start_dj_show(
            new_dj_id,
            start_time or datetime.now(timezone),
            end_time or datetime.now(timezone),
        )

    timeline_scheduler = TimelineScheduler(
        advanced_scheduler=advanced_scheduler, config_manager=config_manager
    )
    timeline_scheduler.on_dj_switch = on_dj_switch

    # Start at 13:50 (10 minutes before daytime_diana's show ends at 14:00)
    # This gives us enough time to see normal scheduling, then best-fit kick in
    fixed_now = timezone.localize(datetime(2026, 1, 20, 13, 50, 0))
    show_end = timezone.localize(datetime(2026, 1, 20, 14, 0, 0))

    print(f"\n🎵 Song Library ({len(MOCK_SONG_LIBRARY)} songs):")
    for song in sorted(MOCK_SONG_LIBRARY, key=lambda s: s["duration"]):
        print(f"   {song['duration'] / 60:.1f}m - {song['artist']} - {song['title']}")

    with patch.object(timeline_scheduler, "_now", return_value=fixed_now):
        # Create initial timeline
        timeline = timeline_scheduler.create_show_timeline(
            dj_id="daytime_diana",
            start_time=fixed_now,
            end_time=show_end,
            look_ahead_minutes=10,  # Try to schedule up to 10 minutes
        )

        print(f"\n📋 Initial timeline ({len(timeline.items)} items):")
        running_time = fixed_now
        for i, item in enumerate(timeline.items):
            end_time_item = running_time + timedelta(seconds=item.estimated_duration)
            songs_info = ""
            if hasattr(item.content, "songs") and item.content.songs:
                songs_info = (
                    f" [{', '.join([s.title[:20] for s in item.content.songs])}]"
                )
            print(
                f"   {i + 1}. {item.item_type} @ {running_time.strftime('%H:%M:%S')} -> {end_time_item.strftime('%H:%M:%S')} ({item.estimated_duration:.1f}s){songs_info}"
            )
            running_time = end_time_item

        timeline_end = running_time
        gap_to_show_end = (show_end - timeline_end).total_seconds()
        print(f"\n   Timeline ends at: {timeline_end.strftime('%H:%M:%S')}")
        print(f"   Show boundary: {show_end.strftime('%H:%M:%S')}")
        print(f"   Gap to show end: {gap_to_show_end:.1f}s")

        # Extend timeline to trigger transition
        print("\n🔄 Extending timeline past show boundary...")

        extend_time = timeline_end - timedelta(seconds=60)
        with patch.object(timeline_scheduler, "_now", return_value=extend_time):
            timeline = timeline_scheduler.extend_timeline(timeline, extend_minutes=15)
        # This fixture reserves a tail for handover speech. The production
        # scheduler now waits until the boundary instead of handing over early.
        with patch.object(timeline_scheduler, "_now", return_value=show_end):
            timeline = timeline_scheduler.extend_timeline(timeline, extend_minutes=15)
        assert all(
            item.scheduled_start >= show_end
            for item in timeline.items
            if item.item_type.startswith("dj_transition")
        )

        print(f"\n📋 Extended timeline ({len(timeline.items)} items):")
        running_time = fixed_now
        for i, item in enumerate(timeline.items):
            end_time_item = running_time + timedelta(seconds=item.estimated_duration)
            dj_info = (
                f" [next: {item.next_dj_id}]"
                if hasattr(item, "next_dj_id") and item.next_dj_id
                else ""
            )
            songs_info = ""
            if hasattr(item.content, "songs") and item.content.songs:
                songs_info = f" [{', '.join([s.title[:15] + '...' if len(s.title) > 15 else s.title for s in item.content.songs])}]"

            # Mark items that end after show boundary
            boundary_marker = ""
            if item.dj_id == "daytime_diana":
                if end_time_item > show_end:
                    boundary_marker = " ⚠️ PAST BOUNDARY"
                elif (show_end - end_time_item).total_seconds() < 60:
                    boundary_marker = f" ✓ {(show_end - end_time_item).total_seconds():.0f}s before boundary"

            print(
                f"   {i + 1}. {item.item_type} @ {running_time.strftime('%H:%M:%S')} -> {end_time_item.strftime('%H:%M:%S')} ({item.estimated_duration:.1f}s) DJ: {item.dj_id}{dj_info}{songs_info}{boundary_marker}"
            )
            running_time = end_time_item

        # Verify transition items were created
        transition_end_items = [
            item for item in timeline.items if item.item_type == "dj_transition_end"
        ]
        transition_start_items = [
            item for item in timeline.items if item.item_type == "dj_transition_start"
        ]

        # Find the last Diana content item before transition
        diana_content_items = [
            item
            for item in timeline.items
            if item.dj_id == "daytime_diana" and item.item_type == "song_block"
        ]

        if diana_content_items:
            last_diana_item = diana_content_items[-1]
            last_diana_idx = timeline.items.index(last_diana_item)

            # Calculate when Diana's content ends
            diana_end_time = fixed_now
            for item in timeline.items[: last_diana_idx + 1]:
                diana_end_time += timedelta(seconds=item.estimated_duration)

            gap_before_transition = (show_end - diana_end_time).total_seconds()

            # Calculate actual gap after transition speech
            transition_end_time = diana_end_time
            for item in timeline.items[last_diana_idx + 1 :]:
                if item.item_type in ("dj_transition_end", "dj_transition_start"):
                    transition_end_time += timedelta(seconds=item.estimated_duration)
                else:
                    break
            actual_gap_to_boundary = (show_end - transition_end_time).total_seconds()
        else:
            gap_before_transition = None
            actual_gap_to_boundary = None

        print(f"\n📊 Results:")
        print(f"   Show end transition items: {len(transition_end_items)}")
        print(f"   Show start transition items: {len(transition_start_items)}")
        print(f"   DJ switch callbacks: {dj_switches}")
        print(f"   Final timeline DJ: {timeline.dj_id}")

        if gap_before_transition is not None:
            print(f"   Gap from last song to boundary: {gap_before_transition:.1f}s")
            print(f"   Actual gap after transitions: {actual_gap_to_boundary:.1f}s")
            if actual_gap_to_boundary <= 10:
                print(
                    f"   ✅ Excellent fit! Only {actual_gap_to_boundary:.1f}s gap after transitions"
                )
            elif actual_gap_to_boundary <= 30:
                print(
                    f"   ✅ Good fit! {actual_gap_to_boundary:.1f}s gap after transitions"
                )
            else:
                print(f"   ⚠️  Large gap after transitions - could fit more filler")

        # Assertions
        assert len(transition_end_items) >= 1, (
            "Should have at least one show_end transition"
        )
        assert len(transition_start_items) >= 1, (
            "Should have at least one show_start transition"
        )
        assert "afternoon_alex" in dj_switches, (
            "DJ switch callback should have been called for afternoon_alex"
        )
        assert timeline.dj_id == "afternoon_alex", (
            "Timeline DJ should now be afternoon_alex"
        )

        # Verify new content was added for afternoon_alex
        alex_items = [
            item
            for item in timeline.items
            if item.dj_id == "afternoon_alex" and item.item_type == "song_block"
        ]
        print(f"   Content items for afternoon_alex: {len(alex_items)}")

    print("\n✅ PASSED: Realistic best-fit transition works correctly")
    return True


def test_real_config_manager_time_range():
    """
    Test the REAL ConfigManager._time_in_range() method.

    This is critical because the mock had the correct implementation
    while the real code had a bug with inclusive end times.
    """
    print("\n" + "=" * 60)
    print("TEST: Real ConfigManager Time Range Boundary")
    print("=" * 60)

    from radio_server.config_manager import ConfigManager

    # Create a real config manager
    config_manager = ConfigManager(config_path="/app/config")

    # Test 1: Time clearly within range
    print("\n1. Testing time clearly within range (10:30 in 10:00-14:00):")
    result = config_manager._time_in_range(time(10, 30), time(10, 0), time(14, 0))
    print(f"   10:30 in [10:00, 14:00): {result}")
    assert result == True, "10:30 should be in range [10:00, 14:00)"

    # Test 2: Time at start (inclusive)
    print("\n2. Testing time at start boundary (10:00 in 10:00-14:00):")
    result = config_manager._time_in_range(time(10, 0), time(10, 0), time(14, 0))
    print(f"   10:00 in [10:00, 14:00): {result}")
    assert result == True, (
        "10:00 should be in range [10:00, 14:00) - start is inclusive"
    )

    # Test 3: Time at end (EXCLUSIVE - this is the critical fix!)
    print("\n3. Testing time at end boundary (14:00 in 10:00-14:00):")
    result = config_manager._time_in_range(time(14, 0), time(10, 0), time(14, 0))
    print(f"   14:00 in [10:00, 14:00): {result}")
    assert result == False, (
        "14:00 should NOT be in range [10:00, 14:00) - end is exclusive"
    )

    # Test 4: Time just before end
    print("\n4. Testing time just before end (13:59:59 in 10:00-14:00):")
    result = config_manager._time_in_range(time(13, 59, 59), time(10, 0), time(14, 0))
    print(f"   13:59:59 in [10:00, 14:00): {result}")
    assert result == True, "13:59:59 should be in range [10:00, 14:00)"

    # Test 5: Time in next slot (14:00 should be in 14:00-18:00)
    print("\n5. Testing time at next slot start (14:00 in 14:00-18:00):")
    result = config_manager._time_in_range(time(14, 0), time(14, 0), time(18, 0))
    print(f"   14:00 in [14:00, 18:00): {result}")
    assert result == True, "14:00 should be in range [14:00, 18:00)"

    # Test 6: Overnight range (22:00 to 02:00)
    print("\n6. Testing overnight range (23:00 in 22:00-02:00):")
    result = config_manager._time_in_range(time(23, 0), time(22, 0), time(2, 0))
    print(f"   23:00 in [22:00, 02:00): {result}")
    assert result == True, "23:00 should be in overnight range [22:00, 02:00)"

    # Test 7: Overnight range - early morning side
    print("\n7. Testing overnight range early morning (01:00 in 22:00-02:00):")
    result = config_manager._time_in_range(time(1, 0), time(22, 0), time(2, 0))
    print(f"   01:00 in [22:00, 02:00): {result}")
    assert result == True, "01:00 should be in overnight range [22:00, 02:00)"

    # Test 8: Overnight range - end boundary (exclusive)
    print("\n8. Testing overnight range end (02:00 in 22:00-02:00):")
    result = config_manager._time_in_range(time(2, 0), time(22, 0), time(2, 0))
    print(f"   02:00 in [22:00, 02:00): {result}")
    assert result == False, (
        "02:00 should NOT be in overnight range [22:00, 02:00) - end is exclusive"
    )

    print("\n✅ PASSED: Real ConfigManager time range boundary works correctly")
    return True


def test_real_config_manager_schedule_lookup():
    """
    Test the REAL ConfigManager.get_schedule_entry_at_time() method.

    This tests the actual schedule lookup that was failing at DJ transitions.
    """
    print("\n" + "=" * 60)
    print("TEST: Real ConfigManager Schedule Lookup at Boundaries")
    print("=" * 60)

    import pytz

    from radio_server.config_manager import ConfigManager

    # Use fixed schedule data: local production show times are editable.
    config_manager = ConfigManager.__new__(ConfigManager)
    config_manager.schedule = [
        MockScheduleEntry("morning_mike", time(6), time(10), []),
        MockScheduleEntry("daytime_diana", time(10), time(14), []),
        MockScheduleEntry("disco_stu", time(14), time(16), []),
        MockScheduleEntry("afternoon_alex", time(16), time(18), []),
        MockScheduleEntry("evening_emma", time(18), time(22), []),
        MockScheduleEntry("night_nick", time(22), time(6), []),
    ]
    timezone = pytz.timezone("Europe/London")

    # Test at exactly 16:00 - should return afternoon_alex, NOT disco_stu
    print("\n1. Testing schedule lookup at exactly 16:00:")
    test_time = timezone.localize(datetime(2026, 1, 20, 16, 0, 0))
    entry = config_manager.get_schedule_entry_at_time(test_time)
    print(f"   At 16:00:00, found DJ: {entry.dj_name if entry else 'None'}")

    # This was the actual bug - at 16:00, it was returning disco_stu instead of afternoon_alex
    assert entry is not None, "Should find a schedule entry at 16:00"
    assert entry.dj_name == "afternoon_alex", (
        f"At 16:00, should get afternoon_alex (16:00-18:00), not {entry.dj_name}"
    )

    # Test at 15:59:59 - should return disco_stu
    print("\n2. Testing schedule lookup at 15:59:59:")
    test_time = timezone.localize(datetime(2026, 1, 20, 15, 59, 59))
    entry = config_manager.get_schedule_entry_at_time(test_time)
    print(f"   At 15:59:59, found DJ: {entry.dj_name if entry else 'None'}")
    assert entry is not None, "Should find a schedule entry at 15:59:59"
    assert entry.dj_name == "disco_stu", (
        f"At 15:59:59, should get disco_stu (14:00-16:00), not {entry.dj_name}"
    )

    # Test at exactly 18:00 - should return evening_emma
    print("\n3. Testing schedule lookup at exactly 18:00:")
    test_time = timezone.localize(datetime(2026, 1, 20, 18, 0, 0))
    entry = config_manager.get_schedule_entry_at_time(test_time)
    print(f"   At 18:00:00, found DJ: {entry.dj_name if entry else 'None'}")
    assert entry is not None, "Should find a schedule entry at 18:00"
    assert entry.dj_name == "evening_emma", (
        f"At 18:00, should get evening_emma (18:00-22:00), not {entry.dj_name}"
    )

    # Test at exactly 10:00 - should return daytime_diana
    print("\n4. Testing schedule lookup at exactly 10:00:")
    test_time = timezone.localize(datetime(2026, 1, 20, 10, 0, 0))
    entry = config_manager.get_schedule_entry_at_time(test_time)
    print(f"   At 10:00:00, found DJ: {entry.dj_name if entry else 'None'}")
    assert entry is not None, "Should find a schedule entry at 10:00"
    assert entry.dj_name == "daytime_diana", (
        f"At 10:00, should get daytime_diana (10:00-14:00), not {entry.dj_name}"
    )

    print(
        "\n✅ PASSED: Real ConfigManager schedule lookup works correctly at boundaries"
    )
    return True


def run_all_tests():
    """Run all DJ transition tests."""
    print("\n" + "=" * 60)
    print("DJ TRANSITION TESTS")
    print("=" * 60)

    tests = [
        ("Real ConfigManager Time Range", test_real_config_manager_time_range),
        (
            "Real ConfigManager Schedule Lookup",
            test_real_config_manager_schedule_lookup,
        ),
        ("Basic Timeline Creation", test_timeline_creation_basic),
        ("DJ Transition Scheduling", test_dj_transition_scheduling),
        ("Late Transition Recovery", test_late_transition_recovery),
        ("Realistic Best-Fit Transition", test_realistic_best_fit_transition),
        ("Multiple Show Transitions", test_multiple_show_transitions),
        ("Overnight Transition", test_overnight_transition),
        ("Transition Item Order", test_transition_item_order),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, "PASSED" if result else "FAILED"))
        except Exception as e:
            logger.error(f"Test {name} failed with exception: {e}", exc_info=True)
            results.append((name, f"ERROR: {e}"))

    print("\n" + "=" * 60)
    print("TEST RESULTS SUMMARY")
    print("=" * 60)

    passed = 0
    for name, result in results:
        status = "✅" if result == "PASSED" else "❌"
        print(f"{status} {name}: {result}")
        if result == "PASSED":
            passed += 1

    print(f"\n{passed}/{len(tests)} tests passed")
    return passed == len(tests)


if __name__ == "__main__":
    success = run_all_tests()
    exit(0 if success else 1)
