import json
import logging
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pytz

logger = logging.getLogger(__name__)


@dataclass
class DJConfig:
    """Configuration for a DJ personality."""

    name: str
    voice_id: str
    personality_prompt: str
    ident_jingles_folder: str
    speech_files_folder: str
    announcement_frequency: float  # How often to announce songs (0.0-1.0)
    talk_schedule: dict[str, Any] = None  # Optional per-DJ scheduled talk rules
    talk_beds: dict[str, Any] = None  # Optional per-DJ bed overrides by talk type
    trivia_topics: list[str] = None  # Optional per-DJ trivia focus areas
    sting_rules: dict[str, Any] = None  # Optional per-DJ sting overrides
    talk_min_gap_minutes: dict[str, float] = None  # Optional per-DJ talk cooldowns
    speech_speed: float = 1.0  # Speech speed multiplier (0.25-4.0)
    tts_instructions: str = "You are a professional radio DJ. Speak clearly and enthusiastically."  # Instructions for TTS generation
    voice_provider: str = "openai"  # "openai" (default), "elevenlabs", or "gemini"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DJConfig":
        return cls(
            name=data["name"],
            voice_id=data["voice_id"],
            personality_prompt=data["personality_prompt"],
            ident_jingles_folder=data["ident_jingles_folder"],
            speech_files_folder=data["speech_files_folder"],
            announcement_frequency=data["announcement_frequency"],
            talk_schedule=data.get("talk_schedule", {}),
            talk_beds=data.get("talk_beds", {}),
            trivia_topics=data.get("trivia_topics", []),
            sting_rules=data.get("sting_rules", {}),
            talk_min_gap_minutes=data.get("talk_min_gap_minutes", {}),
            speech_speed=data.get("speech_speed", 1.0),
            tts_instructions=data.get(
                "tts_instructions",
                "You are a professional radio DJ. Speak clearly and enthusiastically.",
            ),
            voice_provider=data.get("voice_provider", "openai"),
        )


@dataclass
class ScheduleEntry:
    """A single schedule entry for the radio station."""

    start_time: time
    end_time: time
    dj_name: str
    music_folders: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleEntry":
        start_time = time.fromisoformat(data["start_time"])
        end_time = time.fromisoformat(data["end_time"])
        return cls(
            start_time=start_time,
            end_time=end_time,
            dj_name=data["dj_name"],
            music_folders=data["music_folders"],
        )


@dataclass
class StationConfig:
    """Main station configuration."""

    station_name: str
    location: str
    timezone: str
    weather_location: str
    ad_break_interval_minutes: int
    crossfade_duration_seconds: float
    stream_bitrate: int
    # Optional case-sensitive substring replacements applied to TTS text before
    # synthesis. Useful for forcing pronunciation of place or product names.
    pronunciations: dict[str, str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StationConfig":
        return cls(
            station_name=data["station_name"],
            location=data["location"],
            timezone=data["timezone"],
            weather_location=data["weather_location"],
            ad_break_interval_minutes=data["ad_break_interval_minutes"],
            crossfade_duration_seconds=data["crossfade_duration_seconds"],
            stream_bitrate=data["stream_bitrate"],
            pronunciations=data.get("pronunciations") or {},
        )


class ConfigManager:
    """Manages all configuration for the radio station."""

    def __init__(self, config_path: str = "/app/config"):
        self.config_path = Path(config_path)
        self.station_config: StationConfig | None = None
        self.dj_configs: dict[str, DJConfig] = {}
        self.schedule: list[ScheduleEntry] = []
        self.timezone = pytz.UTC

        self._load_configs()

    def _load_configs(self):
        """Load all configuration files."""
        try:
            self._load_station_config()
            self._load_dj_configs()
            self._load_schedule()
            logger.info("All configurations loaded successfully")
        except Exception as e:
            logger.error(f"Error loading configurations: {e}")
            raise

    def _load_station_config(self):
        """Load main station configuration."""
        config_file = self.config_path / "station.json"
        if not config_file.exists():
            raise FileNotFoundError(f"Station config not found: {config_file}")

        with config_file.open() as f:
            data = json.load(f)

        self.station_config = StationConfig.from_dict(data)
        self.timezone = pytz.timezone(self.station_config.timezone)
        logger.info(f"Loaded station config for {self.station_config.station_name}")

    def _load_dj_configs(self):
        """Load all DJ configuration files."""
        dj_config_path = Path("/app/dj-configs")
        if not dj_config_path.exists():
            logger.warning(f"DJ configs directory not found: {dj_config_path}")
            return

        for config_file in dj_config_path.glob("*.json"):
            # Skip *.example.json templates shipped for forkers
            if config_file.name.endswith(".example.json"):
                continue
            try:
                with config_file.open() as f:
                    data = json.load(f)

                dj_config = DJConfig.from_dict(data)
                self.dj_configs[dj_config.name] = dj_config
                logger.info(f"Loaded DJ config for {dj_config.name}")
            except Exception as e:
                logger.error(f"Error loading DJ config {config_file}: {e}")

    def _load_schedule(self):
        """Load the daily schedule."""
        schedule_file = self.config_path / "schedule.json"
        if not schedule_file.exists():
            raise FileNotFoundError(f"Schedule not found: {schedule_file}")

        with schedule_file.open() as f:
            data = json.load(f)

        self.schedule = [ScheduleEntry.from_dict(entry) for entry in data["schedule"]]
        logger.info(f"Loaded schedule with {len(self.schedule)} entries")

    def get_current_schedule_entry(self) -> ScheduleEntry | None:
        """Get the current schedule entry based on the current time."""
        now = datetime.now(self.timezone).time()

        for entry in self.schedule:
            if self._time_in_range(now, entry.start_time, entry.end_time):
                return entry

        return None

    def get_current_dj_config(self) -> DJConfig | None:
        """Get the DJ configuration for the current time slot."""
        current_entry = self.get_current_schedule_entry()
        if current_entry and current_entry.dj_name in self.dj_configs:
            return self.dj_configs[current_entry.dj_name]
        return None

    def get_next_schedule_entry(self) -> ScheduleEntry | None:
        """Get the next schedule entry after the current one."""
        current_entry = self.get_current_schedule_entry()
        if not current_entry or not self.schedule:
            return None

        # Find current entry's index
        try:
            current_index = self.schedule.index(current_entry)
        except ValueError:
            return None

        # Get next entry (wrapping around to start of schedule)
        next_index = (current_index + 1) % len(self.schedule)
        return self.schedule[next_index]

    def get_schedule_entry_at_time(self, dt: datetime) -> ScheduleEntry | None:
        """Get the schedule entry that should be active at a specific datetime."""
        check_time = dt.time()
        for entry in self.schedule:
            if self._time_in_range(check_time, entry.start_time, entry.end_time):
                return entry
        return None

    def get_dj_config(self, dj_name: str) -> DJConfig | None:
        """Get the DJ configuration by name."""
        return self.dj_configs.get(dj_name)

    def _time_in_range(self, current: time, start: time, end: time) -> bool:
        """Check if current time is within the start and end time range.

        Uses inclusive start and exclusive end: [start, end)
        This ensures that at exactly the boundary time (e.g., 16:00),
        the next DJ's show is selected, not the ending show.
        """
        if start == end:
            # Equal boundaries mean a 24-hour slot, not an empty one
            return True
        if start <= end:
            # Same day range - use exclusive end
            return start <= current < end
        else:
            # Overnight range (crosses midnight)
            return current >= start or current < end

    def get_music_folders(self) -> list[str]:
        """Get music folders for the current time slot."""
        current_entry = self.get_current_schedule_entry()
        if current_entry:
            return current_entry.music_folders
        return []

    def reload_station_config(self):
        """Reload station configuration."""
        logger.info("🔄 Reloading station configuration...")
        try:
            self._load_station_config()
            logger.info("✅ Station config reloaded successfully")
        except Exception as e:
            logger.error(f"❌ Error reloading station config: {e}")

    def reload_schedule(self):
        """Reload schedule configuration."""
        logger.info("🔄 Reloading schedule configuration...")
        try:
            self._load_schedule()
            logger.info("✅ Schedule reloaded successfully")
        except Exception as e:
            logger.error(f"❌ Error reloading schedule: {e}")

    def reload_dj_configs(self):
        """Reload all DJ configurations."""
        logger.info("🔄 Reloading DJ configurations...")
        try:
            self.dj_configs = {}
            self._load_dj_configs()
            logger.info(f"✅ DJ configs reloaded: {len(self.dj_configs)} DJs")
        except Exception as e:
            logger.error(f"❌ Error reloading DJ configs: {e}")

    def reload_configs(self):
        """Reload all configuration files."""
        logger.info("Reloading configurations...")
        self._load_configs()
