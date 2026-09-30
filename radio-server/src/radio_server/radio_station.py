import asyncio
import contextlib
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .advanced_program_scheduler import AdvancedProgramScheduler, ScheduleSegment
from .audio_mixer import RadioAudioMixer
from .audio_utils import PLACEHOLDER_ARTWORK_FILENAME
from .config_manager import ConfigManager
from .config_watcher import ConfigWatcher
from .continuous_audio_producer import ContinuousAudioProducer
from .dj_ai import DJAI
from .music_library import MusicLibrary
from .news_feed_manager import NewsFeedManager
from .sponsor_manager import SponsorManager
from .timeline_api import TimelineAPIServer
from .timeline_scheduler import TimelineScheduler
from .video_stream_producer import VideoStreamProducer
from .weather_service import WeatherService

logger = logging.getLogger(__name__)


# Timing constants
TIMELINE_CHECK_INTERVAL_SECONDS = 5
DJ_TRANSITION_WAIT_SECONDS = 10
QUEUE_AHEAD_SECONDS = 5
QUEUE_WINDOW_SECONDS = 10
TRACK_REMAINING_THRESHOLD_SECONDS = 30
MAX_EARLY_QUEUE_SECONDS = 60
TIMING_VARIANCE_THRESHOLD_SECONDS = 10
MIN_SONG_REMAINDER_SECONDS = 0
QUEUE_WAIT_DELAY_SECONDS = 2
INTRO_QUEUE_DELAY_SECONDS = 0.5
CLEANUP_INTERVAL_SECONDS = 1800  # 30 minutes
LISTENER_CHECK_INTERVAL_SECONDS = 120  # Check listeners every 2 minutes
DIRECT_AUDIO_LISTENER_TIMEOUT_SECONDS = 30
CLEANUP_AGE_SECONDS = 3600  # 1 hour
ARTWORK_CLEANUP_AGE_SECONDS = 172800  # 48 hours
ERROR_RETRY_DELAY_SECONDS = 60
DEFAULT_AUDIO_DURATION_SECONDS = 180.0  # 3 minutes fallback
LOOK_AHEAD_MINUTES = 15
MAX_OUTRO_DURATION_SECONDS = 25.0
OUTRO_BUFFER_MIN_SECONDS = 3
OUTRO_BUFFER_MAX_SECONDS = 8


def _log_skew_at_start(item, actual_start: datetime) -> None:
    """Emit a SKEW log line when a timeline item is handed to the producer.

    `actual_start` is when the item was queued, which runs minutes ahead of when
    its audio reaches the stream, so this line measures the prep/queue chain
    only — never air time. Grep with `grep 'SKEW phase=start'` and aggregate per
    item_type to spot systematic delays in that chain.
    """
    queue_drift = (actual_start - item.scheduled_start).total_seconds()
    logger.info(
        f"📊 SKEW phase=start item_type={item.item_type} "
        f"dj={item.dj_id or '-'} estimated={item.estimated_duration:.2f}s "
        f"scheduled_start={item.scheduled_start.strftime('%H:%M:%S.%f')[:-3]} "
        f"queued_at={actual_start.strftime('%H:%M:%S.%f')[:-3]} "
        f"queue_drift={queue_drift:+.2f}s"
    )


def _log_skew_at_completion(
    item, source: str, on_air_seconds: float | None = None
) -> None:
    """Emit a SKEW log line when a timeline item completes.

    `on_air_seconds` is how long the producer actually streamed the item, which
    is the only figure comparable to `estimated_duration`. Without it the line
    can only fall back to `actual_end - actual_start`, and `actual_start` is a
    queue timestamp from minutes before the audio aired — that fallback used to
    report every item as several minutes over, drowning out real skew.

    Args:
        item: The timeline item that finished.
        source: Which completion path fired, so analysis can filter biased
            fallback paths from clean primary completions.
        on_air_seconds: Seconds the producer streamed this item's audio, or
            None when the caller has no measurement.
    """
    if on_air_seconds is None and (not item.actual_start or not item.actual_end):
        logger.info(
            f"📊 SKEW phase=complete item_type={item.item_type} "
            f"dj={item.dj_id or '-'} estimated={item.estimated_duration:.2f}s "
            f"actual=missing source={source}"
        )
        return

    if on_air_seconds is not None:
        actual = on_air_seconds
        measured = "on_air"
    else:
        actual = (item.actual_end - item.actual_start).total_seconds()
        measured = "queue_to_end"

    delta = actual - item.estimated_duration
    queue_drift = (item.actual_start - item.scheduled_start).total_seconds()
    logger.info(
        f"📊 SKEW phase=complete item_type={item.item_type} "
        f"dj={item.dj_id or '-'} estimated={item.estimated_duration:.2f}s "
        f"actual={actual:.2f}s delta={delta:+.2f}s measured={measured} "
        f"queue_drift={queue_drift:+.2f}s source={source}"
    )


@dataclass
class PlaylistItem:
    """Represents an item in the playlist."""

    type: str  # 'music', 'speech', 'jingle', 'ad'
    file_path: str
    metadata: dict
    duration: float


class RadioStation:
    """Main radio station orchestrator."""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager

        # Audio processing
        self.audio_mixer = RadioAudioMixer()  # Professional audio mixing

        # New architecture components
        self.continuous_audio = ContinuousAudioProducer()  # New continuous streaming
        self.continuous_audio.on_item_finished = self._on_audio_item_finished
        self.program_scheduler = None  # Will initialize after creating other components
        self.timeline_scheduler = None  # Timeline-based scheduler for advance planning
        self.timeline_api = None  # API server for timeline viewing

        self.dj_ai = None  # Will initialize after program scheduler
        self.music_library = MusicLibrary()
        self.weather_service = WeatherService()
        self.news_manager = NewsFeedManager()  # News feed manager for DJ news segments
        self.sponsor_manager = (
            SponsorManager()
        )  # Sponsor manager for DJ sponsor segments
        station_name = (
            config_manager.station_config.station_name
            if config_manager.station_config
            else "Radio Station"
        )
        self.video_stream = VideoStreamProducer(station_name=station_name)

        # Multi-DJ scheduler management for seamless transitions
        self.dj_schedulers: dict[
            str, AdvancedProgramScheduler
        ] = {}  # One scheduler per DJ
        self.dj_music_libraries: dict[
            str, MusicLibrary
        ] = {}  # Pre-loaded music libraries
        self.active_dj_id: str | None = None  # Currently active DJ
        self.preloading_task: asyncio.Task | None = None  # Background preload task

        # Config file watcher for hot reloading
        self.config_watcher = ConfigWatcher()

        self.is_running = False
        self.current_playlist: list[PlaylistItem] = []  # Legacy - will phase out
        self.last_ad_break = datetime.now()
        self.current_song_metadata = {}

        # Program scheduling state
        self.current_segment_task: asyncio.Task | None = None
        self.scheduler_task: asyncio.Task | None = None
        self.cleanup_task: asyncio.Task | None = None
        self.listener_monitor_task: asyncio.Task | None = None
        self.timeline_manager_task: asyncio.Task | None = None

        # Cached listener count - start with 1 to avoid skipping DJ speech on startup
        # (listeners may reconnect before the monitor has a chance to count them)
        self.cached_listener_count: int = 1
        self.direct_audio_listener_access: dict[str, float] = {}
        self.current_playing_timeline_id: str | None = None
        self._active_timeline_queue_id: str | None = None

    async def start(self) -> None:
        """Start the radio station."""
        logger.info("Starting radio station...")

        # Initialize all services
        await self._initialize_services()

        # Start news feed manager
        await self.news_manager.start()
        logger.info("📰 News feed manager started")

        # Log sponsor manager status
        if self.sponsor_manager.has_sponsors():
            status = self.sponsor_manager.get_status()
            logger.info(
                f"💰 Sponsor manager ready: {status['total_sponsors']} sponsors "
                f"({status['read_sponsors']} read, {status['vamp_sponsors']} vamp) - "
                f"Source: {status['source']}"
            )
        else:
            logger.warning("💰 Sponsor manager: No sponsors configured")

        # Setup config watcher for hot reloading
        self.config_watcher.register_reload_callback(
            "station", self.config_manager.reload_station_config
        )
        self.config_watcher.register_reload_callback(
            "schedule", self._reload_schedule_and_music
        )
        self.config_watcher.register_reload_callback(
            "dj_configs", self.config_manager.reload_dj_configs
        )
        self.config_watcher.register_reload_callback(
            "dj_prompts", self.dj_ai.unified_generator.reload_dj_prompts
        )
        self.config_watcher.register_reload_callback(
            "scheduler_weights", self.program_scheduler.reload_scheduler_weights
        )
        self.config_watcher.register_reload_callback(
            "jingles", self.program_scheduler.reload_jingles
        )
        self.config_watcher.register_reload_callback(
            "sponsors", self.sponsor_manager.reload
        )
        self.config_watcher.register_reload_callback(
            "news_feeds", self.news_manager.reload
        )
        self.config_watcher.register_reload_callback(
            "music_library", self.music_library.reload
        )
        self.config_watcher.start()
        logger.info(
            "🔍 Config file watcher started - configs will reload automatically"
        )

        # Start the continuous audio producer with metadata callback
        await self.continuous_audio.start_streaming(
            metadata_callback=self._handle_metadata_update
        )

        # Point the video pipeline at the local PCM relay for perfectly synced audio
        try:
            pcm_audio_url = self.continuous_audio.get_pcm_audio_url()
            if pcm_audio_url:
                self.video_stream.configure_pcm_audio(pcm_audio_url)
        except Exception as exc:
            logger.error(f"Failed to configure video audio source: {exc}")

        # Start the video stream producer for HLS output
        await self.video_stream.start()

        # Start the timeline API server
        asyncio.create_task(self.timeline_api.start(host="0.0.0.0", port=8080))

        # Legacy audio producer disabled
        # await self.audio_producer.start_streaming()

        # Start the program-driven broadcast system
        self.is_running = True
        self.scheduler_task = asyncio.create_task(self._program_broadcast_loop())

        # Start periodic cleanup task and do initial cleanup
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup_task())

        # Start listener monitoring task
        self.listener_monitor_task = asyncio.create_task(
            self._listener_monitoring_loop()
        )
        logger.info("👥 Listener monitoring started")
        asyncio.create_task(self._run_immediate_cleanup())

        logger.info("Radio station started successfully")

    async def stop(self) -> None:
        """Stop the radio station."""
        logger.info("Stopping radio station...")

        self.is_running = False

        if self.scheduler_task:
            self.scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.scheduler_task

        if self.cleanup_task:
            self.cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.cleanup_task

        if self.listener_monitor_task:
            self.listener_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.listener_monitor_task

        if self.current_segment_task:
            self.current_segment_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.current_segment_task

        # Stop timeline manager task
        await self._stop_current_timeline_manager()

        # Stop audio streaming
        await self.continuous_audio.stop_streaming()
        # await self.audio_producer.stop_streaming()  # Disabled

        # Stop news feed manager
        await self.news_manager.stop()

        # Stop config watcher
        self.config_watcher.stop()

        logger.info("Radio station stopped")

    def _station_name(self) -> str:
        """Return the configured station name, or a generic fallback."""
        cfg = getattr(self.config_manager, "station_config", None)
        return getattr(cfg, "station_name", None) or "Radio Station"

    async def _initialize_services(self) -> None:
        """Initialize all required services."""
        logger.info("Initializing services...")

        # Initialize music library
        music_folders = []
        current_entry = self.config_manager.get_current_schedule_entry()
        if current_entry:
            music_folders = current_entry.music_folders

        await self.music_library.initialize(music_folders)

        # Initialize weather service
        if self.config_manager.station_config:
            self.weather_service.set_location(
                self.config_manager.station_config.weather_location
            )

        # Initialize advanced program scheduler with all components
        self.program_scheduler = AdvancedProgramScheduler(
            self.config_manager,
            self.music_library,
            None,  # dj_ai will be set after initialization
            self.weather_service,
            None,  # audio_manager disabled for now
            self,  # Pass radio_station for listener count access
        )

        # Initialize DJAI with scheduler reference
        self.dj_ai = DJAI(
            scheduler=self.program_scheduler,
            config_manager=self.config_manager,
            weather_service=self.weather_service,
        )

        # Setup the unified generator now that all dependencies are available
        self.dj_ai.setup_unified_generator(
            self.config_manager,
            self.weather_service,
            self.news_manager,
            self.sponsor_manager,
        )

        # Update the scheduler with the DJAI instance
        self.program_scheduler.dj_ai = self.dj_ai

        if self.video_stream:
            self.video_stream.set_fact_generator(self.dj_ai.generate_song_facts)

        # Initialize timeline scheduler with config_manager for DJ transition handling
        self.timeline_scheduler = TimelineScheduler(
            self.program_scheduler, self.config_manager.timezone, self.config_manager
        )
        # Set up callback for DJ switching during transitions
        self.timeline_scheduler.on_dj_switch = self._handle_dj_switch

        # Initialize timeline API server
        self.timeline_api = TimelineAPIServer(self)

        logger.info("Services initialized")

    async def _reload_schedule_and_music(self) -> None:
        """Reload schedule and music library when schedule changes."""
        try:
            # Reload schedule config first
            self.config_manager.reload_schedule()

            # Get new music folders from updated schedule
            current_entry = self.config_manager.get_current_schedule_entry()
            if current_entry:
                music_folders = current_entry.music_folders
                logger.info(
                    f"🎵 Reinitializing music library with folders: {music_folders}"
                )
                await self.music_library.initialize(music_folders)
            else:
                logger.warning("⚠️ No current schedule entry after reload")
        except Exception as e:
            logger.error(f"❌ Error reloading schedule and music library: {e}")

    async def _handle_metadata_update(self, metadata: dict[str, Any]) -> None:
        """Record actual song starts before forwarding metadata to the overlay."""
        timeline = (
            self.timeline_scheduler.current_timeline
            if self.timeline_scheduler
            else None
        )
        item = (
            next(
                (
                    item
                    for item in timeline.items
                    if item.timeline_id == metadata.get("timeline_id")
                ),
                None,
            )
            if timeline
            else None
        )
        if metadata.get("content_type") == "MUSIC" and self.program_scheduler:
            tracker = self.program_scheduler.recent_tracker
            artist, title = metadata.get("artist", ""), metadata.get("title", "")
            key = tracker._get_song_key(artist, title)
            if item and item.item_type == "song_block":
                song_keys = {
                    tracker._get_song_key(song.artist, song.title)
                    for song in item.content.songs
                }
                if key in song_keys and key not in item.recorded_song_keys:
                    tracker.add_song(artist, title, item.dj_id)
                    item.recorded_song_keys.add(key)
            elif not metadata.get("timeline_id"):
                # Startup music has no timeline item, but is still a station play.
                tracker.add_song(artist, title, getattr(self, "active_dj_id", "") or "")
        if item:
            await self._sync_video_dj_overlay(item.dj_id)
        if self.video_stream:
            try:
                await self.video_stream.handle_metadata_update(metadata)
            except Exception as exc:
                logger.error(f"Error forwarding metadata to video stream: {exc}")

    async def _stop_current_timeline_manager(self) -> None:
        """Stop and await the active timeline manager task."""
        if self.timeline_scheduler:
            self.timeline_scheduler.stop_timeline_manager()
        else:
            logger.warning(
                "⚠️ Timeline scheduler not initialized during stop; skipping stop_timeline_manager"
            )

        if self.timeline_manager_task:
            task = self.timeline_manager_task
            self.timeline_manager_task = None

            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def _needs_flexible_timing(self, timeline_item) -> bool:
        """Check if a timeline item needs flexible timing (DJ content, intro/outro)"""
        # DJ talk and DJ transition items need immediate queueing when ready
        if timeline_item.item_type in [
            "dj_talk",
            "dj_transition_end",
            "dj_transition_start",
        ]:
            return True

        # Song blocks with intro/outro also need flexible timing
        if (
            timeline_item.item_type == "song_block"
            and hasattr(timeline_item, "content")
            and hasattr(timeline_item.content, "block_type")
        ):
            block_type = timeline_item.content.block_type
            return block_type in [
                "intro_then_songs",
                "songs_then_outro",
                "individual_intros",
                "individual_outros",
            ]

        return False

    async def _program_broadcast_loop(self) -> None:
        """Timeline-based broadcast loop with advance planning."""
        logger.info("Starting timeline-based broadcast loop...")

        # Create initial timeline - single path to success
        timeline = self._create_current_timeline()
        if timeline:
            logger.info(f"✅ Initial timeline created for {timeline.dj_id}")
            # Set as current and start manager
            self.timeline_scheduler.current_timeline = timeline
            if self.video_stream:
                await self.video_stream.update_dj(timeline.dj_id)
            self.timeline_manager_task = asyncio.create_task(
                self.timeline_scheduler.run_timeline_manager(timeline)
            )

            # Give the timeline manager a moment to start preparing items
            # This prevents the broadcast loop from racing with preparation
            await asyncio.sleep(2)
        else:
            logger.error("❌ Failed to create initial timeline - system cannot start")
            raise RuntimeError("Timeline creation failed - cannot start radio station")

        # Timeline-based broadcast loop
        while self.is_running:
            try:
                # Check if we have an active timeline
                timeline = self.timeline_scheduler.current_timeline
                if not timeline:
                    logger.warning("No active timeline, waiting...")
                    await asyncio.sleep(DJ_TRANSITION_WAIT_SECONDS)
                    continue

                # PROACTIVE PRELOADING: Always preload the next DJ before transition
                # This ensures their music library is ready when on_dj_switch is called
                next_entry = self.config_manager.get_next_schedule_entry()
                if next_entry and next_entry.dj_name not in self.dj_schedulers:
                    logger.info(
                        f"🔄 Preloading resources for next DJ: {next_entry.dj_name}"
                    )
                    await self._preload_next_dj(
                        next_entry.dj_name, next_entry.music_folders
                    )

                # Also preload current scheduled DJ if timeline is behind schedule
                current_entry = self.config_manager.get_current_schedule_entry()
                if (
                    current_entry
                    and current_entry.dj_name != timeline.dj_id
                    and current_entry.dj_name not in self.dj_schedulers
                ):
                    logger.info(
                        f"🔄 Preloading resources for current scheduled DJ: {current_entry.dj_name}"
                    )
                    await self._preload_next_dj(
                        current_entry.dj_name, current_entry.music_folders
                    )

                # Get current playback state
                playback_info = self.continuous_audio.get_current_playback_info()
                queue_size = self.continuous_audio.get_queue_size()

                current_track = self.continuous_audio.get_current_metadata()
                current_title = (
                    f"{current_track.get('artist', 'None')} - {current_track.get('title', 'None')}"
                    if current_track
                    else "None"
                )

                # Get current and next timeline items
                timezone = self.config_manager.timezone
                current_time = datetime.now(timezone)

                # Restart the timeline manager if it stopped unexpectedly. The
                # timeline runs continuously across show boundaries, so a dead
                # manager must be restarted regardless of the current show_end.
                if timeline and (
                    not self.timeline_manager_task or self.timeline_manager_task.done()
                ):
                    logger.warning(
                        "Timeline manager task missing; restarting for active show"
                    )
                    self.timeline_manager_task = asyncio.create_task(
                        self.timeline_scheduler.run_timeline_manager(timeline)
                    )

                current_item = self.timeline_scheduler.get_current_item(
                    timeline, self.get_on_air_timeline_id()
                )

                # Enhanced timeline status for better debugging
                next_unplayed = self.timeline_scheduler.get_next_unplayed_item(timeline)
                next_item_status = next_unplayed.status if next_unplayed else "None"
                next_item_type = next_unplayed.item_type if next_unplayed else "None"

                logger.debug(
                    f"🔄 TIMELINE STATUS - Queue: {queue_size}, Playing: {current_title}, "
                    f"Current Item: {current_item.item_type if current_item else 'None'}, "
                    f"Next Item: {next_item_type} ({next_item_status}), "
                    f"Is Playing: {playback_info.get('is_playing', False)}"
                )

                # Additional debugging for queue issues
                if queue_size > 0 and not playback_info.get("is_playing", False):
                    logger.warning(
                        f"⚠️ Queue has {queue_size} items but nothing is playing - possible playback issue"
                    )

                # Check if we need to queue the next timeline item
                should_queue_next = False

                # Get the next unplayed item in timeline order (not by scheduled time)
                next_item = self.timeline_scheduler.get_next_unplayed_item(timeline)

                if next_item:
                    needs_flexible = self._needs_flexible_timing(next_item)
                    time_until_next = (
                        next_item.scheduled_start - current_time
                    ).total_seconds()

                    if queue_size == 0:
                        # Queue is empty - we need content
                        if playback_info.get("is_playing", False):
                            # Something is playing but queue is empty - preload next item
                            if next_item.status == "ready" and (
                                playback_info.get("remaining_time", 999)
                                < TRACK_REMAINING_THRESHOLD_SECONDS
                                or time_until_next <= QUEUE_WINDOW_SECONDS
                                or needs_flexible
                            ):
                                should_queue_next = True
                        else:
                            # Nothing playing and queue is empty - queue immediately if ready
                            if next_item.status == "ready":
                                logger.info(
                                    f"🚨 Queue is empty, nothing playing - queuing {next_item.item_type}"
                                )
                                should_queue_next = True
                            elif next_item.status in ["scheduled", "preparing"]:
                                # Need to wait for preparation
                                logger.debug(
                                    f"⏳ Queue empty but next item not ready yet: {next_item.item_type} ({next_item.status})"
                                )
                    else:
                        # Queue has items - only preload if item is ready and due soon
                        if next_item.status == "ready" and (
                            time_until_next <= QUEUE_WINDOW_SECONDS
                            or (
                                playback_info.get("remaining_time", 999)
                                < TRACK_REMAINING_THRESHOLD_SECONDS
                                and time_until_next <= MAX_EARLY_QUEUE_SECONDS
                            )
                            or needs_flexible
                        ):
                            should_queue_next = True
                else:
                    logger.debug(
                        "⏳ No unplayed items in timeline - waiting for content"
                    )

                # Queue the next timeline item if needed
                if should_queue_next and next_item:
                    # Check if item is being prepared
                    if next_item.status == "preparing":
                        logger.info(
                            f"🔧 Timeline item still being prepared: {next_item.timeline_id}"
                        )
                        if next_item.item_type == "jingle":
                            logger.info(
                                "🎵 Jingle still preparing: id=%s scheduled=%s",
                                next_item.timeline_id,
                                next_item.scheduled_start.strftime("%H:%M:%S"),
                            )
                        # Wait for the existing preparation task if there is one
                        # Use asyncio.shield to prevent task cancellation on timeout
                        existing_task = self.timeline_scheduler.preparation_tasks.get(
                            next_item.timeline_id
                        )
                        if existing_task and not existing_task.done():
                            logger.info(
                                f"⏳ Waiting for existing preparation task: {next_item.timeline_id}"
                            )
                            try:
                                # Shield the task so timeout doesn't cancel it
                                await asyncio.wait_for(
                                    asyncio.shield(existing_task), timeout=30.0
                                )
                                logger.info(
                                    f"✅ Preparation task completed: {next_item.timeline_id}"
                                )
                            except TimeoutError:
                                logger.warning(
                                    f"⚠️ Preparation wait timed out (task continues): {next_item.timeline_id}"
                                )
                                # Task continues in background, will check again next iteration

                    # Re-check status after waiting - item may now be ready
                    if next_item.status == "scheduled":
                        # Check if there's already a preparation task running
                        existing_task = self.timeline_scheduler.preparation_tasks.get(
                            next_item.timeline_id
                        )
                        if existing_task and not existing_task.done():
                            logger.info(
                                f"⏳ Waiting for existing preparation: {next_item.timeline_id}"
                            )
                            try:
                                # Shield the task so timeout doesn't cancel it
                                await asyncio.wait_for(
                                    asyncio.shield(existing_task), timeout=30.0
                                )
                            except TimeoutError:
                                logger.warning(
                                    f"⚠️ Preparation wait timed out (task continues): {next_item.timeline_id}"
                                )
                                # Task continues in background, will check again next iteration
                        else:
                            logger.info(
                                f"🔧 Forcing preparation for overdue item: {next_item.timeline_id}"
                            )
                            await self.timeline_scheduler._prepare_item(
                                timeline, next_item
                            )
                        if next_item.status != "ready":
                            logger.warning(
                                f"⚠️ Overdue item not ready after forced preparation: {next_item.timeline_id} ({next_item.status})"
                            )
                            should_queue_next = False

                    if should_queue_next and next_item.status == "ready":
                        time_until_start = (
                            next_item.scheduled_start - current_time
                        ).total_seconds()
                        logger.info(
                            f"🎯 QUEUEING TIMELINE ITEM: {next_item.item_type} "
                            f"scheduled for {next_item.scheduled_start.strftime('%H:%M:%S')} "
                            f"(in {time_until_start:.1f}s)"
                        )

                        # Mark as queued BEFORE playing to prevent duplicate queueing
                        next_item.status = "queued"

                        try:
                            await self._play_timeline_item(next_item)

                            # Mark item as playing with accurate timing
                            next_item.status = "playing"
                            next_item.actual_start = current_time
                            _log_skew_at_start(next_item, current_time)

                            # Don't adjust scheduled_start - keep it as the original planned time
                            time_diff = (
                                current_time - next_item.scheduled_start
                            ).total_seconds()
                            if abs(time_diff) > TIMING_VARIANCE_THRESHOLD_SECONDS:
                                logger.warning(
                                    f"⏰ Timeline timing variance: item was {time_diff:.1f}s off schedule "
                                    f"(scheduled: {next_item.scheduled_start.strftime('%H:%M:%S')}, "
                                    f"actual: {current_time.strftime('%H:%M:%S')})"
                                )

                            logger.info(
                                f"✅ Successfully processed timeline item: {next_item.item_type} "
                                f"(scheduled: {next_item.scheduled_start.strftime('%H:%M:%S')}, "
                                f"actual: {current_time.strftime('%H:%M:%S')})"
                            )

                        except Exception as e:
                            logger.error(
                                f"❌ Error playing timeline item {next_item.timeline_id}: {e}",
                                exc_info=True,
                            )
                            next_item.status = "failed"

                # Wait before next iteration
                await asyncio.sleep(TIMELINE_CHECK_INTERVAL_SECONDS)

            except Exception as e:
                logger.error(f"Error in timeline broadcast loop: {e}")
                await asyncio.sleep(DJ_TRANSITION_WAIT_SECONDS)
                continue

    async def _preload_next_dj(self, dj_id: str, music_folders: list[str]) -> None:
        """Preload the next DJ's resources (music library and scheduler) in the background.

        This allows the next DJ to start immediately when their show begins, with no
        initialization delay.
        """
        try:
            logger.info(f"🔄 Preloading resources for {dj_id}")

            # Check if already preloaded
            if dj_id in self.dj_schedulers and dj_id in self.dj_music_libraries:
                self._attach_shared_recent_tracker(self.dj_schedulers[dj_id])
                logger.info(f"✅ Resources already preloaded for {dj_id}")
                return

            # Create and initialize music library for this DJ
            if dj_id not in self.dj_music_libraries:
                dj_music_lib = MusicLibrary()
                await dj_music_lib.initialize(music_folders)
                self.dj_music_libraries[dj_id] = dj_music_lib
                logger.info(
                    f"✅ Preloaded music library for {dj_id}: {len(music_folders)} folders"
                )

            # Create scheduler instance for this DJ
            if dj_id not in self.dj_schedulers:
                dj_config = self.config_manager.get_dj_config(dj_id)
                if dj_config:
                    dj_scheduler = self._create_scheduler(
                        self.dj_music_libraries[dj_id]
                    )
                    self.dj_schedulers[dj_id] = dj_scheduler
                    logger.info(f"✅ Preloaded scheduler for {dj_id}")

        except Exception as e:
            logger.error(f"❌ Error preloading DJ {dj_id}: {e}", exc_info=True)

    def _cleanup_old_dj_resources(self, current_dj: str) -> None:
        """Clean up resources for DJs that are no longer active.

        Keeps only the current DJ and the next upcoming DJ loaded in memory.
        """
        try:
            # Get next DJ from schedule
            next_entry = self.config_manager.get_next_schedule_entry()
            next_dj = next_entry.dj_name if next_entry else None

            # Keep current and next DJ
            djs_to_keep = {current_dj}
            if next_dj:
                djs_to_keep.add(next_dj)

            # Clean up old schedulers
            old_djs = set(self.dj_schedulers.keys()) - djs_to_keep
            for old_dj in old_djs:
                del self.dj_schedulers[old_dj]
                logger.info(f"🗑️ Cleaned up scheduler for {old_dj}")

            # Clean up old music libraries
            old_libs = set(self.dj_music_libraries.keys()) - djs_to_keep
            for old_dj in old_libs:
                del self.dj_music_libraries[old_dj]
                logger.info(f"🗑️ Cleaned up music library for {old_dj}")

        except Exception as e:
            logger.error(f"❌ Error cleaning up old DJ resources: {e}")

    def _handle_dj_switch(
        self,
        new_dj_id: str,
        start_time: datetime,
        end_time: datetime,
        music_folders: list[str],
    ) -> None:
        """Handle switching to a new DJ during timeline extension.

        This is called synchronously from the timeline scheduler when it detects
        a DJ transition and needs to switch schedulers for planning future content.

        Args:
            new_dj_id: The DJ to switch to
            start_time: The current time (start of planning window for new DJ)
            end_time: The new DJ's show end time
            music_folders: The music folders for the new DJ's show
        """
        try:
            logger.info(
                f"🔄 Switching scheduler to {new_dj_id} for timeline planning (until {end_time.strftime('%H:%M:%S')}) with music folders: {music_folders}"
            )

            new_scheduler = None

            # Check if we have a preloaded scheduler for this DJ
            if new_dj_id in self.dj_schedulers:
                new_scheduler = self.dj_schedulers[new_dj_id]
                self._attach_shared_recent_tracker(new_scheduler)
                self.timeline_scheduler.advanced_scheduler = new_scheduler
                logger.info(f"✅ Switched to preloaded scheduler for {new_dj_id}")
            else:
                # Need to create scheduler on-demand (shouldn't normally happen)
                logger.warning(
                    f"⚠️ No preloaded scheduler for {new_dj_id}, creating on-demand"
                )
                dj_config = self.config_manager.get_dj_config(new_dj_id)
                current_entry = self.config_manager.get_current_schedule_entry()

                if dj_config and current_entry:
                    # Use preloaded library if available, otherwise start empty
                    # and register it so the async _reinitialize_music_library
                    # task below populates the same object the scheduler will hold.
                    if new_dj_id in self.dj_music_libraries:
                        dj_music_lib = self.dj_music_libraries[new_dj_id]
                    else:
                        dj_music_lib = MusicLibrary()
                        self.dj_music_libraries[new_dj_id] = dj_music_lib

                    # Create scheduler
                    new_scheduler = self._create_scheduler(dj_music_lib)
                    self.dj_schedulers[new_dj_id] = new_scheduler
                    self.timeline_scheduler.advanced_scheduler = new_scheduler
                    logger.info(f"✅ Created on-demand scheduler for {new_dj_id}")

            # CRITICAL: Start the DJ show on the new scheduler
            # Without this, get_next_segment() returns empty because current_dj is None
            if new_scheduler:
                new_scheduler.start_dj_show(new_dj_id, start_time, end_time)
                logger.info(
                    f"🎬 Started DJ show for {new_dj_id} on scheduler (until {end_time.strftime('%H:%M:%S')})"
                )

            # Update active DJ
            old_dj = self.active_dj_id
            self.active_dj_id = new_dj_id

            # Reinitialize the main music library with the new DJ's folders
            # This is critical - without this, songs are still selected from the old DJ's folders
            import asyncio

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Schedule the async initialization
                    asyncio.create_task(
                        self._reinitialize_music_library(new_dj_id, music_folders)
                    )
                else:
                    loop.run_until_complete(
                        self.music_library.initialize(music_folders)
                    )
            except Exception as e:
                logger.error(f"Failed to reinitialize music library: {e}")

            # Schedule cleanup of old DJ resources (but keep the cleanup asynchronous)
            if old_dj and old_dj != new_dj_id:
                self._cleanup_old_dj_resources(new_dj_id)

        except Exception as e:
            logger.error(f"❌ Error handling DJ switch: {e}", exc_info=True)

    def _attach_shared_recent_tracker(
        self, scheduler: AdvancedProgramScheduler
    ) -> None:
        """Keep recent-song history shared across all scheduler instances."""
        if not scheduler or not self.program_scheduler:
            return

        shared_tracker = getattr(self.program_scheduler, "recent_tracker", None)
        if not shared_tracker:
            return

        if getattr(scheduler, "recent_tracker", None) is not shared_tracker:
            scheduler.recent_tracker = shared_tracker
            logger.debug("🔗 Linked shared recent tracker to scheduler instance")

    def _create_scheduler(
        self, music_library: MusicLibrary
    ) -> AdvancedProgramScheduler:
        """Create a scheduler with shared state wiring."""
        scheduler = AdvancedProgramScheduler(
            config_manager=self.config_manager,
            music_library=music_library,
            dj_ai=self.dj_ai,
            weather_service=self.weather_service,
            audio_manager=None,
            radio_station=self,
        )
        self._attach_shared_recent_tracker(scheduler)
        return scheduler

    async def _reinitialize_music_library(
        self, dj_id: str, music_folders: list[str]
    ) -> None:
        """Populate the DJ's own music library with the given folders.

        The scheduler holds a permanent reference to dj_music_libraries[dj_id]
        and reads tracks directly from it, so initializing that library in
        place makes the new DJ's tracks available without reassigning any
        scheduler reference. Reassigning to self.music_library (a shared
        global re-initialized async on every switch) opens a race where the
        scheduler briefly serves the previous DJ's tracks.
        """
        try:
            dj_lib = self.dj_music_libraries.get(dj_id)
            if dj_lib is None:
                dj_lib = MusicLibrary()
                self.dj_music_libraries[dj_id] = dj_lib

            # Skip the rescan if preload already loaded these exact folders
            if dj_lib.current_folders != music_folders or not dj_lib.tracks:
                logger.info(
                    f"🎵 Reinitializing music library for {dj_id} with folders: {music_folders}"
                )
                await dj_lib.initialize(music_folders)

            # Keep the global library in sync for non-scheduler consumers
            # (startup song, config-watcher reload callback)
            if (
                self.music_library.current_folders != music_folders
                or not self.music_library.tracks
            ):
                await self.music_library.initialize(music_folders)

            logger.info(
                f"✅ Music library ready for {dj_id}: {len(dj_lib.get_all_tracks())} tracks"
            )
            # A previous empty-library pass may have backed off. Loading is now
            # complete, so allow the next manager tick to fill the timeline.
            self.timeline_scheduler._next_extension_retry_at = None
        except Exception as e:
            logger.error(f"❌ Error reinitializing music library: {e}", exc_info=True)

    async def _handle_transition_end_item(self, timeline_item) -> None:
        """Handle show_end transition item - play outgoing DJ's sign-off."""
        try:
            outgoing_dj = timeline_item.dj_id
            incoming_dj = timeline_item.next_dj_id

            logger.info(f"🎙️ Playing show_end for {outgoing_dj}")

            outgoing_config = self.config_manager.get_dj_config(outgoing_dj)

            # Check if we have pre-generated audio from timeline preparation
            if (
                hasattr(timeline_item.content, "audio_file")
                and timeline_item.content.audio_file
            ):
                logger.info(
                    f"✅ Using pre-generated show_end: {timeline_item.content.audio_file}"
                )
                await self._queue_audio(
                    timeline_item.content.audio_file,
                    {
                        "artist": outgoing_config.name
                        if outgoing_config
                        else outgoing_dj,
                        "title": "Show End",
                        "content_type": "DJ_TALK",
                    },
                    timeline_id=timeline_item.timeline_id,
                )
                return

            # Fallback: Generate audio on-the-fly if not pre-generated
            logger.warning(
                "⚠️ No pre-generated audio for show_end, generating on-the-fly"
            )
            incoming_config = (
                self.config_manager.get_dj_config(incoming_dj) if incoming_dj else None
            )

            if outgoing_config:
                from .unified_dj_generator import DJTalkRequest

                show_end_request = DJTalkRequest(
                    talk_category="show_transitions",
                    style="show_end",
                    context={
                        "next_dj_name": incoming_config.name.replace("_", " ").title()
                        if incoming_config
                        else "the next DJ",
                    },
                )

                show_end_result = await self.dj_ai.unified_generator.generate_dj_talk(
                    outgoing_config, show_end_request
                )

                if show_end_result.success and show_end_result.audio_file:
                    logger.info(f"✅ Generated show_end: {show_end_result.audio_file}")
                    await self._queue_audio(
                        show_end_result.audio_file,
                        {
                            "artist": outgoing_config.name,
                            "title": "Show End",
                            "content_type": "DJ_TALK",
                        },
                        timeline_id=timeline_item.timeline_id,
                    )
                else:
                    logger.error(
                        f"❌ Failed to generate show_end: {show_end_result.error}"
                    )

        except Exception as e:
            logger.error(f"❌ Error handling transition end: {e}", exc_info=True)

    async def _handle_transition_start_item(self, timeline_item) -> None:
        """Handle show_start transition item - update video overlay and play incoming DJ's intro.

        Note: The scheduler switch is already done during timeline extension by _handle_dj_switch.
        This method just handles the playback of the transition audio.
        """
        try:
            incoming_dj = timeline_item.dj_id

            logger.info(f"🎙️ Playing show_start for {incoming_dj}")

            incoming_config = self.config_manager.get_dj_config(incoming_dj)

            # Check if we have pre-generated audio from timeline preparation
            if (
                hasattr(timeline_item.content, "audio_file")
                and timeline_item.content.audio_file
            ):
                logger.info(
                    f"✅ Using pre-generated show_start: {timeline_item.content.audio_file}"
                )
                await self._queue_audio(
                    timeline_item.content.audio_file,
                    {
                        "artist": incoming_config.name
                        if incoming_config
                        else incoming_dj,
                        "title": "Show Start",
                        "content_type": "DJ_TALK",
                    },
                    timeline_id=timeline_item.timeline_id,
                )
                return

            # Fallback: Generate audio on-the-fly if not pre-generated
            logger.warning(
                "⚠️ No pre-generated audio for show_start, generating on-the-fly"
            )

            if incoming_config:
                from .unified_dj_generator import DJTalkRequest

                show_start_request = DJTalkRequest(
                    talk_category="show_transitions",
                    style="show_start",
                )

                show_start_result = await self.dj_ai.unified_generator.generate_dj_talk(
                    incoming_config, show_start_request
                )

                if show_start_result.success and show_start_result.audio_file:
                    logger.info(
                        f"✅ Generated show_start: {show_start_result.audio_file}"
                    )
                    await self._queue_audio(
                        show_start_result.audio_file,
                        {
                            "artist": incoming_config.name,
                            "title": "Show Start",
                            "content_type": "DJ_TALK",
                        },
                        timeline_id=timeline_item.timeline_id,
                    )
                else:
                    logger.error(
                        f"❌ Failed to generate show_start: {show_start_result.error}"
                    )

        except Exception as e:
            logger.error(f"❌ Error handling transition start: {e}", exc_info=True)

    def _create_current_timeline(self) -> Any | None:
        """Create timeline for current schedule - single robust path"""
        try:
            timezone = self.config_manager.timezone
            now = datetime.now(timezone)
            logger.info(
                f"🕐 TIMEZONE DEBUG - Timezone: {timezone}, Current time: {now}, UTC time: {datetime.utcnow()}"
            )

            # Get current schedule entry
            current_entry = self.config_manager.get_current_schedule_entry()
            if not current_entry:
                logger.error("No current schedule entry found")
                return None

            # Calculate show times
            today = now.date()
            show_start = timezone.localize(
                datetime.combine(today, current_entry.start_time)
            )
            show_end = timezone.localize(
                datetime.combine(today, current_entry.end_time)
            )

            # Handle overnight shows
            if current_entry.end_time < current_entry.start_time:
                if now.time() < current_entry.end_time:
                    show_start = show_start - timedelta(days=1)
                else:
                    show_end = show_end + timedelta(days=1)

            dj_id = current_entry.dj_name

            # Use current time as start if we're already past the show start
            timeline_start = max(now, show_start)
            logger.info(
                f"Creating timeline for {dj_id} from {timeline_start} to {show_end} (show started at {show_start})"
            )

            # Create timeline - pass original show times to scheduler but start timeline from current time
            timeline = self.timeline_scheduler.create_show_timeline_mid_show(
                dj_id,
                timeline_start,
                show_end,
                show_start,
                look_ahead_minutes=LOOK_AHEAD_MINUTES,
            )

            return timeline

        except (AttributeError, KeyError, ValueError) as e:
            logger.error(f"Timeline creation failed: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error creating timeline: {type(e).__name__}: {e}",
                exc_info=True,
            )
            return None

    async def _play_timeline_item(self, timeline_item) -> None:
        """Stage all audio, then queue the complete item atomically for playback."""
        self._pending_timeline_audio = []
        try:
            await self._queue_timeline_item(timeline_item)
            audio_parts = self._pending_timeline_audio
            if not audio_parts:
                raise RuntimeError("Timeline item produced no audio")
            if timeline_item.not_before:
                audio_parts[0]["metadata"]["not_before"] = (
                    timeline_item.not_before.isoformat()
                )
            await self.continuous_audio.queue_audio_files(audio_parts)
            timeline_item.remaining_audio_parts = len(audio_parts)
        finally:
            self._pending_timeline_audio = None

    async def _queue_timeline_item(self, timeline_item) -> None:
        """Play a timeline item based on its type."""
        try:
            previous_context = self._active_timeline_queue_id
            self._active_timeline_queue_id = timeline_item.timeline_id
            try:
                # Handle DJ transition items
                if timeline_item.item_type == "dj_transition_end":
                    await self._handle_transition_end_item(timeline_item)
                    return
                elif timeline_item.item_type == "dj_transition_start":
                    await self._handle_transition_start_item(timeline_item)
                    return

                if timeline_item.item_type == "song_block":
                    # Create a compatible segment structure
                    segment = ScheduleSegment(
                        segment_id=timeline_item.timeline_id,
                        segment_type="song_block",
                        content=timeline_item.content,
                        start_time=timeline_item.scheduled_start,
                        estimated_duration=timeline_item.estimated_duration,
                        dj_id=timeline_item.dj_id,
                    )
                    await self._play_song_block(segment, timeline_item.dj_id)

                elif timeline_item.item_type == "jingle":
                    # Play jingle as a simple audio file
                    from .advanced_program_scheduler import JingleSegment

                    jingle_segment = timeline_item.content
                    if isinstance(jingle_segment, JingleSegment):
                        logger.info(
                            "🎵 Playing jingle: %s (id=%s scheduled=%s file=%s)",
                            jingle_segment.name,
                            timeline_item.timeline_id,
                            timeline_item.scheduled_start.strftime("%H:%M:%S"),
                            jingle_segment.file_path,
                        )
                        await self._play_dj_audio(
                            jingle_segment.file_path,
                            metadata={
                                "title": jingle_segment.name or "Station Jingle",
                                "artist": self._station_name(),
                                "album": "Station Jingles",
                                "year": "",
                                "content_type": "JINGLE",
                            },
                        )
                    else:
                        logger.error(
                            f"Invalid jingle segment content type: {type(jingle_segment)}"
                        )

                elif timeline_item.item_type == "dj_talk":
                    # Create a compatible segment structure
                    segment = ScheduleSegment(
                        segment_id=timeline_item.timeline_id,
                        segment_type=timeline_item.item_type,
                        content=timeline_item.content,
                        start_time=timeline_item.scheduled_start,
                        estimated_duration=timeline_item.estimated_duration,
                        dj_id=timeline_item.dj_id,
                    )
                    await self._play_dj_talk_segment(segment)

                else:
                    logger.warning(
                        f"Unknown timeline item type: {timeline_item.item_type}"
                    )
            finally:
                self._active_timeline_queue_id = previous_context

        except Exception as e:
            logger.error(
                f"Failed to play timeline item {timeline_item.timeline_id}: {type(e).__name__}: {e}",
                exc_info=True,
            )
            raise

    async def _sync_video_dj_overlay(self, dj_id: str | None) -> None:
        """Align the video overlay DJ label with the timeline item being played."""
        if not self.video_stream or not dj_id:
            return

        try:
            await self.video_stream.update_dj(dj_id)
        except Exception as exc:
            logger.error(f"Failed to update video DJ overlay for {dj_id}: {exc}")

    async def _play_advanced_segment(self, segment) -> None:
        """Play an advanced program segment with proper handling."""
        try:
            if segment.segment_type == "song_block":
                await self._play_song_block(segment, segment.dj_id)
            elif segment.segment_type == "dj_talk":
                await self._play_dj_talk_segment(segment)
            else:
                logger.warning(f"Unknown segment type: {segment.segment_type}")

        except Exception as e:
            logger.error(
                f"Failed to play segment {segment.segment_id}: {type(e).__name__}: {e}",
                exc_info=True,
            )

    async def _play_song_block(self, segment, _dj_id: str) -> None:
        """Play a song block with proper intro/outro handling."""
        song_block = segment.content

        logger.info(
            f"Playing song block: {song_block.block_type} with {len(song_block.songs)} songs"
        )

        if getattr(song_block, "prepared_mix_file", None):
            metadata = song_block.prepared_mix_metadata
            if not metadata and song_block.songs:
                first_song = song_block.songs[0]
                metadata = {
                    "title": first_song.title,
                    "artist": first_song.artist,
                    "album": getattr(first_song, "album", "Unknown Album"),
                    "year": str(first_song.year) if first_song.year else "",
                    "content_type": "MUSIC",
                    "file_path": str(getattr(first_song, "file_path", "")),
                }
            await self._queue_audio(
                song_block.prepared_mix_file,
                metadata or {},
                metadata_schedule=song_block.prepared_mix_metadata_schedule,
            )

            # If outro is separate from song, queue it after the mix
            if (
                getattr(song_block, "outro_audio", None)
                and song_block.crossover_type != "over_song"
            ):
                await self._play_dj_audio(song_block.outro_audio)
            return

        # Handle different block types - block_type determines DJ audio behavior
        if song_block.block_type == "intro_then_songs":
            if getattr(song_block, "intro_audio", None):
                intro_song = await self._create_full_intro_song_file(
                    song_block.songs[0], song_block.intro_audio
                )
                if intro_song:
                    intro_file, intro_duration = intro_song
                    enhanced_song = self._build_song_for_mix(
                        song_block.songs[0], intro_file, intro_duration
                    )
                    mix_songs = [enhanced_song] + song_block.songs[1:]
                    await self._play_song_list_with_crossfade(mix_songs)
                else:
                    logger.warning(
                        "⚠️ Intro mix failed; falling back to mixed intro sequence"
                    )
                    await self._play_mixed_intro_sequence(song_block)
            else:
                await self._play_song_list_with_crossfade(song_block.songs)

        elif song_block.block_type == "songs_then_outro":
            # Determine how to handle outro based on crossover type
            if song_block.crossover_type == "over_song":
                if song_block.outro_audio:
                    last_song = song_block.songs[-1]
                    outro_song = await self._create_full_outro_song_file(
                        last_song, song_block.outro_audio
                    )
                    if outro_song:
                        outro_file, outro_duration = outro_song
                        enhanced_last = self._build_song_for_mix(
                            last_song, outro_file, outro_duration
                        )
                        mix_songs = song_block.songs[:-1] + [enhanced_last]
                        await self._play_song_list_with_crossfade(mix_songs)
                    else:
                        logger.warning(
                            "⚠️ Outro mix failed; falling back to mixed outro sequence"
                        )
                        await self._play_mixed_outro_sequence(song_block)
                else:
                    await self._play_song_list_with_crossfade(song_block.songs)
            else:
                # Play all songs normally, then separate outro
                await self._play_song_list_with_crossfade(song_block.songs)

                if hasattr(song_block, "outro_audio") and song_block.outro_audio:
                    await self._play_dj_audio(song_block.outro_audio)

        elif song_block.block_type == "individual_intros":
            # Each song gets its own intro, then crossfade the full song files
            mix_songs = []
            if (
                hasattr(song_block, "intro_audio_files")
                and song_block.intro_audio_files
            ):
                for i, song in enumerate(song_block.songs):
                    intro_audio = None
                    if i < len(song_block.intro_audio_files):
                        intro_audio = song_block.intro_audio_files[i]

                    if intro_audio:
                        intro_song = await self._create_full_intro_song_file(
                            song, intro_audio
                        )
                        if intro_song:
                            intro_file, intro_duration = intro_song
                            mix_songs.append(
                                self._build_song_for_mix(
                                    song, intro_file, intro_duration
                                )
                            )
                            continue

                    if not intro_audio:
                        logger.warning(
                            f"No intro audio for song {i}: {song.artist} - {song.title}"
                        )
                    else:
                        logger.warning(
                            f"Intro mix failed for song {i}: {song.artist} - {song.title}"
                        )
                    mix_songs.append(song)

                await self._play_song_list_with_crossfade(mix_songs)
            else:
                logger.warning(
                    "individual_intros block but no intro_audio_files, playing songs normally"
                )
                await self._play_song_list_with_crossfade(song_block.songs)

        elif song_block.block_type == "individual_outros":
            # Each song gets its own outro, then crossfade the full song files
            if (
                hasattr(song_block, "outro_audio_files")
                and song_block.outro_audio_files
                and song_block.crossover_type == "over_song"
            ):
                mix_songs = []
                for i, song in enumerate(song_block.songs):
                    outro_audio = None
                    if i < len(song_block.outro_audio_files):
                        outro_audio = song_block.outro_audio_files[i]

                    if outro_audio:
                        outro_song = await self._create_full_outro_song_file(
                            song, outro_audio
                        )
                        if outro_song:
                            outro_file, outro_duration = outro_song
                            mix_songs.append(
                                self._build_song_for_mix(
                                    song, outro_file, outro_duration
                                )
                            )
                            continue

                    if not outro_audio:
                        logger.warning(
                            f"No outro audio for song {i}: {song.artist} - {song.title}"
                        )
                    else:
                        logger.warning(
                            f"Outro mix failed for song {i}: {song.artist} - {song.title}"
                        )
                    mix_songs.append(song)

                await self._play_song_list_with_crossfade(mix_songs)
            else:
                # Fallback: no individual outros prepared or separate_from_song crossover
                if (
                    hasattr(song_block, "outro_audio_files")
                    and song_block.outro_audio_files
                    and song_block.crossover_type != "over_song"
                ):
                    # Preserve separate-outro behavior
                    for i, song in enumerate(song_block.songs):
                        if (
                            i < len(song_block.outro_audio_files)
                            and song_block.outro_audio_files[i]
                        ):
                            temp_block = type(
                                "obj",
                                (object,),
                                {
                                    "songs": [song],
                                    "outro_audio": song_block.outro_audio_files[i],
                                    "block_id": f"{song_block.block_id}_song_{i}",
                                },
                            )()
                            await self._play_mixed_outro_sequence(temp_block)
                        else:
                            logger.warning(
                                f"No outro audio for song {i}: {song.artist} - {song.title}"
                            )
                            await self._play_song_normally(song)
                else:
                    logger.warning(
                        "individual_outros block not eligible for outro crossfade mix, playing songs normally"
                    )
                    await self._play_song_list_with_crossfade(song_block.songs)

        elif (
            song_block.block_type == "crossfade"
            or song_block.block_type == "silent_block"
        ):
            # Handle silent blocks based on crossover type
            if song_block.block_type == "silent_block":
                if song_block.crossover_type == "auto_crossfade":
                    # Create crossfaded transitions between songs
                    await self._play_crossfaded_song_block(song_block)
                elif song_block.crossover_type == "hard_cut":
                    # Play songs with hard cuts (no crossfading)
                    for song in song_block.songs:
                        await self._play_song_normally(song)
                else:
                    # Default fallback for other crossover types
                    await self._play_song_list_with_crossfade(song_block.songs)
            else:
                # Legacy crossfade block type - play songs normally
                await self._play_song_list_with_crossfade(song_block.songs)

        else:
            # Default: play songs normally
            await self._play_song_list_with_crossfade(song_block.songs)

        logger.info(f"Completed song block: {song_block.block_id}")

    async def _play_dj_talk_segment(self, segment) -> None:
        """Play a DJ talk segment."""
        talk_segment = segment.content

        logger.debug(
            f"DJ TALK SEGMENT DEBUG - ID: {talk_segment.segment_id}, Type: {talk_segment.talk_type}, Audio File: {getattr(talk_segment, 'audio_file', 'MISSING')}"
        )

        if not hasattr(talk_segment, "audio_file") or not talk_segment.audio_file:
            logger.warning(
                f"No audio available for DJ talk segment: {talk_segment.segment_id} (audio_file: {getattr(talk_segment, 'audio_file', 'MISSING ATTRIBUTE')})"
            )
            raise RuntimeError(f"No audio for scheduled talk {talk_segment.segment_id}")

        # Queue the DJ talk audio
        logger.info(
            f"Queuing DJ talk: {talk_segment.talk_type} - {talk_segment.audio_file}"
        )
        await self._play_dj_audio(talk_segment.audio_file)

    async def _play_song_normally(self, song) -> None:
        """Play a song without any crossover effects - just queue it normally."""
        metadata = {
            "title": song.title,
            "artist": song.artist,
            "album": getattr(song, "album", "Unknown Album"),
            "year": str(song.year) if song.year else "",
            "content_type": "MUSIC",
            "file_path": str(song.file_path),
        }

        logger.info(f"🎵 Playing song normally: {song.artist} - {song.title}")
        await self._queue_audio(song.file_path, metadata)

    async def _play_song_list_with_crossfade(self, songs) -> None:
        if not songs:
            return
        if len(songs) == 1:
            await self._play_song_normally(songs[0])
            return

        crossfade_duration = self._get_safe_crossfade_duration(songs)
        await self._create_and_play_crossfaded_mix(
            songs, crossfade_duration=crossfade_duration
        )

    def _build_remainder_song(self, song, remainder_path: str, duration: float):
        """Build a song-like object for a remainder file."""
        return self._build_song_for_mix(song, remainder_path, duration)

    def _build_song_for_mix(self, song, file_path: str, duration: float):
        """Build a song-like object with an overridden file and duration."""
        return SimpleNamespace(
            title=song.title,
            artist=song.artist,
            album=getattr(song, "album", "Unknown Album"),
            year=getattr(song, "year", None),
            duration=float(duration or 0.0),
            file_path=file_path,
        )

    def _get_safe_crossfade_duration(self, songs) -> float | None:
        """Pick a crossfade duration that won't exceed the shortest track."""
        crossfade_duration = None
        if self.config_manager and self.config_manager.station_config:
            crossfade_duration = float(
                self.config_manager.station_config.crossfade_duration_seconds
            )

        durations = [
            float(getattr(song, "duration", 0.0) or 0.0) for song in songs or []
        ]
        durations = [duration for duration in durations if duration > 0.0]
        if not durations:
            return crossfade_duration

        max_allowed = max(0.1, min(durations) * 0.5)
        if crossfade_duration is None:
            return max_allowed
        return min(crossfade_duration, max_allowed)

    async def _play_crossfaded_song_block(self, song_block) -> None:
        """Play a block of songs with crossfading between them."""
        if not song_block.songs:
            return

        logger.info(f"🎛️ Playing crossfaded song block: {len(song_block.songs)} songs")

        # For single song, no crossfading needed
        if len(song_block.songs) == 1:
            await self._play_song_normally(song_block.songs[0])
            return

        # For multiple songs, create a combined crossfaded mix
        crossfade_duration = self._get_safe_crossfade_duration(song_block.songs)
        await self._create_and_play_crossfaded_mix(
            song_block.songs, crossfade_duration=crossfade_duration
        )

    async def _create_and_play_crossfaded_mix(
        self, songs, crossfade_duration: float | None = None
    ) -> None:
        """Create a single mixed file with crossfades between all songs using AudioMixer."""
        try:
            logger.info(f"🎛️ Creating crossfaded mix of {len(songs)} songs")

            # Prepare song file paths
            song_files = [str(song.file_path) for song in songs]

            # Use AudioMixer to create the crossfade
            result = await self.audio_mixer.create_crossfade_mix(
                song_files, crossfade_duration=crossfade_duration
            )

            if result.success:
                crossfade_duration = 0.0
                if result.details:
                    try:
                        crossfade_duration = float(
                            result.details.get("crossfade_duration", 0.0) or 0.0
                        )
                    except (TypeError, ValueError):
                        crossfade_duration = 0.0
                crossfade_duration = max(0.0, crossfade_duration)

                mix_context = [
                    {"title": song.title, "artist": song.artist} for song in songs
                ]

                def _build_song_metadata(song, position: int) -> dict[str, Any]:
                    return {
                        "title": song.title,
                        "artist": song.artist,
                        "album": getattr(song, "album", "Unknown Album"),
                        "year": str(song.year) if song.year else "",
                        "content_type": "MUSIC",
                        "is_mix": True,
                        "mix_context": mix_context,
                        "mix_length": len(songs),
                        "mix_position": position + 1,
                        "mix_crossfade_duration": crossfade_duration,
                        "mix_label": f"Crossfade Mix ({len(songs)} tracks)",
                        "file_path": str(
                            song.file_path
                        ),  # Add original file path for artwork extraction
                    }

                metadata_schedule: list[dict[str, Any]] = []
                accumulated_offset = 0.0

                for index in range(1, len(songs)):
                    previous_song = songs[index - 1]
                    previous_duration = float(getattr(previous_song, "duration", 0.0))
                    accumulated_offset += max(
                        0.0, previous_duration - crossfade_duration
                    )

                    metadata_schedule.append(
                        {
                            "offset": round(accumulated_offset, 3),
                            "metadata": _build_song_metadata(songs[index], index),
                        }
                    )

                metadata = _build_song_metadata(songs[0], 0)

                if metadata_schedule:
                    logger.debug(
                        "Scheduled crossfade metadata offsets: %s",
                        [entry["offset"] for entry in metadata_schedule],
                    )

                logger.info(f"✅ Crossfaded mix created: {result.output_file}")
                await self._queue_audio(
                    result.output_file,
                    metadata,
                    metadata_schedule=metadata_schedule,
                )
            else:
                logger.error(
                    f"❌ Crossfaded mix creation failed: {result.error_message}, falling back to normal playback"
                )
                for song in songs:
                    await self._play_song_normally(song)

        except Exception as e:
            logger.error(f"❌ Error creating crossfaded mix: {e}")
            # Fallback to normal playback
            for song in songs:
                await self._play_song_normally(song)

    async def _play_mixed_outro_sequence(self, song_block) -> None:
        """Play songs with mixed outro DJ talk over the final song."""
        if not song_block.songs:
            return

        # Play all songs normally except the last one
        for song in song_block.songs[:-1]:
            await self._play_song_normally(song)

        # Handle the last song with outro mixing
        last_song = song_block.songs[-1]
        outro_audio_file = getattr(song_block, "outro_audio", None)

        if outro_audio_file:
            # Create mixed outro for the last song
            metadata = {
                "title": last_song.title,
                "artist": last_song.artist,
                "album": getattr(last_song, "album", "Unknown Album"),
                "year": str(last_song.year) if last_song.year else "",
                "content_type": "MUSIC",
            }

            logger.info(
                f"🎵🎙️ Creating mixed outro sequence: DJ talk over {last_song.artist} - {last_song.title}"
            )

            # Create mixed outro file
            mixed_outro = await self._create_mixed_outro_file(
                last_song.file_path, outro_audio_file, metadata
            )

            if mixed_outro:
                # Queue the mixed outro (song with DJ overlay)
                outro_metadata = {
                    "title": last_song.title,
                    "artist": last_song.artist,
                }
                await self._queue_audio(mixed_outro, outro_metadata)

            else:
                logger.warning("Failed to create mixed outro, playing song normally")
                await self._play_song_normally(last_song)
        else:
            # No outro audio, just play normally
            await self._play_song_normally(last_song)

    async def _play_mixed_intro_sequence(self, song_block) -> None:
        """Play mixed intro followed by song remainder as separate timeline items."""
        if not song_block.songs:
            return

        song = song_block.songs[0]  # Handle first song with intro
        dj_audio_file = getattr(song_block, "intro_audio", None)

        if not dj_audio_file:
            # No DJ audio, just play normally
            await self._play_song_normally(song)
            return

        metadata = {
            "title": song.title,
            "artist": song.artist,
            # "album": song.album,
            "year": str(song.year) if song.year else "",
            "content_type": "MUSIC",
        }

        logger.info(
            f"🎵🎙️ Creating mixed intro sequence: DJ talk over {song.artist} - {song.title}"
        )

        # Create mixed intro file
        mixed_intro = await self._create_mixed_intro_file(
            song.file_path, dj_audio_file, metadata
        )

        if mixed_intro:
            # Get the ACTUAL intro duration from the created file to ensure perfect alignment
            intro_section_duration = await self._get_audio_duration_ffprobe(mixed_intro)
            song_duration = await self._get_audio_duration_ffprobe(song.file_path)

            # Validate that we have enough song left for a meaningful remainder
            remainder_duration = song_duration - intro_section_duration
            if remainder_duration < MIN_SONG_REMAINDER_SECONDS:
                logger.warning(
                    f"⚠️ Song remainder too short ({remainder_duration:.1f}s), playing full intro only"
                )
                # Just play the intro file which includes more of the song
                intro_metadata = metadata.copy()
                await self._queue_audio(mixed_intro, intro_metadata)
                remaining_songs = song_block.songs[1:]
                if remaining_songs:
                    await self._play_song_list_with_crossfade(remaining_songs)
                return

            # Create song remainder (from intro end to song end)
            song_remainder = await self._create_song_remainder_file(
                song.file_path, intro_section_duration, metadata
            )

            # Wait for queue to have space before adding multiple items
            current_queue_size = self.continuous_audio.get_queue_size()
            if current_queue_size > 1:
                logger.info(
                    f"⏱️ Waiting for queue space (current: {current_queue_size})"
                )
                # Wait a bit for queue to clear
                await asyncio.sleep(QUEUE_WAIT_DELAY_SECONDS)

            # Queue mixed intro first
            intro_metadata = metadata.copy()
            await self._queue_audio(mixed_intro, intro_metadata)

            intro_duration_actual = await self._get_audio_duration_ffprobe(mixed_intro)
            logger.info(
                f"✅ Queued mixed intro: {song.artist} - {song.title} (duration: {intro_duration_actual:.1f}s)"
            )

            # Small delay to ensure intro is properly queued before remainder
            await asyncio.sleep(INTRO_QUEUE_DELAY_SECONDS)

            # Queue song remainder (optionally crossfade into the remaining songs)
            if song_remainder:
                remainder_duration_actual = await self._get_audio_duration_ffprobe(
                    song_remainder
                )
                remaining_songs = song_block.songs[1:]

                if remaining_songs:
                    remainder_song = self._build_remainder_song(
                        song, song_remainder, remainder_duration_actual
                    )
                    mix_songs = [remainder_song] + remaining_songs
                    crossfade_duration = self._get_safe_crossfade_duration(mix_songs)
                    await self._create_and_play_crossfaded_mix(
                        mix_songs, crossfade_duration=crossfade_duration
                    )
                    logger.info(
                        f"✅ Queued crossfaded remainder mix: {song.artist} - {song.title} + {len(remaining_songs)} song(s)"
                    )
                else:
                    remainder_metadata = metadata.copy()
                    await self._queue_audio(song_remainder, remainder_metadata)

                    total_duration = intro_duration_actual + remainder_duration_actual
                    logger.info(
                        f"✅ Queued song remainder: {song.artist} - {song.title} (duration: {remainder_duration_actual:.1f}s, total: {total_duration:.1f}s)"
                    )

                # Verify both items are in queue
                final_queue_size = self.continuous_audio.get_queue_size()
                logger.info(
                    f"📊 Final queue size after mixed sequence: {final_queue_size}"
                )
            else:
                raise RuntimeError("Song remainder creation failed; retry entire block")
        else:
            # Fallback to original song if mixing fails
            logger.warning("⚠️ Mixing failed, playing original song")
            await self._queue_audio(song.file_path, metadata)
            remaining_songs = song_block.songs[1:]
            if remaining_songs:
                await self._play_song_list_with_crossfade(remaining_songs)

        # Store current metadata
        self.current_song_metadata = metadata

        # Remaining songs are handled within the intro sequence when possible.

    async def _create_full_intro_song_file(
        self, song, intro_audio_file
    ) -> tuple[str, float] | None:
        """Create a full song file with DJ intro mixed over the start."""
        metadata = {
            "title": song.title,
            "artist": song.artist,
            "year": str(song.year) if getattr(song, "year", None) else "",
            "content_type": "MUSIC",
        }

        mixed_intro = await self._create_mixed_intro_file(
            song.file_path, intro_audio_file, metadata
        )
        if not mixed_intro:
            return None

        intro_duration = await self._get_audio_duration_ffprobe(mixed_intro)
        if intro_duration <= 0:
            return None

        song_remainder = await self._create_song_remainder_file(
            song.file_path, intro_duration, metadata
        )
        if not song_remainder:
            logger.warning(
                f"⚠️ Failed to create remainder for intro mix: {song.artist} - {song.title}"
            )
            return mixed_intro, intro_duration

        concat_result = await self.audio_mixer.concatenate_audio(
            [mixed_intro, song_remainder]
        )
        if concat_result.success:
            return concat_result.output_file, concat_result.duration

        logger.warning(
            f"⚠️ Failed to concatenate intro + remainder for {song.artist} - {song.title}: {concat_result.error_message}"
        )
        return mixed_intro, intro_duration

    async def _create_full_outro_song_file(
        self, song, outro_audio_file
    ) -> tuple[str, float] | None:
        """Create a full song file with DJ outro mixed over the end."""
        metadata = {
            "title": song.title,
            "artist": song.artist,
            "album": getattr(song, "album", "Unknown Album"),
            "year": str(song.year) if getattr(song, "year", None) else "",
            "content_type": "MUSIC",
        }

        result = await self.audio_mixer.create_outro_mix(
            song.file_path, outro_audio_file, metadata
        )
        if not result.success:
            logger.warning(
                f"❌ Outro mixing failed: {result.error_message} - using fallback"
            )
            return None

        duration = await self._get_audio_duration_ffprobe(result.output_file)
        if duration <= 0:
            return None

        return result.output_file, duration

    async def _create_mixed_intro_file(
        self, song_path: str, dj_audio_path: str, metadata: dict
    ) -> str | None:
        """Create a professionally mixed file with DJ talk over song intro using AudioMixer."""
        try:
            logger.info("🎛️ MIXING: Creating professional radio intro mix...")
            logger.info(f"  🎵 Song: {metadata.get('title', 'Unknown')}")
            logger.info(f"  🎙️ DJ Audio: {dj_audio_path}")

            result = await self.audio_mixer.create_intro_mix(
                song_path, dj_audio_path, metadata
            )

            if result.success:
                logger.info(
                    f"✅ PROFESSIONAL MIX CREATED: {result.output_file} (duration: {result.duration:.1f}s)"
                )
                return result.output_file
            else:
                logger.warning(
                    f"❌ Intro mixing failed: {result.error_message} - using fallback"
                )
                return None

        except Exception as e:
            logger.warning(f"❌ Error creating mixed intro file: {e} - radio continues")
            return None

    async def _get_audio_duration_ffprobe(self, file_path: str) -> float:
        """Get audio duration using ffprobe."""
        from .audio_utils import get_audio_duration

        duration = await get_audio_duration(file_path)
        return duration if duration > 0 else DEFAULT_AUDIO_DURATION_SECONDS

    async def _create_mixed_outro_file(
        self, song_file: str, dj_audio_file: str, metadata: dict
    ) -> str | None:
        """Create a mixed outro file with DJ talk over the song ending using AudioMixer."""
        try:
            logger.info("🎛️ MIXING: Creating professional radio outro mix...")
            logger.info(f"  🎵 Song: {metadata.get('title', 'Unknown')}")
            logger.info(f"  �️ DJ Audio: {dj_audio_file}")

            result = await self.audio_mixer.create_outro_mix(
                song_file, dj_audio_file, metadata
            )

            if result.success:
                logger.info(
                    f"✅ PROFESSIONAL OUTRO MIX CREATED: {result.output_file} (duration: {result.duration:.1f}s)"
                )
                return result.output_file
            else:
                logger.warning(
                    f"❌ Outro mixing failed: {result.error_message} - using fallback"
                )
                return None

        except Exception as e:
            logger.warning(f"❌ Error creating mixed outro file: {e} - radio continues")
            return None

    async def _create_auto_crossfade_file(
        self, song_file: str, metadata: dict
    ) -> str | None:
        """Create an auto-crossfaded file for seamless song transitions."""
        result = await self.audio_mixer.create_auto_crossfade(song_file, metadata)
        if result.success:
            return result.output_file
        return None

    async def _create_song_remainder_file(
        self, song_path: str, skip_seconds: float, metadata: dict
    ) -> str | None:
        """Create a file with the song starting from skip_seconds using AudioMixer."""
        try:
            logger.info(f"🎵 Creating song remainder from {skip_seconds:.1f}s")

            result = await self.audio_mixer.create_audio_remainder(
                song_path, skip_seconds, metadata, audio_type="song"
            )

            if result.success:
                logger.info(
                    f"✅ SONG REMAINDER CREATED: {result.output_file} (duration: {result.duration:.1f}s)"
                )
                return result.output_file
            else:
                logger.error(f"❌ Song remainder failed: {result.error_message}")
                return None

        except Exception as e:
            logger.error(f"❌ Error creating song remainder file: {e}")
            return None

    async def _create_dj_remainder_file(
        self, dj_audio_path: str, skip_seconds: float, metadata: dict
    ) -> str | None:
        """Create a file with DJ audio starting from skip_seconds using AudioMixer."""
        try:
            logger.info(f"🎙️ Creating DJ remainder from {skip_seconds:.1f}s")

            result = await self.audio_mixer.create_audio_remainder(
                dj_audio_path, skip_seconds, metadata, audio_type="dj"
            )

            if result.success:
                logger.info(
                    f"✅ DJ REMAINDER CREATED: {result.output_file} (duration: {result.duration:.1f}s)"
                )
                return result.output_file
            else:
                logger.error(f"❌ DJ remainder failed: {result.error_message}")
                return None

        except Exception as e:
            logger.error(f"❌ Error creating DJ remainder file: {e}")
            return None

    async def _cleanup_temp_file(self, file_path: str, delay: int = 300):
        """Clean up temporary mixed audio files after use."""
        try:
            await asyncio.sleep(delay)
            path = Path(file_path)
            if path.exists():
                path.unlink()
                logger.debug(f"🧹 Cleaned up temp file: {file_path}")
        except Exception as e:
            logger.warning(f"Failed to cleanup temp file {file_path}: {e}")

    def _cleanup_files_older_than(
        self,
        directory: Path,
        patterns: list[str],
        cutoff_time: float,
        protected_names: set[str] | None = None,
    ) -> tuple[int, int]:
        files_cleaned = 0
        total_size_cleaned = 0
        protected = protected_names or set()

        for pattern in patterns:
            for file in directory.glob(pattern):
                if file.name in protected:
                    continue
                if not file.is_file():
                    continue

                try:
                    stats = file.stat()
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    logger.warning(f"Failed to stat file {file.name}: {exc}")
                    continue

                if stats.st_mtime >= cutoff_time:
                    continue

                try:
                    file.unlink()
                    files_cleaned += 1
                    total_size_cleaned += stats.st_size
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    logger.warning(f"Failed to clean up temp file {file.name}: {exc}")

        return files_cleaned, total_size_cleaned

    def _protected_audio_names(self) -> set[str]:
        """Return audio basenames still needed by scheduled or queued content."""
        paths = self.continuous_audio.get_audio_files_in_use()
        timeline = (
            self.timeline_scheduler.current_timeline
            if self.timeline_scheduler
            else None
        )
        for item in timeline.items if timeline else []:
            if item.status == "completed":
                continue
            for name in (
                "audio_file",
                "file_path",
                "intro_audio",
                "outro_audio",
                "prepared_mix_file",
            ):
                value = getattr(item.content, name, None)
                if value:
                    paths.add(value)
            for name in ("intro_audio_files", "outro_audio_files"):
                paths.update(
                    value for value in getattr(item.content, name, None) or [] if value
                )
        for part in getattr(self, "_pending_timeline_audio", None) or []:
            paths.add(part["file_path"])
        return {Path(path).name for path in paths}

    async def _cleanup_temp_artifacts(self) -> tuple[int, int]:
        temp_dir = Path("/app/temp-audio")
        if not temp_dir.exists():
            return 0, 0

        current_time = time.time()
        audio_cutoff = current_time - CLEANUP_AGE_SECONDS
        artwork_cutoff = current_time - ARTWORK_CLEANUP_AGE_SECONDS

        files_cleaned = 0
        total_size_cleaned = 0

        audio_count, audio_size = await asyncio.to_thread(
            self._cleanup_files_older_than,
            temp_dir,
            ["*.mp3"],
            audio_cutoff,
            self._protected_audio_names(),
        )
        files_cleaned += audio_count
        total_size_cleaned += audio_size

        artwork_dir = temp_dir / "artwork"
        if artwork_dir.exists():
            artwork_count, artwork_size = await asyncio.to_thread(
                self._cleanup_files_older_than,
                artwork_dir,
                ["*.jpg", "*.png", "*.notfound"],
                artwork_cutoff,
                {"current.jpg", PLACEHOLDER_ARTWORK_FILENAME},
            )
            files_cleaned += artwork_count
            total_size_cleaned += artwork_size

        return files_cleaned, total_size_cleaned

    async def _periodic_cleanup_task(self) -> None:
        """Periodically clean up old temporary audio files."""
        while self.is_running:
            try:
                # Run cleanup every 30 minutes
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

                files_cleaned, total_size_cleaned = await self._cleanup_temp_artifacts()

                if files_cleaned > 0:
                    size_mb = total_size_cleaned / (1024 * 1024)
                    logger.info(
                        f"🧹 Cleanup completed: Removed {files_cleaned} old files ({size_mb:.2f} MB)"
                    )

            except asyncio.CancelledError:
                logger.info("🧹 Cleanup task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in periodic cleanup task: {e}", exc_info=True)
                await asyncio.sleep(60)  # Wait a bit before retrying

    async def _listener_monitoring_loop(self) -> None:
        """Periodically check listener count and cache it."""
        while self.is_running:
            try:
                # Get viewer count from web-interface (video + proxied audio)
                video_viewers = await self._get_video_viewer_count()
                direct_audio_listeners = self.get_direct_audio_listener_count()
                total_count = video_viewers + direct_audio_listeners

                # Only log if count changed
                if total_count != self.cached_listener_count:
                    logger.info(
                        "👥 Listener count: %s (web=%s, direct_audio=%s)",
                        total_count,
                        video_viewers,
                        direct_audio_listeners,
                    )
                    self.cached_listener_count = total_count

                # Wait before next check
                await asyncio.sleep(LISTENER_CHECK_INTERVAL_SECONDS)

            except asyncio.CancelledError:
                logger.info("👥 Listener monitoring task cancelled")
                break
            except Exception as e:
                logger.error(f"Error checking listener count: {e}")
                await asyncio.sleep(60)  # Wait a bit before retrying

    async def _get_video_viewer_count(self) -> int:
        """Get the current number of video viewers from web-interface."""
        try:
            import aiohttp

            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    "http://web-interface:3000/api/video-viewers",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as response,
            ):
                if response.status == 200:
                    data = await response.json()
                    return data.get("count", 0)
                return 0
        except Exception:
            # Don't log every error to avoid spam - video viewer tracking is optional
            return 0

    def track_direct_audio_listener(self, client_ip: str | None) -> None:
        if not client_ip:
            return
        self.direct_audio_listener_access[client_ip] = time.time()

    def get_direct_audio_listener_count(self) -> int:
        now = time.time()
        stale = [
            ip
            for ip, last_access in self.direct_audio_listener_access.items()
            if now - last_access > DIRECT_AUDIO_LISTENER_TIMEOUT_SECONDS
        ]
        for ip in stale:
            self.direct_audio_listener_access.pop(ip, None)
        return len(self.direct_audio_listener_access)

    def get_listener_count(self) -> int:
        """Get the cached listener count."""
        return self.cached_listener_count

    async def _run_immediate_cleanup(self) -> None:
        """Run cleanup immediately on startup."""
        try:
            files_cleaned, total_size_cleaned = await self._cleanup_temp_artifacts()

            if files_cleaned > 0:
                size_mb = total_size_cleaned / (1024 * 1024)
                logger.info(
                    f"🧹 STARTUP CLEANUP: Removed {files_cleaned} old temp files ({size_mb:.1f} MB)"
                )
            else:
                logger.info("🧹 STARTUP CLEANUP: No old temp files found")

        except Exception as e:
            logger.error(f"Error in startup cleanup: {e}")

    def _on_audio_item_finished(
        self, metadata: dict[str, Any], actual_duration: float
    ) -> None:
        """Producer callback when an audio item finishes streaming.

        Compound items trigger one callback per constituent audio file.
        Only the final part completes the timeline item and releases its song
        reservations. The on-air duration passed to skew logging belongs to
        that final audio file.

        Args:
            metadata: Metadata of the audio file that just finished.
            actual_duration: Seconds the producer streamed that file.
        """
        timeline_id = metadata.get("timeline_id") if metadata else None
        if not timeline_id:
            return

        timeline = (
            self.timeline_scheduler.current_timeline
            if self.timeline_scheduler
            else None
        )
        if not timeline:
            return

        tz = self.config_manager.timezone
        finished_at = datetime.now(tz)
        for item in timeline.items:
            if item.timeline_id != timeline_id:
                continue
            item.remaining_audio_parts = max(0, item.remaining_audio_parts - 1)
            if item.remaining_audio_parts:
                return
            item.status = "completed"
            item.actual_end = finished_at
            if item.item_type == "song_block" and self.program_scheduler:
                self.program_scheduler.recent_tracker.release_reservation(
                    item.content.block_id
                )
            _log_skew_at_completion(
                item, source="audio_finished", on_air_seconds=actual_duration
            )
            return

    def get_on_air_metadata(self) -> dict[str, Any]:
        """Get the metadata of the audio that is streaming right now.

        Every queued audio file is tagged with its timeline item, and the
        producer republishes the tag — plus the track title, artist and start
        time — whenever it starts streaming something new. That is what
        listeners actually hear, unlike item status, which flips to "playing"
        as soon as an item is handed to the producer's queue.

        Returns:
            The on-air metadata, or an empty dict if nothing is streaming.
        """
        if not self.continuous_audio:
            return {}
        return self.continuous_audio.get_current_metadata()

    def get_on_air_remaining(self) -> float:
        """Get how long the audio currently on air has left.

        Returns:
            Seconds until the audio on air changes, or 0.0 when idle.
        """
        if not self.continuous_audio:
            return 0.0
        return self.continuous_audio.get_on_air_remaining()

    def get_on_air_timeline_id(self) -> str | None:
        """Get the timeline_id of the audio that is streaming right now.

        Returns:
            The on-air timeline_id, or None if nothing is streaming.
        """
        return self.get_on_air_metadata().get("timeline_id")

    async def _queue_audio(
        self,
        file_path: str,
        metadata: dict[str, Any],
        metadata_schedule: list[dict[str, Any]] | None = None,
        timeline_id: str | None = None,
    ) -> None:
        """Queue audio while tagging it with the active timeline item if available."""
        effective_timeline_id = timeline_id or self._active_timeline_queue_id
        metadata_payload = metadata.copy()
        if effective_timeline_id:
            metadata_payload["timeline_id"] = effective_timeline_id

        schedule_payload: list[dict[str, Any]] | None = None
        if metadata_schedule:
            schedule_payload = []
            for entry in metadata_schedule:
                entry_copy = entry.copy()
                metadata_copy = (entry_copy.get("metadata") or {}).copy()
                if effective_timeline_id:
                    metadata_copy["timeline_id"] = effective_timeline_id
                entry_copy["metadata"] = metadata_copy
                schedule_payload.append(entry_copy)

        pending = getattr(self, "_pending_timeline_audio", None)
        if pending is not None:
            pending.append(
                {
                    "file_path": file_path,
                    "metadata": metadata_payload,
                    "metadata_schedule": schedule_payload,
                }
            )
            return
        await self.continuous_audio.queue_audio_file(
            file_path,
            metadata_payload,
            metadata_schedule=schedule_payload,
        )

    async def _queue_startup_song(self) -> None:
        """Queue a single random song immediately on startup.

        Fills the gap while the first timeline items (which may need TTS generation)
        are being prepared, so there is no silence at the start of the broadcast.
        """
        tracks = self.music_library.get_all_tracks()
        if not tracks:
            logger.warning("No tracks available for startup song — skipping")
            return

        track = random.choice(tracks)
        metadata: dict[str, Any] = {
            "title": track.metadata.get("title", "Unknown"),
            "artist": track.metadata.get("artist", "Unknown"),
            "album": track.metadata.get("album", ""),
            "content_type": "SONG",
        }
        logger.info(
            "🎵 Queuing startup song: %s - %s",
            metadata["artist"],
            metadata["title"],
        )
        await self._queue_audio(track.file_path, metadata)

    async def _play_dj_audio(
        self, audio_file: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Play DJ audio (TTS generated speech)."""
        logger.info(f"🎙️ QUEUING DJ AUDIO: {audio_file}")

        # Verify the file exists first
        from pathlib import Path

        if not Path(audio_file).exists():
            raise FileNotFoundError(audio_file)

        # Queue DJ audio with appropriate metadata
        dj_metadata = {
            "title": "DJ Talk",
            "artist": f"{self._station_name()} DJ",
            "album": "",
            "year": "",
            "content_type": "DJ_TALK",
        }
        if metadata:
            dj_metadata.update(metadata)

        await self._queue_audio(audio_file, dj_metadata)
        logger.info(f"✅ DJ AUDIO QUEUED: {audio_file}")

    def _get_jingle_files(self, jingles_folder: str) -> list[Path]:
        """Get all jingle files from the specified folder."""
        if not jingles_folder:
            return []

        jingles_path = Path("/app/jingles") / jingles_folder
        if not jingles_path.exists():
            logger.warning(f"Jingles folder not found: {jingles_path}")
            return []

        return list(jingles_path.glob("*.mp3"))
