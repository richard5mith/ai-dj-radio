"""Advanced program scheduler — song block scheduling, DJ talk generation, radio flow."""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config_manager import ConfigManager
from .dj_ai import DJAI
from .jingles_manager import JinglesManager
from .music_library import MusicLibrary
from .unified_dj_generator import DJTalkRequest
from .weather_service import WeatherService

logger = logging.getLogger(__name__)


# Constants for timing and duration
DEFAULT_MAX_RECENT_SONGS = 400
DEFAULT_SHORT_TERM_RECENT_GUARD = 25
DEFAULT_SONG_DURATION_SECONDS = 360.0  # 7 minutes
MIN_SONG_DURATION_SECONDS = 60.0  # 1 minute
ESTIMATED_INTRO_DURATION_SECONDS = 15.0
ESTIMATED_OUTRO_DURATION_SECONDS = 10.0
ESTIMATED_DJ_TALK_DURATION_SECONDS = 15.0
GENERAL_TIME_BUFFER_SECONDS = 30.0
QUEUING_WINDOW_SECONDS = 60.0  # 1 minute
MUSIC_BLOCK_BUFFER_SECONDS = 90.0  # 1.5 minutes
YEAR_GROUPING_DECADE = 10
MAX_SEARCH_SONGS_FOR_FIT = 400  # Limit search for performance
MAX_DJ_TALK_HISTORY = 4

# File paths
RECENT_SONGS_STATE_FILE = "/app/temp-audio/recent_songs.json"
SCHEDULER_WEIGHTS_PATH = "/app/config/scheduler_weights.json"
DJ_PROMPTS_PATH = "/app/config/dj_prompts.json"
BEDS_BASE_PATH = Path("/app/beds")


@dataclass
class Song:
    file_path: str
    title: str
    artist: str
    album: str
    year: int
    genre: str
    duration: float


@dataclass
class SongBlock:
    block_id: str
    block_type: str  # intro_then_songs, songs_then_outro, individual_intros, etc.
    songs: list[Song]
    selection_approach: str
    intro_style: str | None = None
    outro_style: str | None = None
    crossover_type: str = "no_crossover"
    estimated_duration: float = 0.0
    dj_audio_ready: bool = False
    intro_audio: str | None = None  # Path to generated intro audio file
    intro_audio_files: list[str] | None = None  # For individual_intros - one per song
    outro_audio_ready: bool = False
    outro_audio: str | None = None  # Path to generated outro audio file
    outro_audio_files: list[str] | None = None  # For individual_outros - one per song
    carryover_bed_key: str | None = None
    carryover_bed_volume: float | None = None
    carryover_bed_offset_seconds: float | None = None
    prepared_mix_file: str | None = None
    prepared_mix_duration: float = 0.0
    prepared_mix_metadata: dict[str, Any] | None = None
    prepared_mix_metadata_schedule: list[dict[str, Any]] | None = None
    prepared_mix_includes_outro: bool = False


@dataclass
class DJTalkSegment:
    segment_id: str
    talk_type: str
    prompt_style: str
    content: str
    audio_file: str | None = None
    duration: float = 0.0
    requires_data: dict[str, Any] = None
    bed_key: str | None = None
    bed_volume: float | None = None
    bed_offset_seconds: float | None = None


@dataclass
@dataclass
class JingleSegment:
    segment_id: str
    jingle_id: str
    file_path: str
    name: str
    duration: float = 0.0


@dataclass
class ScheduleSegment:
    segment_id: str
    segment_type: str  # "song_block", "dj_talk", "jingle"
    content: Any  # SongBlock, DJTalkSegment, or JingleSegment
    start_time: datetime
    estimated_duration: float
    dj_id: str
    next_song: dict[str, Any] | None = (
        None  # Optional hint about the next song to tease
    )


class RecentTracker:
    """Track station play events separately from pending song reservations."""

    def __init__(
        self,
        max_recent: int = DEFAULT_MAX_RECENT_SONGS,
        state_file: str = RECENT_SONGS_STATE_FILE,
    ) -> None:
        """Load history from state_file, retaining at most max_recent play events."""
        self.max_recent = max_recent
        self.state_file = Path(state_file)
        self.plays: list[dict[str, str]] = []
        self.reservations: dict[str, set[str]] = {}
        self._load_state()

    @staticmethod
    def _get_song_key(artist: str, title: str) -> str:
        """Return a normalized identity for the supplied artist and title."""
        return f"{artist.lower().strip()}|{title.lower().strip()}"

    def _prune(self) -> None:
        """Remove events older than 24 hours or beyond the global play limit."""
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        self.plays = [
            event
            for event in self.plays
            if datetime.fromisoformat(event["played_at"]) >= cutoff
        ][-self.max_recent :]

    def _load_state(self) -> None:
        """Load validated play events, migrating legacy history without refreshing it."""
        try:
            if not self.state_file.exists():
                return
            data = json.loads(self.state_file.read_text())
            events = data.get("plays", [])
            if "plays" not in data:
                # Legacy histories have no individual timestamps or global order.
                # Interleave their newest entries using the file's last update,
                # so migration neither favours a DJ nor extends exclusions forever.
                updated = data.get("last_updated")
                histories = data.get("recent_songs_by_dj", {})
                if updated and histories:
                    rows = [
                        {**song, "dj_id": dj_id, "played_at": updated}
                        for index in range(max(map(len, histories.values())))
                        for dj_id, songs in histories.items()
                        if index < len(songs)
                        for song in [songs[index]]
                    ]
                    events = list(reversed(rows[: self.max_recent]))
            for event in events:
                try:
                    when = datetime.fromisoformat(event["played_at"])
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=UTC)
                    if not all(
                        isinstance(event.get(key), str)
                        for key in ("artist", "title", "dj_id")
                    ):
                        continue
                    self.plays.append(
                        {
                            "artist": event["artist"],
                            "title": event["title"],
                            "dj_id": event["dj_id"],
                            "played_at": when.isoformat(),
                        }
                    )
                except (KeyError, TypeError, ValueError):
                    logger.warning("Ignoring invalid play-history event")
            self.plays.sort(
                key=lambda event: datetime.fromisoformat(event["played_at"])
            )
            self._prune()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logger.error("Cannot load play history: %s", exc)
            self.plays = []

    def _save_state(self) -> None:
        """Atomically persist actual plays; pending reservations remain in memory."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"version": 2, "plays": self.plays}, indent=2)
            )
            temporary.replace(self.state_file)
        except OSError as exc:
            logger.error("Cannot save play history: %s", exc)

    def add_song(
        self,
        artist: str,
        title: str,
        dj_id: str,
        played_at: datetime | None = None,
    ) -> None:
        """Record one actual play for dj_id at played_at (UTC now by default)."""
        when = played_at or datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        self.plays.append(
            {
                "artist": artist,
                "title": title,
                "dj_id": dj_id,
                "played_at": when.isoformat(),
            }
        )
        self._prune()
        self._save_state()

    def reserve_songs(self, block_id: str, songs: list[Song]) -> None:
        """Reserve songs for block_id until that scheduled block finishes."""
        self.reservations[block_id] = {
            self._get_song_key(song.artist, song.title) for song in songs
        }

    def release_reservation(self, block_id: str) -> None:
        """Release block_id after its audio has actually finished."""
        self.reservations.pop(block_id, None)

    def is_recent(self, artist: str, title: str, dj_id: str) -> bool:
        """Return whether this DJ played the supplied song within the global window."""
        self._prune()
        key = self._get_song_key(artist, title)
        return any(
            event["dj_id"] == dj_id
            and self._get_song_key(event["artist"], event["title"]) == key
            for event in self.plays
        )

    def get_available_songs(self, all_songs: list[Song], dj_id: str) -> list[Song]:
        """Return unreserved songs, relaxing oldest station plays only when necessary."""
        self._prune()
        reserved = set().union(*self.reservations.values())
        candidates = [
            song
            for song in all_songs
            if self._get_song_key(song.artist, song.title) not in reserved
        ]
        if not candidates:
            return []
        latest = {
            self._get_song_key(event["artist"], event["title"]): index
            for index, event in enumerate(self.plays)
        }
        available = [
            song
            for song in candidates
            if self._get_song_key(song.artist, song.title) not in latest
        ]
        if available:
            return available
        # Reduce the guard only as far as necessary for this library. Even two
        # songs alternate; a one-song library necessarily repeats its only song.
        guard = min(DEFAULT_SHORT_TERM_RECENT_GUARD, len(latest))
        while guard:
            guarded = {
                self._get_song_key(event["artist"], event["title"])
                for event in self.plays[-guard:]
            }
            available = [
                song
                for song in candidates
                if self._get_song_key(song.artist, song.title) not in guarded
            ]
            if available:
                logger.info("Relaxed station play history for DJ %s", dj_id)
                return available
            guard -= 1
        oldest = min(
            latest[self._get_song_key(song.artist, song.title)] for song in candidates
        )
        return [
            song
            for song in candidates
            if latest[self._get_song_key(song.artist, song.title)] == oldest
        ]


class AdvancedProgramScheduler:
    """Advanced scheduler with intelligent song blocks and DJ talk"""

    def __init__(
        self,
        config_manager: ConfigManager,
        music_library: MusicLibrary,
        dj_ai: DJAI,
        weather_service: WeatherService,
        audio_manager=None,
        radio_station=None,
    ):
        self.config_manager = config_manager
        self.music_library = music_library
        self.dj_ai = dj_ai
        self.weather_service = weather_service
        self.audio_manager = audio_manager
        self.radio_station = radio_station
        self.audio_mixer = getattr(radio_station, "audio_mixer", None)
        self.recent_tracker = RecentTracker()

        # Initialize jingles manager
        self.jingles_manager = JinglesManager()

        # Load scheduler configuration
        self.weights = self._load_scheduler_weights()
        self.prompts = self._load_dj_prompts()

        # Current scheduling state
        self.current_dj = None
        self.show_start_time = None
        self.show_end_time = None
        self.pending_audio_generation = {}

        # Track recent DJ talk types to avoid repetition
        self.recent_dj_talk_types: list[str] = []
        self.max_dj_talk_history: int = MAX_DJ_TALK_HISTORY
        self._scheduled_talk_slots: dict[str, dict[str, datetime]] = {}
        self._recent_talk_times_by_dj: dict[str, dict[str, datetime]] = {}

        logger.info("Advanced program scheduler initialized")

    def reset_dj_talk_history(self) -> None:
        """Reset DJ talk type history (call when music plays)"""
        logger.debug("Resetting DJ talk history after song block")
        self.recent_dj_talk_types = []

    def _load_scheduler_weights(self) -> dict:
        """Load scheduler weights configuration"""
        weights_path = Path(SCHEDULER_WEIGHTS_PATH)
        try:
            logger.info(f"Loading scheduler weights from: {weights_path}")
            with weights_path.open() as f:
                weights = json.load(f)
                logger.info("Successfully loaded scheduler weights")
                return weights
        except FileNotFoundError:
            logger.critical(f"Scheduler weights file not found: {weights_path}")
            exit(1)
        except json.JSONDecodeError as e:
            logger.critical(f"Invalid JSON in scheduler weights file: {e}")
            exit(1)
        except OSError as e:
            logger.critical(f"Failed to read scheduler weights file: {e}")
            exit(1)

    def reload_scheduler_weights(self):
        """Reload scheduler weights from configuration file."""
        logger.info("🔄 Reloading scheduler weights...")
        try:
            self.weights = self._load_scheduler_weights()
            logger.info("✅ Scheduler weights reloaded successfully")
        except Exception as e:
            logger.error(f"❌ Failed to reload scheduler weights: {e}")

    def reload_jingles(self):
        """Reload jingles configuration."""
        logger.info("🔄 Reloading jingles...")
        try:
            self.jingles_manager.load_jingles()
            logger.info("✅ Jingles reloaded successfully")
        except Exception as e:
            logger.error(f"❌ Failed to reload jingles: {e}")

    def _load_dj_prompts(self) -> dict:
        """Load DJ prompts configuration"""
        prompts_path = Path(DJ_PROMPTS_PATH)
        try:
            with prompts_path.open() as f:
                prompts = json.load(f)
                logger.info("Successfully loaded DJ prompts configuration")
                return prompts
        except FileNotFoundError:
            logger.warning(
                f"DJ prompts file not found: {prompts_path}, using empty configuration"
            )
            return {}
        except json.JSONDecodeError as e:
            logger.error(
                f"Invalid JSON in DJ prompts file: {e}, using empty configuration"
            )
            return {}
        except OSError as e:
            logger.error(
                f"Failed to read DJ prompts file: {e}, using empty configuration"
            )
            return {}

    def _determine_crossover_behavior(
        self,
        dj_id: str,
        block_type: str,
        intro_style: str,
        outro_style: str,
        _song_count: int,
    ) -> str:
        """Determine appropriate crossover behavior based on DJ content and song arrangement (song_count unused but kept for API compatibility)"""

        # If it's a silent block (no DJ talk), handle song-to-song transitions
        if block_type == "silent_block":
            song_transition_weights = self._get_weights_for_dj(dj_id, "crossover").get(
                "song_transitions", {"auto_crossfade": 1.0}
            )
            return self._weighted_choice(song_transition_weights)

        # If there's DJ talk (intro or outro), decide how to mix it
        if intro_style or outro_style:
            dj_mixing_weights = self._get_weights_for_dj(dj_id, "crossover").get(
                "dj_mixing", {"over_song": 0.6, "separate_from_song": 0.4}
            )
            return self._weighted_choice(dj_mixing_weights)

        # Fallback - if no DJ content, treat as song transition
        song_transition_weights = self._get_weights_for_dj(dj_id, "crossover").get(
            "song_transitions", {"auto_crossfade": 1.0}
        )
        return self._weighted_choice(song_transition_weights)

    def _weighted_choice(self, weights: dict[str, float]) -> str:
        """Make a weighted random choice"""
        items = list(weights.keys())
        weights_list = list(weights.values())
        return random.choices(items, weights=weights_list, k=1)[0]

    def _get_bed_settings(
        self, dj_config, talk_category: str, talk_style: str
    ) -> tuple[str | None, float | None]:
        prompt_config = self.prompts.get(talk_category, {}).get(talk_style, {})
        bed_key = prompt_config.get("bed")
        bed_volume = prompt_config.get("bed_volume")

        dj_beds = getattr(dj_config, "talk_beds", None) or {}
        if isinstance(dj_beds, dict) and talk_style in dj_beds:
            bed_key = dj_beds.get(talk_style)

        bed_volume_override = None
        if isinstance(bed_key, dict):
            bed_volume_override = bed_key.get("bed_volume")
            bed_key = bed_key.get("bed")

        if isinstance(bed_key, list):
            bed_key = random.choice([b for b in bed_key if b]) if bed_key else None

        if bed_volume_override is not None:
            bed_volume = bed_volume_override

        return bed_key, bed_volume

    def _get_schedule_now(self) -> datetime:
        if hasattr(self, "virtual_current_time") and self.virtual_current_time:
            return self.virtual_current_time
        if hasattr(self.show_end_time, "tzinfo") and self.show_end_time.tzinfo:
            return datetime.now(self.show_end_time.tzinfo)
        return datetime.now()

    def _get_talk_min_gap_minutes(self, dj_id: str) -> dict[str, float]:
        global_limits = self.weights.get("global_weights", {}).get(
            "talk_min_gap_minutes", {}
        )
        if not isinstance(global_limits, dict):
            global_limits = {}

        dj_limits: dict[str, float] = {}
        dj_config = self.config_manager.get_dj_config(dj_id)
        if dj_config:
            dj_limits = getattr(dj_config, "talk_min_gap_minutes", {}) or {}
        if not isinstance(dj_limits, dict):
            dj_limits = {}

        merged = {**global_limits, **dj_limits}
        return merged

    def _record_talk_type_usage(self, dj_id: str, talk_type: str, when: datetime):
        self.recent_dj_talk_types.append(talk_type)
        if len(self.recent_dj_talk_types) > self.max_dj_talk_history:
            self.recent_dj_talk_types = self.recent_dj_talk_types[
                -self.max_dj_talk_history :
            ]

        if dj_id:
            self._recent_talk_times_by_dj.setdefault(dj_id, {})[talk_type] = when

    def _resolve_bed_path(self, bed_key: str) -> Path:
        bed_path = Path(bed_key)
        if not bed_path.is_absolute():
            bed_path = BEDS_BASE_PATH / bed_path
        return bed_path

    def _get_safe_crossfade_duration(self, durations: list[float]) -> float | None:
        crossfade_duration = None
        if self.config_manager and self.config_manager.station_config:
            crossfade_duration = float(
                self.config_manager.station_config.crossfade_duration_seconds
            )

        valid_durations = [d for d in durations if d > 0.0]
        if not valid_durations:
            return crossfade_duration

        max_allowed = max(0.1, min(valid_durations) * 0.5)
        if crossfade_duration is None:
            return max_allowed
        return min(crossfade_duration, max_allowed)

    def _build_mix_entry(
        self, song: Song, file_path: str, duration: float
    ) -> dict[str, Any]:
        return {
            "title": song.title,
            "artist": song.artist,
            "album": getattr(song, "album", "Unknown Album"),
            "year": getattr(song, "year", None),
            "duration": float(duration or 0.0),
            "file_path": file_path,
        }

    async def _build_full_intro_song(
        self, song: Song, intro_audio: str
    ) -> tuple[str, float] | None:
        if not self.audio_mixer:
            return None

        metadata = {
            "title": song.title,
            "artist": song.artist,
            "year": str(song.year) if song.year else "",
        }

        intro_result = await self.audio_mixer.create_intro_mix(
            song.file_path, intro_audio, metadata
        )
        if not intro_result.success:
            logger.warning(
                f"❌ Intro mix failed for {song.artist} - {song.title}: {intro_result.error_message}"
            )
            return None

        remainder_result = await self.audio_mixer.create_audio_remainder(
            song.file_path, intro_result.duration, metadata, audio_type="song"
        )
        if not remainder_result.success:
            logger.warning(
                f"❌ Intro remainder failed for {song.artist} - {song.title}: {remainder_result.error_message}"
            )
            return None

        concat_result = await self.audio_mixer.concatenate_audio(
            [intro_result.output_file, remainder_result.output_file]
        )
        if concat_result.success:
            return concat_result.output_file, concat_result.duration

        logger.warning(
            f"❌ Intro concatenation failed for {song.artist} - {song.title}: {concat_result.error_message}"
        )
        return None

    async def _build_full_outro_song(
        self, song: Song, outro_audio: str
    ) -> tuple[str, float] | None:
        if not self.audio_mixer:
            return None

        metadata = {
            "title": song.title,
            "artist": song.artist,
            "album": getattr(song, "album", "Unknown Album"),
            "year": str(song.year) if song.year else "",
        }

        result = await self.audio_mixer.create_outro_mix(
            song.file_path, outro_audio, metadata
        )
        if not result.success:
            logger.warning(
                f"❌ Outro mix failed for {song.artist} - {song.title}: {result.error_message}"
            )
            return None

        return result.output_file, result.duration

    async def _prepare_song_block_mix(self, song_block: SongBlock, dj_id: str) -> None:
        if not self.audio_mixer or song_block.prepared_mix_file:
            return

        mix_entries: list[dict[str, Any]] = []
        includes_outro = False

        # Build per-song files (with intros/outros where appropriate)
        if song_block.block_type == "intro_then_songs" and song_block.intro_audio:
            first_song = song_block.songs[0]
            intro_song = await self._build_full_intro_song(
                first_song, song_block.intro_audio
            )
            if intro_song:
                file_path, duration = intro_song
                mix_entries.append(
                    self._build_mix_entry(first_song, file_path, duration)
                )
            else:
                logger.warning(
                    f"⚠️ Falling back to original audio for intro song: {first_song.artist} - {first_song.title}"
                )
                mix_entries.append(
                    self._build_mix_entry(
                        first_song, first_song.file_path, first_song.duration
                    )
                )

            for song in song_block.songs[1:]:
                mix_entries.append(
                    self._build_mix_entry(song, song.file_path, song.duration)
                )

        elif song_block.block_type == "individual_intros":
            for i, song in enumerate(song_block.songs):
                intro_audio = None
                if song_block.intro_audio_files and i < len(
                    song_block.intro_audio_files
                ):
                    intro_audio = song_block.intro_audio_files[i]
                if intro_audio:
                    intro_song = await self._build_full_intro_song(song, intro_audio)
                    if intro_song:
                        file_path, duration = intro_song
                        mix_entries.append(
                            self._build_mix_entry(song, file_path, duration)
                        )
                        continue
                    logger.warning(
                        f"⚠️ Falling back to original audio for intro song: {song.artist} - {song.title}"
                    )
                mix_entries.append(
                    self._build_mix_entry(song, song.file_path, song.duration)
                )

        elif (
            song_block.block_type == "songs_then_outro"
            and song_block.crossover_type == "over_song"
            and song_block.outro_audio
        ):
            for song in song_block.songs[:-1]:
                mix_entries.append(
                    self._build_mix_entry(song, song.file_path, song.duration)
                )

            last_song = song_block.songs[-1]
            outro_song = await self._build_full_outro_song(
                last_song, song_block.outro_audio
            )
            if outro_song:
                file_path, duration = outro_song
                mix_entries.append(
                    self._build_mix_entry(last_song, file_path, duration)
                )
                includes_outro = True
            else:
                logger.warning(
                    f"⚠️ Falling back to original audio for outro song: {last_song.artist} - {last_song.title}"
                )
                mix_entries.append(
                    self._build_mix_entry(
                        last_song, last_song.file_path, last_song.duration
                    )
                )

        elif (
            song_block.block_type == "individual_outros"
            and song_block.crossover_type == "over_song"
            and song_block.outro_audio_files
        ):
            for i, song in enumerate(song_block.songs):
                outro_audio = None
                if i < len(song_block.outro_audio_files):
                    outro_audio = song_block.outro_audio_files[i]
                if outro_audio:
                    outro_song = await self._build_full_outro_song(song, outro_audio)
                    if outro_song:
                        file_path, duration = outro_song
                        mix_entries.append(
                            self._build_mix_entry(song, file_path, duration)
                        )
                        continue
                    logger.warning(
                        f"⚠️ Falling back to original audio for outro song: {song.artist} - {song.title}"
                    )
                mix_entries.append(
                    self._build_mix_entry(song, song.file_path, song.duration)
                )
            includes_outro = True

        else:
            for song in song_block.songs:
                mix_entries.append(
                    self._build_mix_entry(song, song.file_path, song.duration)
                )

        if not mix_entries:
            return

        # Pre-render crossfade mix for multi-song blocks
        durations = [entry["duration"] for entry in mix_entries]
        crossfade_duration = self._get_safe_crossfade_duration(durations)

        if len(mix_entries) == 1:
            entry = mix_entries[0]
            song_block.prepared_mix_file = entry["file_path"]
            song_block.prepared_mix_duration = entry["duration"]
            song_block.prepared_mix_includes_outro = includes_outro
            song_block.prepared_mix_metadata = {
                "title": entry["title"],
                "artist": entry["artist"],
                "album": entry.get("album", "Unknown Album"),
                "year": str(entry.get("year") or ""),
                "content_type": "MUSIC",
                "file_path": entry["file_path"],
            }
            return

        mix_files = [entry["file_path"] for entry in mix_entries]
        result = await self.audio_mixer.create_crossfade_mix(
            mix_files, crossfade_duration=crossfade_duration
        )
        if not result.success:
            logger.warning(
                f"❌ Failed to create prepared crossfade mix for {dj_id}: {result.error_message}"
            )
            return

        crossfade_duration_used = crossfade_duration or 0.0
        if result.details:
            try:
                crossfade_duration_used = float(
                    result.details.get("crossfade_duration", crossfade_duration_used)
                    or 0.0
                )
            except (TypeError, ValueError):
                crossfade_duration_used = crossfade_duration_used or 0.0

        mix_context = [
            {"title": entry["title"], "artist": entry["artist"]}
            for entry in mix_entries
        ]

        def build_metadata(entry: dict[str, Any], position: int) -> dict[str, Any]:
            metadata = {
                "title": entry["title"],
                "artist": entry["artist"],
                "album": entry.get("album", "Unknown Album"),
                "year": str(entry.get("year") or ""),
                "content_type": "MUSIC",
                "file_path": entry["file_path"],
            }
            if len(mix_entries) > 1:
                metadata.update(
                    {
                        "is_mix": True,
                        "mix_context": mix_context,
                        "mix_length": len(mix_entries),
                        "mix_position": position + 1,
                        "mix_crossfade_duration": crossfade_duration_used,
                        "mix_label": f"Crossfade Mix ({len(mix_entries)} tracks)",
                    }
                )
            return metadata

        metadata_schedule: list[dict[str, Any]] = []
        accumulated_offset = 0.0
        for index in range(1, len(mix_entries)):
            previous_duration = float(mix_entries[index - 1]["duration"] or 0.0)
            accumulated_offset += max(0.0, previous_duration - crossfade_duration_used)
            metadata_schedule.append(
                {
                    "offset": round(accumulated_offset, 3),
                    "metadata": build_metadata(mix_entries[index], index),
                }
            )

        song_block.prepared_mix_file = result.output_file
        song_block.prepared_mix_duration = result.duration
        song_block.prepared_mix_metadata = build_metadata(mix_entries[0], 0)
        song_block.prepared_mix_metadata_schedule = metadata_schedule
        song_block.prepared_mix_includes_outro = includes_outro

    async def _mix_bed_audio(
        self,
        audio_file: str,
        bed_key: str | None,
        bed_volume: float | None,
        bed_offset_seconds: float | None,
        segment_label: str,
    ) -> tuple[str, float | None]:
        if not bed_key:
            return audio_file, None

        bed_path = self._resolve_bed_path(bed_key)
        if not bed_path.exists():
            logger.warning(
                f"⚠️ Bed file not found: {bed_path} (segment={segment_label})"
            )
            return audio_file, None

        if not self.radio_station or not getattr(
            self.radio_station, "audio_mixer", None
        ):
            return audio_file, None

        try:
            bed_volume_value = float(bed_volume)
        except (TypeError, ValueError):
            bed_volume_value = 0.2
        bed_volume_value = max(0.0, min(1.0, bed_volume_value))

        mix_result = await self.radio_station.audio_mixer.create_bed_mix(
            audio_file,
            str(bed_path),
            bed_volume=bed_volume_value,
            bed_offset_seconds=bed_offset_seconds,
        )
        if mix_result.success and mix_result.output_file:
            logger.info(f"✅ Mixed bed '{bed_path.name}' under {segment_label}")
            duration = mix_result.duration if mix_result.duration > 0 else None
            return mix_result.output_file, duration

        logger.warning(
            f"⚠️ Failed to mix bed for {segment_label}: {mix_result.error_message}"
        )
        return audio_file, None

    def _get_due_scheduled_talk(
        self, dj_config, now: datetime, listener_count: int | None = None
    ) -> tuple[str, datetime] | None:
        schedule = getattr(dj_config, "talk_schedule", None) or {}
        if not isinstance(schedule, dict) or not schedule:
            return None

        for talk_type, rule in schedule.items():
            slot_time = self._get_due_schedule_slot(
                dj_config.name, talk_type, rule, now, listener_count
            )
            if slot_time:
                return talk_type, slot_time

        return None

    def _get_due_schedule_slot(
        self,
        dj_id: str,
        talk_type: str,
        rule: Any,
        now: datetime,
        listener_count: int | None = None,
    ) -> datetime | None:
        rules = rule if isinstance(rule, list) else [rule]

        for entry in rules:
            if not isinstance(entry, dict):
                continue

            if listener_count == 0 and entry.get("skip_when_no_listeners", True):
                continue

            slot_time = self._find_due_slot_time(entry, now)
            if slot_time is None:
                continue

            last_slot = self._scheduled_talk_slots.get(dj_id, {}).get(talk_type)
            if last_slot and last_slot == slot_time:
                continue

            return slot_time

        return None

    def _find_due_slot_time(
        self, rule: dict[str, Any], now: datetime
    ) -> datetime | None:
        """Find the most recent scheduled slot time that is due right now.

        Checks the previous hour as well as the current one so a slot in the
        last minutes of an hour is still caught at the top of the next hour
        (within the grace window).
        """
        minutes: set[int] = set()

        every_minutes = rule.get("every_minutes")
        if every_minutes:
            try:
                interval = int(every_minutes)
            except (TypeError, ValueError):
                interval = 0
            if interval > 0:
                minutes.update(range(0, 60, interval))

        at_minutes = rule.get("at_minutes", [])
        if isinstance(at_minutes, list):
            for minute in at_minutes:
                try:
                    minutes.add(int(minute))
                except (TypeError, ValueError):
                    continue

        if not minutes:
            return None

        try:
            grace = int(rule.get("grace_minutes", 2))
        except (TypeError, ValueError):
            grace = 2
        grace_seconds = max(0, grace) * 60

        best: datetime | None = None
        for minute in minutes:
            if not 0 <= minute <= 59:
                continue
            for hour_offset in (0, -1):
                slot_time = (now + timedelta(hours=hour_offset)).replace(
                    minute=minute, second=0, microsecond=0
                )
                if 0 <= (now - slot_time).total_seconds() <= grace_seconds and (
                    best is None or slot_time > best
                ):
                    best = slot_time

        return best

    def _mark_scheduled_talk(self, dj_id: str, talk_type: str, slot_time: datetime):
        if dj_id not in self._scheduled_talk_slots:
            self._scheduled_talk_slots[dj_id] = {}
        self._scheduled_talk_slots[dj_id][talk_type] = slot_time

    def _get_weights_for_dj(self, dj_id: str, weight_type: str) -> dict[str, float]:
        """Get weights for a specific DJ, with fallback to global weights"""
        dj_overrides = self.weights.get("dj_overrides", {}).get(dj_id, {})

        if weight_type in dj_overrides:
            return dj_overrides[weight_type]

        return self.weights["global_weights"].get(weight_type, {})

    def _convert_music_track_to_song(self, track) -> Song:
        """Convert music library track to Song object"""
        # Extract metadata from track.metadata dictionary
        metadata = track.metadata
        title = metadata.get("title", "Unknown")
        artist = metadata.get("artist", "Unknown Artist")
        album = metadata.get("album", "Unknown Album")
        year = int(metadata.get("year", 2000)) if metadata.get("year") else 2000
        genre = metadata.get("genre", "Unknown")
        duration = track.duration

        return Song(
            file_path=track.file_path,
            title=title,
            artist=artist,
            album=album,
            year=year,
            genre=genre,
            duration=duration,
        )

    def _select_songs_by_approach(
        self, approach: str, count: int, available_songs: list[Song]
    ) -> list[Song]:
        """Select songs based on the specified approach"""
        if not available_songs:
            return []

        if approach == "random":
            return random.sample(available_songs, min(count, len(available_songs)))

        elif approach == "same_artist":
            # Group by artist and pick from same artist if possible
            by_artist = {}
            for song in available_songs:
                if song.artist not in by_artist:
                    by_artist[song.artist] = []
                by_artist[song.artist].append(song)

            # Find artists with enough songs
            suitable_artists = [
                artist for artist, songs in by_artist.items() if len(songs) >= count
            ]

            if suitable_artists:
                chosen_artist = random.choice(suitable_artists)
                return random.sample(by_artist[chosen_artist], count)
            else:
                return random.sample(available_songs, min(count, len(available_songs)))

        elif approach == "same_album":
            # Group by album
            by_album = {}
            for song in available_songs:
                album_key = f"{song.artist} - {song.album}"
                if album_key not in by_album:
                    by_album[album_key] = []
                by_album[album_key].append(song)

            suitable_albums = [
                album for album, songs in by_album.items() if len(songs) >= count
            ]

            if suitable_albums:
                chosen_album = random.choice(suitable_albums)
                return random.sample(by_album[chosen_album], count)
            else:
                return random.sample(available_songs, min(count, len(available_songs)))

        elif approach == "same_genre":
            # Group by genre
            by_genre = {}
            for song in available_songs:
                if song.genre not in by_genre:
                    by_genre[song.genre] = []
                by_genre[song.genre].append(song)

            suitable_genres = [
                genre for genre, songs in by_genre.items() if len(songs) >= count
            ]

            if suitable_genres:
                chosen_genre = random.choice(suitable_genres)
                return random.sample(by_genre[chosen_genre], count)
            else:
                return random.sample(available_songs, min(count, len(available_songs)))

        elif approach in ["same_year", "same_decade"]:
            # Group by year or decade
            by_period = {}
            for song in available_songs:
                if approach == "same_year":
                    period = song.year
                else:  # same_decade
                    period = (song.year // YEAR_GROUPING_DECADE) * YEAR_GROUPING_DECADE

                if period not in by_period:
                    by_period[period] = []
                by_period[period].append(song)

            suitable_periods = [
                period for period, songs in by_period.items() if len(songs) >= count
            ]

            if suitable_periods:
                chosen_period = random.choice(suitable_periods)
                return random.sample(by_period[chosen_period], count)
            else:
                return random.sample(available_songs, min(count, len(available_songs)))

        # Fallback to random
        return random.sample(available_songs, min(count, len(available_songs)))

    def _find_songs_for_remaining_time(
        self, available_songs: list[Song], target_time: float
    ) -> list[Song] | None:
        """Find the best combination of songs to fill the remaining time.

        Finds songs that fit as close as possible to target_time WITHOUT exceeding it.
        """
        if target_time < MIN_SONG_DURATION_SECONDS:
            return None

        # Filter to only songs that don't exceed target time
        suitable_songs = [s for s in available_songs if s.duration <= target_time]
        if not suitable_songs:
            return None

        # Sort by duration for efficient searching
        sorted_songs = sorted(suitable_songs, key=lambda s: s.duration)

        # Try to find a single song that's closest to target time
        best_single = max(sorted_songs, key=lambda s: s.duration)
        best_fit = [best_single]
        best_diff = target_time - best_single.duration

        # Try to find two songs that get closer to target time
        for i, song1 in enumerate(sorted_songs):
            for song2 in sorted_songs[i + 1 :]:
                total_duration = song1.duration + song2.duration
                if total_duration <= target_time:
                    diff = target_time - total_duration
                    if diff < best_diff:
                        best_fit = [song1, song2]
                        best_diff = diff
                        if diff < 30:  # Within 30 seconds is good enough
                            break
            if best_diff < 30:
                break

        # Try to find three songs that get even closer (only if we're still far off)
        if best_diff > 30:
            for i, song1 in enumerate(sorted_songs[:MAX_SEARCH_SONGS_FOR_FIT]):
                for j, song2 in enumerate(
                    sorted_songs[i + 1 : MAX_SEARCH_SONGS_FOR_FIT + 1], start=i + 1
                ):
                    for song3 in sorted_songs[j + 1 : MAX_SEARCH_SONGS_FOR_FIT + 2]:
                        total_duration = (
                            song1.duration + song2.duration + song3.duration
                        )
                        if total_duration <= target_time:
                            diff = target_time - total_duration
                            if diff < best_diff:
                                best_fit = [song1, song2, song3]
                                best_diff = diff
                                if diff < 30:  # Within 30 seconds is good enough
                                    break
                    if best_diff < 30:
                        break
                if best_diff < 30:
                    break

        # Log what we found
        total_duration = sum(s.duration for s in best_fit)
        logger.info(
            f"Found {len(best_fit)}-song fit: {' + '.join([f'{s.artist} - {s.title}' for s in best_fit])} "
            f"({total_duration:.1f}s for {target_time:.1f}s target, {best_diff:.1f}s under)"
        )

        return best_fit

    def _determine_block_length(
        self, dj_id: str, remaining_time: float, min_tail_buffer: float = 0.0
    ) -> int:
        """Determine song block length based on remaining time and weights.

        min_tail_buffer reserves some time for a final item; blocks that would leave
        less than this buffer are heavily down-weighted to avoid impossible tails.
        """
        weights = self._get_weights_for_dj(dj_id, "song_block_lengths")

        # Estimate average song length
        avg_song_length = DEFAULT_SONG_DURATION_SECONDS

        # Adjust weights based on remaining time and desired tail buffer
        adjusted_weights = {}

        for length_str, weight in weights.items():
            length = int(length_str)
            estimated_time = length * avg_song_length
            remaining_after_block = remaining_time - estimated_time

            if remaining_after_block < MIN_SONG_DURATION_SECONDS:
                # Not enough room even for a minimal follow-up; push weight down
                adjusted_weights[length_str] = weight * 0.1
            elif remaining_after_block < min_tail_buffer:
                # Leaves less than requested buffer; discourage sharply
                adjusted_weights[length_str] = weight * 0.05
            else:
                adjusted_weights[length_str] = weight

        # If all weights are too low, force length 1
        if all(w < 0.01 for w in adjusted_weights.values()):
            return 1

        return int(self._weighted_choice(adjusted_weights))

    def _get_available_songs(self, dj_id: str) -> list[Song]:
        """Return this DJ's library after station history and reservation filtering."""
        all_songs = [
            self._convert_music_track_to_song(track)
            for track in self.music_library.get_all_tracks()
        ]
        return self.recent_tracker.get_available_songs(all_songs, dj_id)

    def _create_song_block(self, dj_id: str, remaining_time: float) -> SongBlock | None:
        """Create a block for dj_id within remaining_time, or None without songs."""
        available_songs = self._get_available_songs(dj_id)
        if not available_songs:
            return None

        logger.debug(
            f"Creating song block for {dj_id} with {remaining_time:.1f}s remaining, {len(available_songs)} available songs"
        )

        # For tight time constraints (less than 8 minutes), try to find songs that fit the remaining time
        if remaining_time < 480:  # Less than 8 minutes
            buffer = 15

            target_music_time = remaining_time - buffer
            logger.debug(
                f"Tight timing: {remaining_time:.1f}s remaining, using {buffer}s buffer, target music time: {target_music_time:.1f}s"
            )

            # Try to find songs that fit the remaining time perfectly
            fitted_songs = self._find_songs_for_remaining_time(
                available_songs, target_music_time
            )

            if fitted_songs:
                # Use the fitted songs
                selected_songs = fitted_songs

                block_type = "silent_block"  # keep it simple

                selection_approach = "time_fitted"

                logger.info(
                    f"✅ Created time-fitted block with {len(selected_songs)} songs for {target_music_time:.1f}s"
                )
            else:
                logger.warning(
                    f"⚠️ No songs fit in remaining time {remaining_time:.1f}s"
                )
                return None

        else:
            # Normal block creation for longer time periods
            # Encourage leaving at least 2 minutes for a follow-up when under 15 minutes remaining
            min_tail_buffer = 120.0 if remaining_time <= 900 else 0.0

            # Determine block characteristics
            block_length = self._determine_block_length(
                dj_id, remaining_time, min_tail_buffer=min_tail_buffer
            )
            block_type = self._weighted_choice(
                self._get_weights_for_dj(dj_id, "song_block_types")
            )
            selection_approach = self._weighted_choice(
                self._get_weights_for_dj(dj_id, "song_selection_approaches")
            )

            # Select songs
            selected_songs = self._select_songs_by_approach(
                selection_approach, block_length, available_songs
            )

        # Determine intro/outro styles based on block type
        intro_style = None
        outro_style = None

        # Check listener count - skip DJ talk if nobody is listening to save API costs
        if self.radio_station:
            listener_count = self.radio_station.get_listener_count()
            if listener_count == 0:
                block_type = "silent_block"

        if block_type in ["intro_then_songs", "individual_intros"]:
            intro_style = self._weighted_choice(
                self._get_weights_for_dj(dj_id, "intro_styles")
            )

        if block_type in ["songs_then_outro", "individual_outros"]:
            outro_style = self._weighted_choice(
                self._get_weights_for_dj(dj_id, "outro_styles")
            )

        # Determine crossover behavior based on what DJ content exists
        crossover_type = self._determine_crossover_behavior(
            dj_id, block_type, intro_style, outro_style, len(selected_songs)
        )

        # Calculate estimated duration based on block type and crossover behavior.
        # This is an approximation: even when DJ talk crosses over the song, a small
        # portion may extend before or after, but we treat that as zero overhead here.
        songs_duration = sum(song.duration for song in selected_songs)
        talk_duration = 0

        # For intro blocks, DJ talk timing depends on crossover type
        if intro_style:
            if crossover_type == "over_song":
                # DJ talks over song intro - no additional time
                talk_duration += 0
            else:
                # DJ talks separately before songs
                talk_duration += ESTIMATED_INTRO_DURATION_SECONDS

        # For outro blocks, DJ talk timing depends on crossover type
        if outro_style:
            if crossover_type == "over_song":
                # DJ talks over song outro - no additional time
                talk_duration += 0
            else:
                # DJ talks separately after songs
                talk_duration += ESTIMATED_OUTRO_DURATION_SECONDS

        estimated_duration = songs_duration + talk_duration

        block_id = f"block_{int(time.time())}_{random.randint(1000, 9999)}"
        self.recent_tracker.reserve_songs(block_id, selected_songs)

        return SongBlock(
            block_id=block_id,
            block_type=block_type,
            songs=selected_songs,
            selection_approach=selection_approach,
            intro_style=intro_style,
            outro_style=outro_style,
            crossover_type=crossover_type,
            estimated_duration=estimated_duration,
        )

    def _create_dj_talk_segment(
        self, dj_id: str, forced_talk_type: str | None = None
    ) -> list[JingleSegment | DJTalkSegment]:
        """
        Create a DJ talk segment with optional jingles, avoiding recently used types.
        Returns a list of segments: [jingle_before?, dj_talk, jingle_after?]
        """
        # Get available talk types with weights
        all_weights = self._get_weights_for_dj(dj_id, "dj_talk_types")
        dj_config = self.config_manager.get_dj_config(dj_id)
        scheduled_talk_types: set[str] = set()
        if dj_config:
            schedule = getattr(dj_config, "talk_schedule", None) or {}
            if isinstance(schedule, dict):
                scheduled_talk_types = {
                    talk_type for talk_type, rule in schedule.items() if rule
                }

        if forced_talk_type:
            talk_type = forced_talk_type
            if talk_type not in all_weights:
                logger.warning(
                    f"Scheduled talk type '{talk_type}' not in weights for {dj_id}"
                )
            logger.debug(f"Using scheduled talk type override: {talk_type}")
        else:
            # Filter out recently used types to avoid repetition
            now = self._get_schedule_now()
            min_gap_minutes = self._get_talk_min_gap_minutes(dj_id)
            last_times = self._recent_talk_times_by_dj.get(dj_id, {})

            available_weights = {}
            for talk_type_name, weight in all_weights.items():
                if talk_type_name in self.recent_dj_talk_types:
                    continue

                min_gap = min_gap_minutes.get(talk_type_name)
                last_used = last_times.get(talk_type_name)
                if min_gap and last_used:
                    elapsed = (now - last_used).total_seconds()
                    if elapsed < (float(min_gap) * 60.0):
                        continue

                if (
                    not scheduled_talk_types
                    or talk_type_name not in scheduled_talk_types
                ):
                    available_weights[talk_type_name] = weight

            # Check if filtering left us with only zero weights
            total_weight = sum(available_weights.values())

            # If all types have been used recently OR all remaining weights are zero, reset and use all types
            if not available_weights or total_weight == 0:
                logger.info(
                    "All DJ talk types used recently or no non-zero weights available, resetting history"
                )
                self.recent_dj_talk_types = []
                now = self._get_schedule_now()
                min_gap_minutes = self._get_talk_min_gap_minutes(dj_id)
                last_times = self._recent_talk_times_by_dj.get(dj_id, {})
                if scheduled_talk_types:
                    available_weights = {
                        name: weight
                        for name, weight in all_weights.items()
                        if name not in scheduled_talk_types
                    }
                if not available_weights:
                    available_weights = {}
                    for name, weight in all_weights.items():
                        min_gap = min_gap_minutes.get(name)
                        last_used = last_times.get(name)
                        if min_gap and last_used:
                            elapsed = (now - last_used).total_seconds()
                            if elapsed < (float(min_gap) * 60.0):
                                continue
                        available_weights[name] = weight
                if not available_weights:
                    logger.warning(
                        "All DJ talk types blocked by min gap; falling back to all types"
                    )
                    # Exclude schedule-owned types: those fire on their own slots and
                    # a random pick here would make them double-fire within a slot
                    available_weights = {
                        name: weight
                        for name, weight in all_weights.items()
                        if name not in scheduled_talk_types
                    }
                    if not available_weights:
                        available_weights = all_weights

            # Choose from available types
            talk_type = self._weighted_choice(available_weights)

        logger.debug(
            f"Selected DJ talk type: {talk_type}, recent history (before): {self.recent_dj_talk_types}"
        )

        now = self._get_schedule_now()
        min_gap = self._get_talk_min_gap_minutes(dj_id).get(talk_type)
        if forced_talk_type and min_gap:
            last_used = self._recent_talk_times_by_dj.get(dj_id, {}).get(talk_type)
            if last_used:
                elapsed = (now - last_used).total_seconds()
                if elapsed < (float(min_gap) * 60.0):
                    logger.warning(
                        f"⏱️ Scheduled talk type '{talk_type}' within {min_gap}min gap "
                        f"({elapsed:.0f}s since last)."
                    )

        segments = []

        # Check for 'before' jingle
        before_jingle = self.jingles_manager.get_jingle_for_talk_type(
            talk_type, dj_id, position="before"
        )
        if before_jingle:
            segment_id = f"jingle_{int(time.time())}_{random.randint(1000, 9999)}"
            jingle_segment = JingleSegment(
                segment_id=segment_id,
                jingle_id=before_jingle.id,
                file_path=str(before_jingle.file_path),
                name=before_jingle.name,
                duration=before_jingle.duration,
            )
            segments.append(jingle_segment)
            logger.info(f"🎵 Scheduled jingle before DJ talk: {before_jingle.name}")

        # Create DJ talk segment
        bed_key, bed_volume = self._get_bed_settings(
            self.config_manager.get_dj_config(dj_id), "dj_talk_types", talk_type
        )
        segment_id = f"talk_{int(time.time())}_{random.randint(1000, 9999)}"
        talk_segment = DJTalkSegment(
            segment_id=segment_id,
            talk_type="dj_talk_types",  # This is the section in prompts
            prompt_style=talk_type,  # This is the specific type like "general_chat", "weather", etc.
            content="",  # Will be generated async
            duration=0.0,  # Will be determined after generation
            bed_key=bed_key,
            bed_volume=bed_volume,
        )
        segments.append(talk_segment)

        # Add to recent history for normal DJ talk
        self._record_talk_type_usage(dj_id, talk_type, now)

        logger.debug(
            f"Added {talk_type} to recent history (after DJ talk): {self.recent_dj_talk_types}"
        )

        # Check for 'after' jingle
        after_jingle = self.jingles_manager.get_jingle_for_talk_type(
            talk_type, dj_id, position="after"
        )
        if after_jingle:
            segment_id = f"jingle_{int(time.time())}_{random.randint(1000, 9999)}"
            jingle_segment = JingleSegment(
                segment_id=segment_id,
                jingle_id=after_jingle.id,
                file_path=str(after_jingle.file_path),
                name=after_jingle.name,
                duration=after_jingle.duration,
            )
            segments.append(jingle_segment)
            logger.info(f"🎵 Scheduled jingle after DJ talk: {after_jingle.name}")

        return segments

    def start_dj_show(
        self,
        dj_id: str,
        start_time: datetime,
        end_time: datetime,
        _actual_show_start: datetime | None = None,
    ) -> None:
        """Start a new DJ show

        Args:
            dj_id: DJ identifier
            start_time: Current timeline start (for remaining time calculation)
            end_time: Show end time
            _actual_show_start: Not used anymore (kept for compatibility)
        """
        self.current_dj = dj_id
        self.show_start_time = start_time
        self.show_end_time = end_time
        # Track virtual current time to avoid scheduling overlapping segments
        self.virtual_current_time = start_time

        logger.info(f"Starting show for {dj_id} from {start_time} to {end_time}")

    def get_next_segment(self) -> list[ScheduleSegment]:
        """Get the next program segment(s) - returns a list because jingles can create multiple segments

        Returns:
            List of ScheduleSegment objects (could be empty, or contain 1+ segments)
        """
        logger.debug(f"Getting next segment for DJ: {self.current_dj}")

        if not self.current_dj:
            logger.error("No active DJ show")
            return []

        # Use virtual_current_time if available (for timeline scheduling), otherwise use real time
        if hasattr(self, "virtual_current_time") and self.virtual_current_time:
            now = self.virtual_current_time
        else:
            # Fallback to real time for backward compatibility
            if hasattr(self.show_end_time, "tzinfo") and self.show_end_time.tzinfo:
                now = datetime.now(self.show_end_time.tzinfo)
            else:
                now = datetime.now()

        remaining_time = (self.show_end_time - now).total_seconds()

        logger.debug(
            f"Virtual/Current time: {now}, Show end: {self.show_end_time}, Remaining: {remaining_time:.1f}s"
        )

        # If remaining time is negative, we've already filled past the show end - stop
        if remaining_time <= 0:
            logger.debug(
                f"Show time already filled (remaining: {remaining_time:.1f}s), returning empty segment list"
            )
            return []

        # Get DJ config to determine announcement frequency
        dj_config = self.config_manager.get_dj_config(self.current_dj)
        if not dj_config:
            logger.error(f"No DJ config found for {self.current_dj}")
            return []

        # Use DJ's announcement_frequency to decide music vs talk
        # announcement_frequency is how often DJ talks (0.0-1.0)
        # So we invert it: (1 - announcement_frequency) = chance of music
        music_chance = 1.0 - dj_config.announcement_frequency

        # Check listener count - skip DJ talk if nobody is listening to save API costs
        listener_count = 0
        if self.radio_station:
            listener_count = self.radio_station.get_listener_count()
            if listener_count == 0:
                music_chance = 1.0

        scheduled_talk = self._get_due_scheduled_talk(dj_config, now, listener_count)
        forced_talk_type = scheduled_talk[0] if scheduled_talk else None
        scheduled_slot = scheduled_talk[1] if scheduled_talk else None

        # Try to create a song block if random roll says music (unless a talk is scheduled)
        if not forced_talk_type and random.random() < music_chance:
            song_block = self._create_song_block(self.current_dj, remaining_time)
            if song_block:
                # Successfully created song block - use it
                self.reset_dj_talk_history()

                # Update virtual current time for next call
                if hasattr(self, "virtual_current_time") and self.virtual_current_time:
                    self.virtual_current_time += timedelta(
                        seconds=song_block.estimated_duration
                    )

                return [
                    ScheduleSegment(
                        segment_id=song_block.block_id,
                        segment_type="song_block",
                        content=song_block,
                        start_time=now,
                        estimated_duration=song_block.estimated_duration,
                        dj_id=self.current_dj,
                    )
                ]
            else:
                # An empty or fully reserved library needs a later planning pass.
                # Yield so asynchronous library loading and playback can progress.
                if not self._get_available_songs(self.current_dj):
                    return []
                # No songs fit in remaining time, fall back to DJ talk
                logger.info(
                    f"🎵 No suitable songs found for {remaining_time:.1f}s remaining, falling back to DJ talk"
                )

        # Create DJ talk (may also include jingles)
        raw_segments = self._create_dj_talk_segment(
            self.current_dj, forced_talk_type=forced_talk_type
        )
        if forced_talk_type and scheduled_slot:
            self._mark_scheduled_talk(self.current_dj, forced_talk_type, scheduled_slot)

        # Convert raw segments (JingleSegment or DJTalkSegment) into ScheduleSegment objects
        schedule_segments = []
        total_duration = 0.0

        for raw_segment in raw_segments:
            if isinstance(raw_segment, JingleSegment):
                # Create a jingle schedule segment
                schedule_segments.append(
                    ScheduleSegment(
                        segment_id=raw_segment.segment_id,
                        segment_type="jingle",
                        content=raw_segment,
                        start_time=now,
                        estimated_duration=raw_segment.duration,
                        dj_id=self.current_dj,
                    )
                )
                total_duration += raw_segment.duration
            elif isinstance(raw_segment, DJTalkSegment):
                # Create a DJ talk schedule segment
                estimated_duration = (
                    ESTIMATED_DJ_TALK_DURATION_SECONDS
                    if raw_segment.duration == 0.0
                    else raw_segment.duration
                )
                schedule_segments.append(
                    ScheduleSegment(
                        segment_id=raw_segment.segment_id,
                        segment_type="dj_talk",
                        content=raw_segment,
                        start_time=now,
                        estimated_duration=estimated_duration,
                        dj_id=self.current_dj,
                    )
                )
                total_duration += estimated_duration

        # Update virtual current time for next call
        if hasattr(self, "virtual_current_time") and self.virtual_current_time:
            self.virtual_current_time += timedelta(seconds=total_duration)

        return schedule_segments

    async def prepare_audio_for_segment(self, segment: ScheduleSegment) -> bool:
        """Prepare audio for a segment (generate TTS, etc.)"""
        try:
            logger.info(
                f"🔧 Preparing audio for segment: {segment.segment_id}, type: {segment.segment_type}"
            )

            if segment.segment_type == "jingle":
                # Prepare jingle audio (validate file exists and get duration)
                jingle_segment = segment.content

                logger.info(
                    f"🔧 Jingle segment details: {jingle_segment.segment_id}, name: {jingle_segment.name}, file: {jingle_segment.file_path}"
                )

                # Validate file exists
                jingle_path = Path(jingle_segment.file_path)
                if not jingle_path.exists():
                    logger.error(
                        f"❌ Jingle file not found: {jingle_segment.file_path}"
                    )
                    return False

                # Get actual duration from audio file
                try:
                    from .audio_utils import get_audio_duration

                    actual_duration = await get_audio_duration(str(jingle_path))
                    if actual_duration > 0:
                        jingle_segment.duration = actual_duration
                        logger.info(
                            f"✅ Jingle duration set to {actual_duration:.1f}s for {jingle_segment.name}"
                        )
                    else:
                        logger.warning(
                            "⚠️ Could not get duration for jingle, using default"
                        )
                        jingle_segment.duration = 10.0
                except Exception as e:
                    logger.warning(
                        f"⚠️ Could not get duration for {jingle_segment.file_path}: {type(e).__name__}: {e}, using default"
                    )
                    jingle_segment.duration = 10.0

                return True

            elif segment.segment_type in [
                "dj_talk",
                "dj_transition_end",
                "dj_transition_start",
            ]:
                talk_segment = segment.content

                logger.info(
                    f"🔧 DJ Talk segment details: {talk_segment.segment_id}, type: {talk_segment.talk_type}, audio_file: {getattr(talk_segment, 'audio_file', 'MISSING')}"
                )

                # Check if we already have audio for this segment
                if talk_segment.audio_file and Path(talk_segment.audio_file).is_file():
                    logger.debug(
                        f"✅ Audio already available for {talk_segment.segment_id}: {talk_segment.audio_file}"
                    )
                    return True
                talk_segment.audio_file = None

                # Need to generate content and audio
                logger.info(f"🔧 Generating DJ talk for {talk_segment.segment_id}")

                # Use unified generator directly
                dj_config = self.config_manager.get_dj_config(segment.dj_id)
                if not dj_config:
                    logger.error(f"❌ No DJ config found for {segment.dj_id}")
                    return False

                logger.info(f"🔧 DJ config loaded for {segment.dj_id}")

                request = DJTalkRequest(
                    talk_category=talk_segment.talk_type,
                    style=getattr(talk_segment, "prompt_style", None),
                    context=getattr(talk_segment, "requires_data", {}) or {},
                    songs=None,
                )

                logger.info(
                    f"🔧 Created DJTalkRequest: category={request.talk_category}, style={request.style}"
                )

                result = await self.dj_ai.unified_generator.generate_dj_talk(
                    dj_config, request
                )

                logger.info(
                    f"🔧 DJ talk generation result: success={result.success}, error={getattr(result, 'error', 'None')}"
                )

                if result.success:
                    # Copy the generated content and audio file
                    talk_segment.audio_file = result.audio_file
                    talk_segment.content = result.text_content
                    logger.info(
                        f"✅ Successfully generated DJ talk for {talk_segment.segment_id}: {result.audio_file}"
                    )

                    # If the generator resolved a more specific style (e.g. sponsor_read/vamp),
                    # re-evaluate bed settings against the resolved style so beds aren't missed.
                    resolved_style = getattr(result, "style", None)
                    if resolved_style and resolved_style != talk_segment.prompt_style:
                        talk_segment.prompt_style = resolved_style
                        if not getattr(talk_segment, "bed_key", None):
                            bed_key, bed_volume = self._get_bed_settings(
                                dj_config, talk_segment.talk_type, resolved_style
                            )
                            talk_segment.bed_key = bed_key
                            talk_segment.bed_volume = bed_volume

                    # Get actual duration from audio file
                    try:
                        if self.audio_manager:
                            actual_duration = await self.audio_manager.get_duration(
                                result.audio_file
                            )
                            talk_segment.duration = actual_duration
                            logger.info(
                                f"✅ DJ talk duration set to {actual_duration:.1f}s for {talk_segment.segment_id}"
                            )
                        else:
                            # Fallback: estimate based on typical speech rate
                            from .audio_utils import get_audio_duration

                            actual_duration = await get_audio_duration(
                                result.audio_file
                            )
                            if actual_duration > 0:
                                talk_segment.duration = actual_duration
                                logger.info(
                                    f"✅ DJ talk duration set to {actual_duration:.1f}s for {talk_segment.segment_id}"
                                )
                            else:
                                raise Exception("ffprobe failed")
                    except (OSError, ValueError, AttributeError) as e:
                        logger.warning(
                            f"⚠️ Could not get duration for {result.audio_file}: {type(e).__name__}: {e}, using default"
                        )
                        talk_segment.duration = ESTIMATED_DJ_TALK_DURATION_SECONDS

                    # Optionally mix bed music under the talk
                    bed_key = getattr(talk_segment, "bed_key", None)
                    if bed_key:
                        mixed_audio, mixed_duration = await self._mix_bed_audio(
                            talk_segment.audio_file,
                            bed_key,
                            getattr(talk_segment, "bed_volume", None),
                            getattr(talk_segment, "bed_offset_seconds", None),
                            f"talk {talk_segment.segment_id}",
                        )
                        talk_segment.audio_file = mixed_audio
                        if mixed_duration:
                            talk_segment.duration = mixed_duration
                        elif mixed_audio != result.audio_file:
                            try:
                                from .audio_utils import get_audio_duration

                                actual_duration = await get_audio_duration(
                                    talk_segment.audio_file
                                )
                                if actual_duration > 0:
                                    talk_segment.duration = actual_duration
                            except Exception as e:
                                logger.warning(
                                    f"⚠️ Could not get bed mix duration: {type(e).__name__}: {e}"
                                )

                    return True
                else:
                    logger.error(
                        f"❌ DJ talk generation failed for {talk_segment.segment_id} - result.success: {result.success}, error: {getattr(result, 'error', 'None')}"
                    )
                    return False

            elif segment.segment_type == "song_block":
                song_block = segment.content
                if (
                    song_block.prepared_mix_file
                    and not Path(song_block.prepared_mix_file).is_file()
                ):
                    song_block.prepared_mix_file = None
                for ready_field, single_field, list_field in (
                    ("dj_audio_ready", "intro_audio", "intro_audio_files"),
                    ("outro_audio_ready", "outro_audio", "outro_audio_files"),
                ):
                    files = getattr(song_block, list_field) or [
                        getattr(song_block, single_field)
                    ]
                    if getattr(song_block, ready_field) and not all(
                        path and Path(path).is_file() for path in files
                    ):
                        setattr(song_block, ready_field, False)
                        setattr(song_block, single_field, None)
                        setattr(song_block, list_field, None)

                # Generate intro/outro audio if needed
                if song_block.intro_style and not song_block.dj_audio_ready:
                    dj_config = self.config_manager.get_dj_config(segment.dj_id)

                    # For individual_intros, generate separate intro for each song
                    if song_block.block_type == "individual_intros":
                        logger.info(
                            f"🎤 Generating {len(song_block.songs)} individual intros in parallel"
                        )

                        # Prepare all requests
                        async def generate_single_intro(i: int, song):
                            song_info = {
                                "title": song.title,
                                "artist": song.artist,
                                # "album": song.album,
                                "year": song.year,
                                "genre": song.genre,
                            }

                            context_data = {
                                "songs": [song_info],
                                "selection_approach": song_block.selection_approach,
                                "block_type": song_block.block_type,
                                "song_index": i,
                            }

                            request = DJTalkRequest(
                                talk_category="intro_styles",
                                style=song_block.intro_style,
                                context=context_data,
                                songs=[song_info],
                            )

                            result = (
                                await self.dj_ai.unified_generator.generate_dj_talk(
                                    dj_config, request
                                )
                            )

                            if result.success:
                                logger.info(
                                    f"✅ Generated intro {i + 1}/{len(song_block.songs)}: {result.audio_file}"
                                )
                                return result.audio_file
                            else:
                                logger.error(
                                    f"❌ Failed to generate intro {i + 1}/{len(song_block.songs)}"
                                )
                                return None

                        # Execute all generations in parallel
                        song_block.intro_audio_files = await asyncio.gather(
                            *[
                                generate_single_intro(i, song)
                                for i, song in enumerate(song_block.songs)
                            ]
                        )
                    else:
                        # Generate intro for the song block (intro_then_songs)
                        songs_info = []
                        for song in song_block.songs:
                            songs_info.append(
                                {
                                    "title": song.title,
                                    "artist": song.artist,
                                    # "album": song.album,
                                    "year": song.year,
                                    "genre": song.genre,
                                }
                            )

                        context_data = {
                            "songs": songs_info,
                            "selection_approach": song_block.selection_approach,
                            "block_type": song_block.block_type,
                        }

                        # Use unified generator directly
                        request = DJTalkRequest(
                            talk_category="intro_styles",
                            style=song_block.intro_style,
                            context=context_data,
                            songs=context_data.get("songs") if context_data else None,
                        )

                        result = await self.dj_ai.unified_generator.generate_dj_talk(
                            dj_config, request
                        )

                        if result.success:
                            # Audio file was already generated
                            # No need to call generate_speech again
                            song_block.intro_audio = result.audio_file

                    intro_files = (
                        song_block.intro_audio_files
                        if song_block.block_type == "individual_intros"
                        else [song_block.intro_audio]
                    )
                    if not intro_files or not all(intro_files):
                        return False
                    song_block.dj_audio_ready = True
                    if song_block.intro_audio and song_block.carryover_bed_key:
                        mixed_intro, _ = await self._mix_bed_audio(
                            song_block.intro_audio,
                            song_block.carryover_bed_key,
                            song_block.carryover_bed_volume,
                            song_block.carryover_bed_offset_seconds,
                            f"intro {song_block.block_id}",
                        )
                        song_block.intro_audio = mixed_intro
                    if song_block.intro_audio_files and song_block.carryover_bed_key:
                        mixed_files: list[str | None] = []
                        for intro_file in song_block.intro_audio_files:
                            if intro_file:
                                mixed_file, _ = await self._mix_bed_audio(
                                    intro_file,
                                    song_block.carryover_bed_key,
                                    song_block.carryover_bed_volume,
                                    song_block.carryover_bed_offset_seconds,
                                    f"intro {song_block.block_id}",
                                )
                                mixed_files.append(mixed_file)
                            else:
                                mixed_files.append(None)
                        song_block.intro_audio_files = mixed_files

                if song_block.outro_style and not song_block.outro_audio_ready:
                    dj_config = self.config_manager.get_dj_config(segment.dj_id)

                    # For individual_outros, generate separate outro for each song
                    if song_block.block_type == "individual_outros":
                        logger.info(
                            f"🎤 Generating {len(song_block.songs)} individual outros in parallel"
                        )

                        # Prepare all requests
                        async def generate_single_outro(i: int, song):
                            song_info = {
                                "title": song.title,
                                "artist": song.artist,
                                # "album": song.album,
                                "year": song.year,
                                "genre": song.genre,
                            }

                            next_in_block = None
                            if i + 1 < len(song_block.songs):
                                next_song = song_block.songs[i + 1]
                                next_in_block = {
                                    "title": next_song.title,
                                    "artist": next_song.artist,
                                    "album": getattr(next_song, "album", ""),
                                    "year": getattr(next_song, "year", None),
                                    "genre": getattr(next_song, "genre", ""),
                                }

                            context_data = {
                                "songs": [song_info],
                                "selection_approach": song_block.selection_approach,
                                "block_type": song_block.block_type,
                                "song_index": i,
                            }

                            if next_in_block:
                                context_data["next_song_in_block"] = next_in_block
                            if segment.next_song:
                                context_data["next_song_after_block"] = (
                                    segment.next_song
                                )

                            request = DJTalkRequest(
                                talk_category="outro_styles",
                                style=song_block.outro_style,
                                context=context_data,
                                songs=[song_info],
                                next_song=next_in_block or segment.next_song,
                            )

                            result = (
                                await self.dj_ai.unified_generator.generate_dj_talk(
                                    dj_config, request
                                )
                            )

                            if result.success:
                                logger.info(
                                    f"✅ Generated outro {i + 1}/{len(song_block.songs)} for {song.artist} - {song.title}"
                                )
                                return result.audio_file
                            else:
                                logger.error(
                                    f"❌ Failed to generate outro for {song.artist} - {song.title}"
                                )
                                return None

                        # Execute all generations in parallel
                        song_block.outro_audio_files = await asyncio.gather(
                            *[
                                generate_single_outro(i, song)
                                for i, song in enumerate(song_block.songs)
                            ]
                        )
                    else:
                        # For songs_then_outro, generate single outro for all songs
                        songs_info = []
                        for song in song_block.songs:
                            songs_info.append(
                                {
                                    "title": song.title,
                                    "artist": song.artist,
                                    # "album": song.album,
                                    "year": song.year,
                                    "genre": song.genre,
                                }
                            )

                        context_data = {
                            "songs": songs_info,
                            "selection_approach": song_block.selection_approach,
                            "block_type": song_block.block_type,
                        }

                        if segment.next_song:
                            context_data["next_song"] = segment.next_song

                        request = DJTalkRequest(
                            talk_category="outro_styles",
                            style=song_block.outro_style,
                            context=context_data,
                            songs=context_data.get("songs") if context_data else None,
                            next_song=segment.next_song,
                        )

                        result = await self.dj_ai.unified_generator.generate_dj_talk(
                            dj_config, request
                        )

                        if result.success:
                            song_block.outro_audio = result.audio_file

                    outro_files = (
                        song_block.outro_audio_files
                        if song_block.block_type == "individual_outros"
                        else [song_block.outro_audio]
                    )
                    if not outro_files or not all(outro_files):
                        return False
                    song_block.outro_audio_ready = True

                await self._prepare_song_block_mix(song_block, segment.dj_id)
                return True

            return False

        except (AttributeError, KeyError, TypeError) as e:
            logger.error(
                f"Failed to prepare audio for segment {segment.segment_id}: {type(e).__name__}: {e}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error preparing audio for segment {segment.segment_id}: {type(e).__name__}: {e}",
                exc_info=True,
            )
            return False
