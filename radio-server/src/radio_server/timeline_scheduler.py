"""Timeline-based scheduler — plans what plays when, with advance coordination."""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from .advanced_program_scheduler import (
    AdvancedProgramScheduler,
    DJTalkSegment,
    JingleSegment,
    ScheduleSegment,
    SongBlock,
)

logger = logging.getLogger(__name__)


@dataclass
class TimelineItem:
    """A single item in the timeline with precise timing"""

    timeline_id: str
    item_type: str  # "song_block", "dj_talk", "jingle", "dj_transition_start", "dj_transition_end"
    content: Any  # Song, DJTalkSegment, SongBlock, etc.
    scheduled_start: datetime
    estimated_duration: float
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    status: str = (
        "scheduled"  # scheduled, preparing, ready, queued, playing, completed, failed
    )
    preparation_progress: dict[str, Any] | None = None
    dj_id: str = ""
    next_dj_id: str | None = None  # For transition items, the DJ coming next
    not_before: datetime | None = None  # Hard lower bound for show handovers
    retry_at: datetime | None = None
    recorded_song_keys: set[str] = field(default_factory=set)
    remaining_audio_parts: int = 0

    def __post_init__(self):
        if self.preparation_progress is None:
            self.preparation_progress = {}

    @property
    def scheduled_end(self) -> datetime:
        """Calculate the scheduled end time based on start time and duration"""
        return self.scheduled_start + timedelta(seconds=self.estimated_duration)


@dataclass
class Timeline:
    """Complete timeline for a radio show"""

    timeline_id: str
    dj_id: str
    show_start: datetime
    show_end: datetime
    items: list[TimelineItem]
    created_at: datetime
    last_updated: datetime
    preparation_window: float = 90.0  # Prepare items 90 seconds in advance


class TimelineScheduler:
    """Timeline-based scheduler that plans shows in advance"""

    def __init__(
        self,
        advanced_scheduler: AdvancedProgramScheduler,
        timezone=None,
        config_manager=None,
    ):
        self.advanced_scheduler = advanced_scheduler
        self.timezone = timezone
        self.config_manager = config_manager
        self.current_timeline: Timeline | None = None
        self.preparation_tasks: dict[str, asyncio.Task] = {}
        self.is_running = False
        self._bed_duration_cache: dict[str, float] = {}
        self._sting_last_time_by_dj: dict[str, datetime] = {}
        self._consecutive_extension_failures = 0
        self._next_extension_retry_at: datetime | None = None
        # Callback to switch DJs - set by radio_station
        self.on_dj_switch = None

        logger.info("Timeline scheduler initialized")

    def _now(self) -> datetime:
        """Get timezone-aware current time"""
        if self.timezone:
            return datetime.now(self.timezone)
        return datetime.now()

    def _can_attempt_extension(self, current_time: datetime) -> bool:
        """Return True when extension backoff has elapsed."""
        if self._next_extension_retry_at is None:
            return True
        return current_time >= self._next_extension_retry_at

    def _record_extension_result(
        self, items_added: int, current_time: datetime
    ) -> None:
        """Track extension success/failure and apply retry backoff after failures."""
        if items_added > 0:
            self._consecutive_extension_failures = 0
            self._next_extension_retry_at = None
            return

        self._consecutive_extension_failures += 1
        backoff_seconds = min(300, self._consecutive_extension_failures * 30)
        self._next_extension_retry_at = current_time + timedelta(
            seconds=backoff_seconds
        )
        logger.warning(
            f"⚠️ Timeline extension added 0 items. Retrying in {backoff_seconds}s "
            f"(failure #{self._consecutive_extension_failures})."
        )

    def _calculate_show_end(self, reference_time: datetime, entry) -> datetime:
        """Compute the absolute datetime when a schedule slot ends.

        For overnight slots (end_time < start_time) the end falls on the next
        calendar day only when reference_time is in the evening portion of the
        slot. Same-day slots always end on reference_time's date — never push
        +1 day just because the end has already passed; that's an upstream
        skew issue, not an overnight signal.
        """
        tz = self.config_manager.timezone
        end_dt = tz.localize(datetime.combine(reference_time.date(), entry.end_time))
        if (
            entry.end_time <= entry.start_time
            and reference_time.time() >= entry.start_time
        ):
            end_dt = end_dt + timedelta(days=1)
        return end_dt

    def _recover_timeline_show_end(self, timeline: Timeline) -> bool:
        """Recover by extending in order, retaining every intervening handover."""
        old_end = timeline.show_end
        self.extend_timeline(timeline)
        return timeline.show_end > old_end

    def create_show_timeline_mid_show(
        self,
        dj_id: str,
        timeline_start: datetime,
        end_time: datetime,
        actual_show_start: datetime,
        look_ahead_minutes: int = 1,
    ) -> Timeline:
        """Create a timeline starting mid-show"""
        return self._create_timeline(
            dj_id, timeline_start, end_time, actual_show_start, look_ahead_minutes
        )

    def create_show_timeline(
        self,
        dj_id: str,
        start_time: datetime,
        end_time: datetime,
        look_ahead_minutes: int = 1,
    ) -> Timeline:
        """Create a complete timeline for a DJ show from the beginning"""
        return self._create_timeline(
            dj_id, start_time, end_time, start_time, look_ahead_minutes
        )

    def _create_timeline(
        self,
        dj_id: str,
        timeline_start: datetime,
        end_time: datetime,
        actual_show_start: datetime,
        look_ahead_minutes: int = 1,
    ) -> Timeline:
        """Create a timeline - internal method"""
        logger.info(
            f"🎯 Creating timeline for {dj_id} from {timeline_start} to {end_time}"
        )

        timeline_id = f"timeline_{dj_id}_{int(timeline_start.timestamp())}"
        timeline = Timeline(
            timeline_id=timeline_id,
            dj_id=dj_id,
            show_start=actual_show_start,  # Actual show start time
            show_end=end_time,
            items=[],
            created_at=self._now(),
            last_updated=self._now(),
        )

        # Start the advanced scheduler for this planning window
        # Use timeline_start (now) for remaining time calculation, but pass actual_show_start
        # so the scheduler knows how far into the show we already are
        self.advanced_scheduler.start_dj_show(
            dj_id, timeline_start, end_time, actual_show_start
        )

        # Plan the initial timeline (first few minutes or until show end)
        plan_until = min(
            end_time, timeline_start + timedelta(minutes=look_ahead_minutes)
        )
        current_time = timeline_start

        # Transitions are handled by radio_station at the actual hand-off time, so
        # we only need to fill the timeline with music, jingles, and DJ talk

        # Plan segments until the target time (just-in-time planning)
        while current_time < plan_until:
            remaining_seconds = (end_time - current_time).total_seconds()
            if remaining_seconds < 60:  # Less than 1 minute left
                break

            # Get next segment(s) from advanced scheduler (returns list)
            segments = self.advanced_scheduler.get_next_segment()
            if not segments:
                logger.warning("No more segments available from advanced scheduler")
                break

            # Track if we added any segments in this iteration
            added_any = False

            # Calculate total duration of all segments in the group
            total_segments_duration = sum(seg.estimated_duration for seg in segments)

            # Never discard a segment group for not fitting: once scheduled,
            # content must play. Overshooting show_end is acceptable.
            if total_segments_duration > remaining_seconds:
                logger.info(
                    f"Segment group overshoots remaining time "
                    f"({total_segments_duration:.1f}s > {remaining_seconds:.1f}s), scheduling anyway"
                )

            # All segments fit, add them all
            for segment in segments:
                # Create timeline item
                item_type = self._map_segment_type(segment.segment_type)
                timeline_item = TimelineItem(
                    timeline_id=str(uuid4()),
                    item_type=item_type,
                    content=segment.content,
                    scheduled_start=current_time,
                    estimated_duration=segment.estimated_duration,
                    dj_id=dj_id,
                )

                timeline.items.append(timeline_item)
                current_time += timedelta(seconds=segment.estimated_duration)
                remaining_seconds = (end_time - current_time).total_seconds()
                added_any = True

                logger.debug(
                    f"📅 Added {item_type} at {timeline_item.scheduled_start.strftime('%H:%M:%S')} "
                    f"({timeline_item.estimated_duration:.1f}s)"
                )

            # If we couldn't add any segments, break to avoid infinite loop
            if not added_any or total_segments_duration <= 0:
                logger.warning(
                    f"Could not add any segments with {remaining_seconds:.1f}s remaining, stopping timeline creation"
                )
                break

        self._insert_stings(timeline)

        logger.info(
            f"✅ Created timeline with {len(timeline.items)} items covering "
            f"{(current_time - timeline_start).total_seconds() / 60:.1f} minutes"
        )

        return timeline

    def extend_timeline(self, timeline: Timeline, extend_minutes: int = 15) -> Timeline:
        """Extend an existing timeline with more content.

        Handles DJ transitions seamlessly by adding transition items and continuing
        to plan for the next DJ when approaching show boundaries.
        """
        # Find the actual end time of the timeline, accounting for items that have
        # already completed or are playing (which may differ from estimated times)
        current_time = self._find_timeline_end(timeline)

        # Keep advanced scheduler's virtual clock aligned with the scheduled timeline gap we are filling
        try:
            if hasattr(self.advanced_scheduler, "virtual_current_time"):
                self.advanced_scheduler.virtual_current_time = current_time
        except Exception:
            logger.debug(
                "Could not update advanced scheduler virtual time during extension"
            )

        # Calculate extension target - may need to go past show_end if transitioning
        extend_until = current_time + timedelta(minutes=extend_minutes)

        # Check if we're approaching or past a DJ transition
        time_until_show_end = (timeline.show_end - current_time).total_seconds()
        next_dj_info = None

        # The tail must reach the boundary before a handover can be appended.
        # Never recover by replacing the DJ: even a very late show owes its
        # goodbye and the next show's introduction, in timeline order.
        if time_until_show_end <= 0 and self.config_manager:
            next_dj_info = self._get_next_dj_info(timeline)
            if next_dj_info:
                current_time = self._add_transition_items(
                    timeline, current_time, next_dj_info
                )
                if self.on_dj_switch:
                    self.on_dj_switch(
                        next_dj_info["dj_id"],
                        current_time,
                        next_dj_info["show_end"],
                        next_dj_info["music_folders"],
                    )
                timeline.dj_id = next_dj_info["dj_id"]
                timeline.show_start = next_dj_info["show_start"]
                timeline.show_end = next_dj_info["show_end"]

        # Set extend_until based on current show_end (which may have been updated)
        extend_until = min(extend_until, timeline.show_end)

        logger.info(
            f"🔄 Extending timeline from {current_time.strftime('%H:%M:%S')} "
            f"to {extend_until.strftime('%H:%M:%S')}"
        )

        items_added = 0
        while current_time < extend_until:
            # Try to get next segment(s) (could be music or DJ talk, returns list)
            segments = self.advanced_scheduler.get_next_segment()
            if not segments:
                # No segment available, stop extending
                logger.debug("No segment available, stopping extension")
                break

            # Track if we added any segments in this iteration
            added_any_in_iteration = False

            # Calculate total duration of all segments in the group
            total_segments_duration = sum(seg.estimated_duration for seg in segments)

            # Calculate remaining time until extension target
            remaining_seconds = (extend_until - current_time).total_seconds()

            # Never discard a segment group for not fitting: once scheduled,
            # content must play. Overshooting the extension target is fine.
            if total_segments_duration > remaining_seconds:
                logger.info(
                    f"Segment group overshoots extension target "
                    f"({total_segments_duration:.1f}s > {remaining_seconds:.1f}s), scheduling anyway"
                )

            # All segments fit, add them all
            for segment in segments:
                item_type = self._map_segment_type(segment.segment_type)
                timeline_item = TimelineItem(
                    timeline_id=str(uuid4()),
                    item_type=item_type,
                    content=segment.content,
                    scheduled_start=current_time,
                    estimated_duration=segment.estimated_duration,
                    dj_id=timeline.dj_id,
                )

                timeline.items.append(timeline_item)
                current_time += timedelta(seconds=segment.estimated_duration)
                items_added += 1
                added_any_in_iteration = True

            # If we couldn't add any segments, break to avoid infinite loop
            if not added_any_in_iteration or total_segments_duration <= 0:
                logger.warning(
                    "Could not add any segments during timeline extension, stopping"
                )
                break

        timeline.last_updated = self._now()
        self._insert_stings(timeline)
        logger.info(f"✅ Extended timeline with {items_added} items")

        return timeline

    def _find_timeline_end(self, timeline: Timeline) -> datetime:
        """Find the actual end time of the timeline content.

        For completed items, use actual_end if available.
        For playing items, use actual_start + estimated_duration.
        For scheduled items, use scheduled_start + estimated_duration.

        Returns the latest end time found, but never earlier than now.
        This prevents stale timelines from getting stuck in the past.
        """
        now = self._now()

        if not timeline.items:
            return now

        latest_end = None

        for item in timeline.items:
            if item.status == "completed" and item.actual_end:
                # Completed item - use actual end time
                item_end = item.actual_end
            elif item.status == "playing" and item.actual_start:
                # Playing item - use actual start + estimated duration
                item_end = item.actual_start + timedelta(
                    seconds=item.estimated_duration
                )
            elif item.status in ["ready", "queued", "scheduled", "preparing"]:
                # Future/pending item - use scheduled times
                item_end = item.scheduled_start + timedelta(
                    seconds=item.estimated_duration
                )
            else:
                # Fallback to scheduled times
                item_end = item.scheduled_start + timedelta(
                    seconds=item.estimated_duration
                )

            if latest_end is None or item_end > latest_end:
                latest_end = item_end

        # Never return a time in the past - if all items are completed/stale,
        # we should start extending from now, not from the past
        if latest_end is None or latest_end < now:
            return now

        return latest_end

    def _map_segment_type(self, segment_type: str) -> str:
        """Map advanced scheduler segment types to timeline item types"""
        mapping = {
            "song_block": "song_block",
            "dj_talk": "dj_talk",
            "jingle": "jingle",
        }
        return mapping.get(segment_type, "unknown")

    def _is_jingle_item(self, item: TimelineItem) -> bool:
        return item.item_type == "jingle" and isinstance(item.content, JingleSegment)

    def _is_sting_item(self, item: TimelineItem) -> bool:
        if not self._is_jingle_item(item):
            return False
        jingle_id = getattr(item.content, "jingle_id", "") or ""
        return jingle_id.startswith("sting_")

    def _is_pending_timeline_item(self, item: TimelineItem) -> bool:
        return item.status in ["scheduled", "preparing", "ready"]

    def _insert_stings(self, timeline: Timeline) -> None:
        if not timeline.items:
            return

        now = self._now()

        index = 0
        while index < len(timeline.items) - 1:
            previous_item = timeline.items[index]
            next_item = timeline.items[index + 1]

            # Never mutate timeline history or already queued/playing content
            if not self._is_pending_timeline_item(
                previous_item
            ) or not self._is_pending_timeline_item(next_item):
                index += 1
                continue
            if previous_item.scheduled_end <= now or next_item.scheduled_start <= now:
                index += 1
                continue

            # Never insert between or adjacent to existing jingles
            if self._is_jingle_item(previous_item) or self._is_jingle_item(next_item):
                index += 1
                continue

            if previous_item.dj_id != next_item.dj_id:
                index += 1
                continue

            rules = self._get_sting_rules(previous_item.dj_id)
            if not rules.get("enabled", False):
                index += 1
                continue

            allowed_prev = set(rules.get("allowed_prev_types", []))
            allowed_next = set(rules.get("allowed_next_types", []))
            if allowed_prev and previous_item.item_type not in allowed_prev:
                index += 1
                continue
            if allowed_next and next_item.item_type not in allowed_next:
                index += 1
                continue

            if rules.get(
                "avoid_after_next_preview", True
            ) and self._is_next_preview_item(previous_item):
                index += 1
                continue

            cooldown = float(rules.get("cooldown_seconds", 0) or 0.0)
            last_time = self._sting_last_time_by_dj.get(previous_item.dj_id)
            if last_time:
                since_last = (previous_item.scheduled_end - last_time).total_seconds()
                if since_last < cooldown:
                    index += 1
                    continue

            chance = float(rules.get("chance", 0.0))
            if chance <= 0 or random.random() > chance:
                index += 1
                continue

            jingle = self.advanced_scheduler.jingles_manager.get_jingle_for_talk_type(
                "sting", previous_item.dj_id, position="sting"
            )
            if not jingle:
                index += 1
                continue

            # Prefer the jingle's real probed duration; fall back to the configured
            # placeholder only if the file probe failed at startup.
            duration = jingle.duration or float(
                rules.get("default_duration_seconds", 1.5) or 1.5
            )
            sting_segment = JingleSegment(
                segment_id=f"sting_{uuid4()}",
                jingle_id=jingle.id,
                file_path=str(jingle.file_path),
                name=jingle.name,
                duration=duration,
            )
            sting_item = TimelineItem(
                timeline_id=str(uuid4()),
                item_type="jingle",
                content=sting_segment,
                scheduled_start=previous_item.scheduled_end,
                estimated_duration=duration,
                dj_id=previous_item.dj_id,
            )

            timeline.items.insert(index + 1, sting_item)
            self._shift_timeline_from_index(timeline, index + 2, duration)
            self._sting_last_time_by_dj[previous_item.dj_id] = (
                sting_item.scheduled_start
            )
            timeline.last_updated = self._now()

            index += 2
        return

    def _shift_timeline_from_index(
        self, timeline: Timeline, start_index: int, delta_seconds: float
    ) -> None:
        if delta_seconds == 0:
            return
        delta = timedelta(seconds=delta_seconds)
        for item in timeline.items[start_index:]:
            # Never shift committed items - queued/playing audio is already
            # fixed in the producer's buffer
            if not self._is_pending_timeline_item(item):
                continue
            item.scheduled_start = item.scheduled_start + delta
            if item.not_before:
                item.scheduled_start = max(item.scheduled_start, item.not_before)

    def _is_next_preview_item(self, item: TimelineItem) -> bool:
        if item.item_type == "dj_talk" and isinstance(item.content, DJTalkSegment):
            return item.content.prompt_style == "next_preview"
        if item.item_type == "song_block" and isinstance(item.content, SongBlock):
            return item.content.outro_style == "next_preview"
        return False

    def _get_sting_rules(self, dj_id: str) -> dict[str, Any]:
        defaults = (
            self.advanced_scheduler.weights.get("global_weights", {}).get("stings", {})
            if self.advanced_scheduler
            else {}
        )
        overrides = (
            self.advanced_scheduler.weights.get("dj_overrides", {})
            .get(dj_id, {})
            .get("stings", {})
            if self.advanced_scheduler
            else {}
        )
        dj_rules: dict[str, Any] = {}
        if self.config_manager:
            dj_config = self.config_manager.get_dj_config(dj_id)
            if dj_config:
                dj_rules = getattr(dj_config, "sting_rules", {}) or {}

        merged = dict(defaults)
        merged.update(overrides)
        merged.update(dj_rules)
        return merged

    def _get_next_dj_info(self, timeline: Timeline) -> dict | None:
        """Get information about the next DJ scheduled after the current show ends."""
        if not self.config_manager:
            return None

        try:
            # Get the schedule entry that would be current after the show ends
            # We need to look at what's scheduled at show_end time
            schedule = self.config_manager.schedule
            if not schedule:
                return None

            current_dj = timeline.dj_id

            # Use config_manager's method to find the DJ who should be on at show_end
            # This handles the schedule lookup properly
            next_entry = self.config_manager.get_schedule_entry_at_time(
                timeline.show_end
            )

            logger.debug(
                f"🔍 Looking for next DJ at {timeline.show_end.strftime('%H:%M:%S')}: "
                f"found {next_entry.dj_name if next_entry else 'None'}, current is {current_dj}"
            )

            if next_entry:
                # Calculate the new show's end time
                new_show_end = self._calculate_show_end(timeline.show_end, next_entry)
                if new_show_end <= timeline.show_end:
                    logger.error(
                        "Schedule slot does not advance past %s", timeline.show_end
                    )
                    return None

                return {
                    "dj_id": next_entry.dj_name,
                    "show_start": timeline.show_end,
                    "show_end": new_show_end,
                    "music_folders": next_entry.music_folders,
                }

            return None
        except Exception as e:
            logger.error(f"Error getting next DJ info: {e}", exc_info=True)
            return None

    def _add_transition_items(
        self, timeline: Timeline, current_time: datetime, next_dj_info: dict
    ) -> datetime:
        """Add DJ transition items to the timeline.

        Returns the time after the transition items.
        """
        outgoing_dj = timeline.dj_id
        incoming_dj = next_dj_info["dj_id"]

        # Get DJ configs for context
        incoming_config = (
            self.config_manager.get_dj_config(incoming_dj)
            if self.config_manager
            else None
        )

        # Schedule transition to start at current_time (which should be close to show_end)
        schedule_start = max(current_time, timeline.show_end)

        # Create show_end segment for outgoing DJ
        show_end_segment = DJTalkSegment(
            segment_id=f"transition_end_{int(schedule_start.timestamp())}",
            talk_type="show_transitions",
            prompt_style="show_end",
            content="",  # Will be generated during preparation
            audio_file=None,
            duration=0.0,
            requires_data={
                "next_dj_name": incoming_config.name.replace("_", " ").title()
                if incoming_config
                else incoming_dj.replace("_", " ").title(),
            },
        )

        # Create show_start segment for incoming DJ
        show_start_segment = DJTalkSegment(
            segment_id=f"transition_start_{int(schedule_start.timestamp())}",
            talk_type="show_transitions",
            prompt_style="show_start",
            content="",  # Will be generated during preparation
            audio_file=None,
            duration=0.0,
            requires_data={},
        )

        # Estimated duration for transition segments (will be updated when generated)
        transition_duration = 30.0

        # Show end item for outgoing DJ
        show_end_item = TimelineItem(
            timeline_id=str(uuid4()),
            item_type="dj_transition_end",
            content=show_end_segment,
            scheduled_start=schedule_start,
            estimated_duration=transition_duration,
            dj_id=outgoing_dj,
            next_dj_id=incoming_dj,
            not_before=timeline.show_end,
            status="scheduled",
        )

        # Show start item for incoming DJ
        show_start_time = schedule_start + timedelta(seconds=transition_duration)
        show_start_item = TimelineItem(
            timeline_id=str(uuid4()),
            item_type="dj_transition_start",
            content=show_start_segment,
            scheduled_start=show_start_time,
            estimated_duration=transition_duration,
            dj_id=incoming_dj,
            next_dj_id=None,
            not_before=timeline.show_end,
            status="scheduled",
        )

        # Add items to timeline
        timeline.items.append(show_end_item)
        timeline.items.append(show_start_item)

        logger.info(
            f"✅ Added transition items: show_end at {schedule_start.strftime('%H:%M:%S')}, "
            f"show_start at {show_start_time.strftime('%H:%M:%S')}"
        )

        # Return time after both transition items
        return show_start_time + timedelta(seconds=transition_duration)

    async def prepare_timeline_items(
        self, timeline: Timeline, current_time: datetime | None = None
    ) -> None:
        """Ensure upcoming timeline items are prepared"""
        if current_time is None:
            current_time = self._now()

        preparation_window = timedelta(seconds=timeline.preparation_window)

        for item in timeline.items:
            # Skip if already prepared or not time yet
            if item.status in ["ready", "playing", "completed", "queued"]:
                continue
            if item.retry_at and current_time < item.retry_at:
                continue

            # Check if item needs preparation soon
            time_until = item.scheduled_start - current_time
            if (
                item.item_type == "jingle"
                and time_until <= preparation_window
                and item.timeline_id not in self.preparation_tasks
            ):
                logger.debug(
                    "🎵 Jingle prep window: id=%s status=%s scheduled=%s time_until=%.1fs",
                    item.timeline_id,
                    item.status,
                    item.scheduled_start.strftime("%H:%M:%S"),
                    time_until.total_seconds(),
                )
            if time_until <= preparation_window:
                if time_until < timedelta(seconds=-30):
                    logger.warning(
                        f"⏰ Preparing overdue item ({time_until.total_seconds():.1f}s late): "
                        f"{item.item_type} scheduled for {item.scheduled_start.strftime('%H:%M:%S')}"
                    )
                # Only start preparation if not already preparing/ready/etc AND not already tracked
                if (
                    item.status
                    not in ["preparing", "ready", "playing", "completed", "queued"]
                    and item.timeline_id not in self.preparation_tasks
                ):
                    # Start preparation task
                    task = asyncio.create_task(self._prepare_item(timeline, item))
                    self.preparation_tasks[item.timeline_id] = task
                    logger.info(
                        f"🔧 Started preparing {item.item_type} scheduled for "
                        f"{item.scheduled_start.strftime('%H:%M:%S')}"
                    )

    async def _prepare_item(self, timeline: Timeline, item: TimelineItem) -> bool:
        """Prepare a single timeline item"""
        try:
            # Check if already being prepared or already prepared to prevent duplicate work
            if item.status in ["preparing", "ready", "playing", "completed", "queued"]:
                logger.debug(
                    f"⏭️ Skipping preparation for {item.timeline_id} - already {item.status}"
                )
                return item.status in ["ready", "queued"]

            # Register this preparation to prevent duplicate tasks
            if item.timeline_id not in self.preparation_tasks:
                # Create a placeholder to mark we're handling this
                self.preparation_tasks[item.timeline_id] = asyncio.current_task()

            item.status = "preparing"
            item.preparation_progress = {"started": self._now().isoformat()}

            if item.item_type in [
                "dj_talk",
                "dj_transition_end",
                "dj_transition_start",
            ]:
                # Prepare DJ talk audio
                if isinstance(item.content, DJTalkSegment):
                    talk_segment = item.content
                    await self._assign_bed_offset(timeline, item, talk_segment)

                    # Convert to ScheduleSegment for compatibility
                    schedule_segment = ScheduleSegment(
                        segment_id=talk_segment.segment_id,
                        segment_type=item.item_type,
                        content=talk_segment,
                        start_time=item.scheduled_start,
                        estimated_duration=item.estimated_duration,
                        dj_id=item.dj_id,
                    )

                    success = await self.advanced_scheduler.prepare_audio_for_segment(
                        schedule_segment
                    )
                    if success:
                        # Update timeline item duration with actual duration from prepared segment
                        if talk_segment.duration > 0:
                            old_duration = item.estimated_duration
                            item.estimated_duration = talk_segment.duration
                            logger.info(
                                f"✅ Updated timeline item duration from {old_duration:.1f}s to {talk_segment.duration:.1f}s"
                            )

                            # Recalculate timeline if duration changed significantly
                            if (
                                abs(old_duration - talk_segment.duration) > 2.0
                            ):  # More than 2 seconds difference
                                await self._recalculate_timeline_times(timeline, item)

                        item.status = "ready"
                        item.preparation_progress["completed"] = self._now().isoformat()
                        logger.info(
                            f"✅ Prepared DJ talk: {talk_segment.content[:50]}..."
                        )
                        return True
                    else:
                        item.status = "failed"
                        logger.error(
                            f"❌ Failed to prepare DJ talk for {item.timeline_id}"
                        )
                        return False

            elif item.item_type == "jingle":
                # Prepare jingle audio (validate file and get duration)
                from .advanced_program_scheduler import JingleSegment

                if isinstance(item.content, JingleSegment):
                    jingle_segment = item.content
                    logger.info(
                        "🎵 Preparing jingle item: id=%s name=%s scheduled=%s file=%s",
                        item.timeline_id,
                        jingle_segment.name,
                        item.scheduled_start.strftime("%H:%M:%S"),
                        jingle_segment.file_path,
                    )

                    # Convert to ScheduleSegment for compatibility
                    schedule_segment = ScheduleSegment(
                        segment_id=jingle_segment.segment_id,
                        segment_type="jingle",
                        content=jingle_segment,
                        start_time=item.scheduled_start,
                        estimated_duration=item.estimated_duration,
                        dj_id=item.dj_id,
                    )

                    success = await self.advanced_scheduler.prepare_audio_for_segment(
                        schedule_segment
                    )
                    if success:
                        # Update timeline item duration with actual duration
                        if jingle_segment.duration > 0:
                            old_duration = item.estimated_duration
                            item.estimated_duration = jingle_segment.duration
                            logger.info(
                                f"✅ Updated jingle duration from {old_duration:.1f}s to {jingle_segment.duration:.1f}s"
                            )

                            # Recalculate timeline if duration changed significantly
                            if abs(old_duration - jingle_segment.duration) > 2.0:
                                await self._recalculate_timeline_times(timeline, item)

                        item.status = "ready"
                        item.preparation_progress["completed"] = self._now().isoformat()
                        logger.info(
                            "✅ Prepared jingle: %s (id=%s duration=%.1fs scheduled=%s)",
                            jingle_segment.name,
                            item.timeline_id,
                            item.estimated_duration,
                            item.scheduled_start.strftime("%H:%M:%S"),
                        )
                        return True
                    else:
                        item.status = "failed"
                        logger.error(
                            "❌ Failed to prepare jingle %s (id=%s file=%s)",
                            jingle_segment.name,
                            item.timeline_id,
                            jingle_segment.file_path,
                        )
                        return False

            elif item.item_type == "song_block":
                # Prepare song block audio (intros/outros)
                if isinstance(item.content, SongBlock):
                    song_block = item.content
                    self._suppress_intro_after_next_preview(timeline, item, song_block)
                    await self._assign_carryover_bed(timeline, item)

                    # Identify the next song (first song of the next song_block) to allow outro "next_preview" teases
                    next_song_info = None
                    try:
                        current_index = timeline.items.index(item)
                        next_item = (
                            timeline.items[current_index + 1]
                            if current_index + 1 < len(timeline.items)
                            else None
                        )
                        if (
                            next_item
                            and next_item.item_type == "song_block"
                            and isinstance(next_item.content, SongBlock)
                            and next_item.content.songs
                        ):
                            first_next_song = next_item.content.songs[0]
                            next_song_info = {
                                "title": first_next_song.title,
                                "artist": first_next_song.artist,
                                "album": getattr(first_next_song, "album", ""),
                                "year": getattr(first_next_song, "year", None),
                                "genre": getattr(first_next_song, "genre", ""),
                            }
                    except ValueError:
                        pass

                    schedule_segment = ScheduleSegment(
                        segment_id=song_block.block_id,
                        segment_type="song_block",
                        content=song_block,
                        start_time=item.scheduled_start,
                        estimated_duration=item.estimated_duration,
                        dj_id=item.dj_id,
                        next_song=next_song_info,
                    )

                    success = await self.advanced_scheduler.prepare_audio_for_segment(
                        schedule_segment
                    )
                    if success:
                        # Calculate actual duration from playback behavior
                        old_duration = item.estimated_duration
                        actual_duration = await self._calculate_song_block_duration(
                            song_block
                        )

                        item.estimated_duration = actual_duration
                        song_block.estimated_duration = actual_duration
                        logger.info(
                            f"✅ Updated song block duration from {old_duration:.1f}s to {actual_duration:.1f}s"
                        )

                        # Recalculate timeline if duration changed significantly
                        if abs(old_duration - actual_duration) > 2.0:
                            await self._recalculate_timeline_times(timeline, item)

                        item.status = "ready"
                        item.preparation_progress["completed"] = self._now().isoformat()
                        logger.info(
                            f"✅ Prepared song block with {len(song_block.songs)} songs"
                        )
                        return True
                    else:
                        item.status = "failed"
                        logger.error(
                            f"❌ Failed to prepare song block {item.timeline_id}"
                        )
                        return False

            else:
                # Items that don't need preparation
                item.status = "ready"
                return True

        except Exception as e:
            logger.error(f"❌ Error preparing timeline item {item.timeline_id}: {e}")
            item.status = "failed"
            return False

        finally:
            if item.status == "failed":
                item.retry_at = self._now() + timedelta(seconds=10)
            # Clean up task reference
            if item.timeline_id in self.preparation_tasks:
                del self.preparation_tasks[item.timeline_id]

    async def _calculate_song_block_duration(self, song_block: SongBlock) -> float:
        """Calculate song block duration based on actual playback sequencing."""
        if getattr(song_block, "prepared_mix_duration", 0.0) > 0:
            actual_duration = float(song_block.prepared_mix_duration)
        else:
            actual_duration = sum(song.duration for song in song_block.songs)

        # Intros are always mixed over songs in playback, so they don't extend duration.
        # Outros only add time when played as separate segments.
        if (
            song_block.block_type == "songs_then_outro"
            and song_block.crossover_type != "over_song"
            and song_block.outro_audio
        ):
            from .audio_utils import get_audio_duration

            try:
                outro_duration = await get_audio_duration(song_block.outro_audio)
                if outro_duration > 0:
                    actual_duration += outro_duration
            except Exception as exc:
                logger.warning(f"⚠️ Could not get outro duration: {exc}")

        return actual_duration

    def get_next_unplayed_item(self, timeline: Timeline) -> TimelineItem | None:
        """Get the next item in timeline order that hasn't been played yet.

        Items are returned in their natural position order in the timeline list,
        not sorted by scheduled_start (which can shift as durations change).
        """

        for item in timeline.items:
            # Return the first item that hasn't been processed
            if item.status not in ["scheduled", "preparing", "ready", "failed"]:
                continue

            return item

        return None

    def get_current_item(
        self, timeline: Timeline, on_air_id: str | None = None
    ) -> TimelineItem | None:
        """Get the timeline item whose audio is on air right now.

        Args:
            timeline: The timeline to search.
            on_air_id: timeline_id reported by the audio producer for the file it
                is currently streaming. Falls back to the first in-flight item
                when the producer has not tagged its audio.

        Returns:
            The item currently being broadcast, or None if nothing is playing.
        """
        if on_air_id:
            for item in timeline.items:
                if item.timeline_id == on_air_id:
                    return item

        for item in timeline.items:
            if item.status in ("playing", "queued"):
                return item
        return None

    def get_playback_order(
        self,
        timeline: Timeline,
        count: int = 10,
        on_air_id: str | None = None,
        lookbehind: int = 1,
    ) -> list[TimelineItem]:
        """Get items in the order they will actually be broadcast.

        Ordering follows the timeline list — the same order the broadcast loop
        consumes it — anchored on the item that is on air right now, so the
        result only advances when the stream itself advances. Scheduled clock
        times are deliberately ignored: items are queued ahead of their audio,
        so wall-clock ordering runs minutes ahead of what listeners hear.

        Args:
            timeline: The timeline to read.
            count: Maximum number of items to return after the on-air one.
            on_air_id: timeline_id of the audio currently streaming, used as the
                anchor for the list.
            lookbehind: How many already-played items to include before the
                on-air one. Listeners sit a buffer behind the encoder, so the
                item they are still hearing may already be finished here.

        Returns:
            The on-air item, whatever precedes it within the lookbehind, and
            the items queued behind it.
        """
        playable = [item for item in timeline.items if item.status != "failed"]

        if on_air_id:
            for index, item in enumerate(playable):
                if item.timeline_id == on_air_id:
                    start = max(0, index - lookbehind)
                    return playable[start : index + count]

        pending = [item for item in playable if item.status != "completed"]
        return pending[:count]

    async def _recalculate_timeline_times(
        self, timeline: Timeline, changed_item: TimelineItem
    ) -> None:
        """Recalculate timeline start/end times after a duration change"""
        try:
            # Find the changed item's position in the timeline
            changed_index = None
            for i, item in enumerate(timeline.items):
                if item.timeline_id == changed_item.timeline_id:
                    changed_index = i
                    break

            if changed_index is None:
                return

            # scheduled_end is now calculated automatically via property

            # Recalculate start times for subsequent items (end times are calculated
            # automatically). Never move items already committed to the audio
            # producer (queued/playing/completed) - their real air time is fixed,
            # so later pending items chain off their last known schedule instead.
            for i in range(changed_index + 1, len(timeline.items)):
                current_item = timeline.items[i]
                previous_item = timeline.items[i - 1]

                if not self._is_pending_timeline_item(current_item):
                    continue

                # Start this item when the previous one ends
                current_item.scheduled_start = previous_item.scheduled_end
                if current_item.not_before:
                    current_item.scheduled_start = max(
                        current_item.scheduled_start, current_item.not_before
                    )

            logger.info(
                f"✅ Recalculated timeline times after duration change for {changed_item.timeline_id}"
            )

        except Exception as e:
            logger.error(f"❌ Error recalculating timeline times: {e}")

    def get_timeline_summary(
        self, timeline: Timeline, on_air_id: str | None = None
    ) -> dict[str, Any]:
        """Get a summary of the timeline for display"""
        current_time = self._now()
        current_item = self.get_current_item(timeline, on_air_id)
        next_items = self.get_playback_order(timeline, 10, on_air_id)[1:]

        # Prepare items for serialization
        def serialize_item(item: TimelineItem) -> dict[str, Any]:
            content_summary = {}

            if item.item_type == "dj_talk" and isinstance(item.content, DJTalkSegment):
                content_summary = {
                    "talk_type": item.content.talk_type,
                    "prompt_style": item.content.prompt_style,
                    "content": item.content.content[:100] + "..."
                    if item.content.content
                    else "Preparing...",
                    "has_audio": bool(item.content.audio_file),
                }
            elif item.item_type == "song_block" and isinstance(item.content, SongBlock):
                songs_info = []
                for song in item.content.songs:
                    songs_info.append(
                        {
                            "title": song.title,
                            "artist": song.artist,
                            "duration": song.duration,
                        }
                    )
                content_summary = {
                    "block_type": item.content.block_type,
                    "songs": songs_info,
                    "selection_approach": item.content.selection_approach,
                    "has_intro": bool(item.content.intro_style),
                    "has_outro": bool(item.content.outro_style),
                }
            elif item.item_type in ["dj_transition_end", "dj_transition_start"]:
                content_summary = {
                    "transition_type": item.item_type,
                    "content": getattr(item.content, "content", "Preparing...")[:100],
                    "has_audio": bool(getattr(item.content, "audio_file", None)),
                }

            # Use the scheduler's timezone directly - no conversion needed
            return {
                "timeline_id": item.timeline_id,
                "item_type": item.item_type,
                "scheduled_start": item.scheduled_start.isoformat(),
                "estimated_duration": item.estimated_duration,
                "status": item.status,
                "content_summary": content_summary,
            }

        # Use consistent timezone handling - all times should already be in the correct timezone
        return {
            "timeline_id": timeline.timeline_id,
            "dj_id": timeline.dj_id,
            "show_start": timeline.show_start.isoformat(),
            "show_end": timeline.show_end.isoformat(),
            "created_at": timeline.created_at.isoformat(),
            "last_updated": timeline.last_updated.isoformat(),
            "total_items": len(timeline.items),
            "current_time": current_time.isoformat(),
            "current_item": serialize_item(current_item) if current_item else None,
            "next_items": [serialize_item(item) for item in next_items],
            "preparation_status": {
                "active_preparations": len(self.preparation_tasks),
                "ready_items": len([i for i in timeline.items if i.status == "ready"]),
                "scheduled_items": len(
                    [i for i in timeline.items if i.status == "scheduled"]
                ),
                "failed_items": len(
                    [i for i in timeline.items if i.status == "failed"]
                ),
            },
        }

    async def _assign_carryover_bed(
        self, timeline: Timeline, item: TimelineItem
    ) -> None:
        if item.item_type != "song_block":
            return
        if not isinstance(item.content, SongBlock):
            return
        song_block = item.content
        if not song_block.intro_style:
            return
        if song_block.carryover_bed_key:
            return

        try:
            index = timeline.items.index(item)
        except ValueError:
            return
        if index <= 0:
            return

        previous_item = timeline.items[index - 1]
        if previous_item.item_type != "dj_talk":
            return
        if not isinstance(previous_item.content, DJTalkSegment):
            return

        bed_key = getattr(previous_item.content, "bed_key", None)
        if not bed_key:
            return

        gap_seconds = (
            item.scheduled_start - previous_item.scheduled_end
        ).total_seconds()
        if gap_seconds > 1.0:
            return

        song_block.carryover_bed_key = bed_key
        song_block.carryover_bed_volume = getattr(
            previous_item.content, "bed_volume", None
        )
        total_duration = self._sum_contiguous_bed_duration(timeline, index, bed_key)
        if total_duration > 0:
            bed_duration = await self._get_bed_duration_seconds(bed_key)
            if bed_duration > 0:
                song_block.carryover_bed_offset_seconds = total_duration % bed_duration
        logger.info(f"🎛️ Carrying bed '{bed_key}' into intro for {item.timeline_id}")

    def _suppress_intro_after_next_preview(
        self, timeline: Timeline, item: TimelineItem, song_block: SongBlock
    ) -> None:
        if not song_block.intro_style:
            return

        try:
            index = timeline.items.index(item)
        except ValueError:
            return
        if index <= 0:
            return

        previous_item = timeline.items[index - 1]
        previous_was_next_preview = False

        if previous_item.item_type == "dj_talk" and isinstance(
            previous_item.content, DJTalkSegment
        ):
            previous_was_next_preview = (
                previous_item.content.prompt_style == "next_preview"
            )
        elif previous_item.item_type == "song_block" and isinstance(
            previous_item.content, SongBlock
        ):
            previous_was_next_preview = (
                previous_item.content.outro_style == "next_preview"
            )

        if not previous_was_next_preview:
            return

        logger.info(f"🧹 Suppressing intro after next_preview for {item.timeline_id}")
        song_block.intro_style = None
        song_block.intro_audio = None
        song_block.intro_audio_files = None
        if song_block.block_type in ["intro_then_songs", "individual_intros"]:
            song_block.block_type = "silent_block"

    async def _assign_bed_offset(
        self,
        timeline: Timeline,
        item: TimelineItem,
        talk_segment: DJTalkSegment,
    ) -> None:
        if talk_segment.bed_offset_seconds is not None:
            return

        try:
            index = timeline.items.index(item)
        except ValueError:
            return
        if index <= 0:
            return

        previous_item = timeline.items[index - 1]
        if previous_item.item_type != "dj_talk":
            return
        if not isinstance(previous_item.content, DJTalkSegment):
            return

        gap_seconds = (
            item.scheduled_start - previous_item.scheduled_end
        ).total_seconds()
        if gap_seconds > 1.0:
            return

        previous_bed_key = getattr(previous_item.content, "bed_key", None)
        if not previous_bed_key:
            return

        # Force bed continuity across adjacent talk segments
        talk_segment.bed_key = previous_bed_key
        if getattr(talk_segment, "bed_volume", None) is None:
            talk_segment.bed_volume = getattr(previous_item.content, "bed_volume", None)

        total_duration = self._sum_contiguous_bed_duration(
            timeline, index, previous_bed_key
        )
        if total_duration <= 0:
            return

        bed_duration = await self._get_bed_duration_seconds(previous_bed_key)
        if bed_duration <= 0:
            return

        talk_segment.bed_offset_seconds = total_duration % bed_duration

    def _sum_contiguous_bed_duration(
        self, timeline: Timeline, current_index: int, bed_key: str
    ) -> float:
        total = 0.0
        index = current_index - 1
        while index >= 0:
            previous_item = timeline.items[index]
            if previous_item.item_type != "dj_talk":
                break
            if not isinstance(previous_item.content, DJTalkSegment):
                break
            previous_bed_key = getattr(previous_item.content, "bed_key", None)
            if previous_bed_key != bed_key:
                break
            gap_seconds = (
                timeline.items[index + 1].scheduled_start - previous_item.scheduled_end
            ).total_seconds()
            if gap_seconds > 1.0:
                break
            total += previous_item.estimated_duration
            index -= 1

        return total

    async def _get_bed_duration_seconds(self, bed_key: str) -> float:
        cached = self._bed_duration_cache.get(bed_key)
        if cached is not None:
            return cached

        if not self.advanced_scheduler:
            return 0.0

        bed_path = self.advanced_scheduler._resolve_bed_path(bed_key)
        if not bed_path.exists():
            self._bed_duration_cache[bed_key] = 0.0
            return 0.0

        from .audio_utils import get_audio_duration

        try:
            duration = await get_audio_duration(str(bed_path))
        except Exception:
            duration = 0.0

        self._bed_duration_cache[bed_key] = duration
        return duration

    async def _run_manager_iteration(self, timeline: Timeline) -> None:
        """One pass of the timeline manager loop: prune, prepare, extend."""
        current_time = self._now()

        # Prune completed timeline items older than 1 hour
        pruned_count = self._prune_old_items(timeline, current_time)
        if pruned_count > 0:
            logger.debug(f"🧹 Pruned {pruned_count} old timeline items")

        # Prepare upcoming items
        await self.prepare_timeline_items(timeline, current_time)

        # Extend timeline more aggressively for better web interface visibility
        # Use _find_timeline_end to account for actual vs estimated durations
        timeline_end = self._find_timeline_end(timeline)
        time_until_timeline_end = (timeline_end - current_time).total_seconds()

        # For continuous timeline, always keep extending
        # No need to check show_end anymore since we run continuously
        if time_until_timeline_end < 600 and self._can_attempt_extension(
            current_time
        ):  # Less than 10 minutes of content
            # Check if extension actually added items - if not, we may be stuck
            old_item_count = len(timeline.items)

            logger.info(
                f"🔄 Timeline extension: {time_until_timeline_end:.1f}s content remaining, extending..."
            )
            timeline = self.extend_timeline(timeline, 15)  # Extend by 15 minutes

            new_item_count = len(timeline.items)
            items_added = new_item_count - old_item_count
            self._record_extension_result(items_added, current_time)

            if items_added == 0:
                # No items were added - this could be a stuck state
                # Check if show_end is in the past
                time_until_show_end = (timeline.show_end - current_time).total_seconds()
                if time_until_show_end < -300:  # Show ended more than 5 minutes ago
                    logger.warning(
                        f"⚠️ Timeline stuck: show ended {-time_until_show_end:.0f}s ago and no items being added. "
                        f"Current DJ: {timeline.dj_id}, show_end: {timeline.show_end.strftime('%H:%M:%S')}"
                    )
                    if self._recover_timeline_show_end(timeline):
                        self._consecutive_extension_failures = 0
                        self._next_extension_retry_at = None
                        logger.info(
                            f"✅ Recovery applied: now running {timeline.dj_id} until {timeline.show_end.strftime('%H:%M:%S')}"
                        )

        # Update timeline
        timeline.last_updated = current_time

    async def run_timeline_manager(self, timeline: Timeline):
        """Run the timeline manager for a show"""
        self.current_timeline = timeline
        self.is_running = True
        self._consecutive_extension_failures = 0
        self._next_extension_retry_at = None

        logger.info(f"🚀 Starting timeline manager for {timeline.dj_id}")

        try:
            while self.is_running:
                try:
                    await self._run_manager_iteration(timeline)
                except Exception as e:
                    # Never let one bad iteration kill the manager - a dead
                    # manager silently stops extending the timeline (dead air)
                    logger.error(
                        f"❌ Timeline manager iteration error (continuing): {e}",
                        exc_info=True,
                    )

                # Wait before next cycle
                await asyncio.sleep(10)  # Check every 10 seconds
        finally:
            self.is_running = False
            logger.info("🏁 Timeline manager stopped")

    def _prune_old_items(self, timeline: Timeline, current_time: datetime) -> int:
        """Remove completed items older than 1 hour to prevent unbounded growth"""
        one_hour_ago = current_time - timedelta(hours=1)
        initial_count = len(timeline.items)

        timeline.items = [
            item
            for item in timeline.items
            if not (
                item.status == "completed"
                and item.actual_end
                and item.actual_end < one_hour_ago
            )
        ]

        return initial_count - len(timeline.items)

    def stop_timeline_manager(self):
        """Stop the timeline manager"""
        self.is_running = False

        # Cancel all preparation tasks
        for task in self.preparation_tasks.values():
            if not task.done():
                task.cancel()

        self.preparation_tasks.clear()
        logger.info("⏹️ Timeline manager stopped")
