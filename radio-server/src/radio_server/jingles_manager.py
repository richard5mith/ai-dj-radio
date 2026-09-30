"""Jingles manager — handles jingle selection and playback logic."""

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from .audio_utils import get_audio_duration_sync

logger = logging.getLogger(__name__)

JINGLES_CONFIG_PATH = Path("/app/config/jingles.json")
JINGLES_BASE_PATH = Path("/app/jingles")
JINGLE_AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".mp4",
    ".aac",
}
DJ_SPECIFIC_STING_WEIGHT = 0.6
GLOBAL_STING_WEIGHT = 0.5


@dataclass
class Jingle:
    """Represents a jingle configuration"""

    id: str
    name: str
    file: str
    talk_types: list[str]
    position: str  # 'before', 'after', 'sting'
    allowed_djs: list[str]
    weight: float
    duration: float = field(default=0.0)

    @property
    def file_path(self) -> Path:
        """Get the full file path for the jingle"""
        return JINGLES_BASE_PATH / self.file


class JinglesManager:
    """Manages jingle selection and configuration"""

    def __init__(self):
        self.jingles: list[Jingle] = []
        self.jingles_by_talk_type: dict[str, list[Jingle]] = {}
        self.load_jingles()

    def load_jingles(self) -> None:
        """Load jingles configuration from file"""
        self.jingles = []
        self.jingles_by_talk_type = {}

        try:
            if not JINGLES_CONFIG_PATH.exists():
                logger.warning(
                    f"Jingles config not found: {JINGLES_CONFIG_PATH}, no jingles will be used"
                )
            else:
                self._load_config_jingles()
        except Exception as e:
            logger.error(f"Error loading jingles config: {e}")
            self.jingles = []
            self.jingles_by_talk_type = {}
            return

        self._load_auto_discovered_stings()

        logger.info(
            "✅ Loaded %s jingles for %s talk types",
            len(self.jingles),
            len(self.jingles_by_talk_type),
        )

    def _load_config_jingles(self) -> None:
        """
        Load explicit jingles from config.

        Only `before` and `after` positions are allowed in config;
        stings are auto-discovered from folder structure.
        """
        with JINGLES_CONFIG_PATH.open() as file_handle:
            config = json.load(file_handle)

        for jingle_data in config.get("jingles", []):
            position = self._normalize_position(jingle_data["position"])
            if position not in {"before", "after"}:
                logger.debug(
                    "Skipping configured jingle '%s' with position '%s' (stings are auto-discovered)",
                    jingle_data.get("name", jingle_data.get("id", "unknown")),
                    jingle_data.get("position"),
                )
                continue

            jingle = Jingle(
                id=jingle_data["id"],
                name=jingle_data["name"],
                file=jingle_data["file"],
                talk_types=jingle_data["talk_types"],
                position=position,
                allowed_djs=jingle_data["allowed_djs"],
                weight=jingle_data.get("weight", 1.0),
            )
            self._register_jingle_if_exists(jingle)

    def _load_auto_discovered_stings(self) -> None:
        """
        Load stings from `/app/jingles/global` and `/app/jingles/<dj_id>`.

        Global stings are available to all DJs, DJ-folder stings are DJ-specific.
        """
        if not JINGLES_BASE_PATH.exists():
            logger.warning(f"Jingles directory not found: {JINGLES_BASE_PATH}")
            return

        global_count = self._load_stings_from_folder(
            folder=JINGLES_BASE_PATH / "global",
            allowed_djs=["all"],
            weight=GLOBAL_STING_WEIGHT,
        )
        dj_specific_count = 0

        for folder in sorted(JINGLES_BASE_PATH.iterdir()):
            if not folder.is_dir() or folder.name == "global":
                continue
            dj_specific_count += self._load_stings_from_folder(
                folder=folder,
                allowed_djs=[folder.name],
                weight=DJ_SPECIFIC_STING_WEIGHT,
            )

        logger.info(
            "🎯 Auto-discovered %s stings (%s global, %s DJ-specific)",
            global_count + dj_specific_count,
            global_count,
            dj_specific_count,
        )

    def _load_stings_from_folder(
        self,
        folder: Path,
        allowed_djs: list[str],
        weight: float,
    ) -> int:
        """
        Load every supported audio file in a folder as a sting.

        Args:
            folder: Folder containing sting audio files.
            allowed_djs: DJs allowed to use these stings.
            weight: Selection weight for all loaded stings in this folder.

        Returns:
            Number of stings loaded from this folder.
        """
        if not folder.exists():
            return 0

        loaded = 0
        for audio_file in sorted(folder.iterdir()):
            if not self._is_supported_audio_file(audio_file):
                continue

            try:
                relative_path = audio_file.relative_to(JINGLES_BASE_PATH)
            except ValueError:
                logger.warning(
                    "Skipping sting outside jingles base path: %s", audio_file
                )
                continue

            jingle = Jingle(
                id=f"sting_{self._slugify_path(relative_path)}",
                name=audio_file.stem.replace("_", " "),
                file=str(relative_path),
                talk_types=["station_id", "sting"],
                position="sting",
                allowed_djs=allowed_djs,
                weight=weight,
            )
            self._register_jingle_if_exists(jingle)
            loaded += 1

        return loaded

    def _register_jingle_if_exists(self, jingle: Jingle) -> None:
        """Register a jingle if its source file exists."""
        if not jingle.file_path.exists():
            logger.warning(
                f"Jingle file not found: {jingle.file_path}, skipping jingle '{jingle.name}'"
            )
            return

        # Probe real duration once at load time so the timeline can be scheduled
        # with accurate offsets instead of an 8s/1.5s placeholder. Jingles are
        # static files, so this is a one-shot cost paid at startup.
        if jingle.duration <= 0:
            jingle.duration = get_audio_duration_sync(str(jingle.file_path))
            if jingle.duration <= 0:
                logger.warning(
                    f"Could not determine duration for jingle '{jingle.name}' at {jingle.file_path}; "
                    f"scheduling will use placeholder until preparation"
                )

        self.jingles.append(jingle)
        for talk_type in jingle.talk_types:
            if talk_type not in self.jingles_by_talk_type:
                self.jingles_by_talk_type[talk_type] = []
            self.jingles_by_talk_type[talk_type].append(jingle)

    def _normalize_position(self, position: str) -> str:
        """
        Normalize position name.

        Positions are lowercased for matching.
        """
        return position.strip().lower()

    def _is_supported_audio_file(self, file_path: Path) -> bool:
        """Return True when path is a file with a supported audio extension."""
        return file_path.is_file() and file_path.suffix.lower() in JINGLE_AUDIO_EXTENSIONS

    def _slugify_path(self, file_path: Path) -> str:
        """Build a stable identifier fragment from a relative path."""
        return str(file_path).replace("/", "_").replace(" ", "_").replace(".", "_")

    def get_jingle_for_talk_type(
        self, talk_type: str, dj_id: str, position: str | None = None
    ) -> Jingle | None:
        """
        Get a random jingle for the given talk type and DJ.

        Args:
            talk_type: The DJ talk type (e.g., 'weather', 'news', 'station_id')
            dj_id: The current DJ's ID
            position: Optional filter for position ('before', 'after', 'sting')

        Returns:
            A randomly selected jingle, or None if no suitable jingle found
        """
        if talk_type not in self.jingles_by_talk_type:
            return None

        normalized_position = self._normalize_position(position) if position else None

        # Filter jingles for this DJ and position
        eligible_jingles = []
        for jingle in self.jingles_by_talk_type[talk_type]:
            # Check if DJ is allowed
            if "all" not in jingle.allowed_djs and dj_id not in jingle.allowed_djs:
                continue

            # Check position if specified
            if normalized_position and jingle.position != normalized_position:
                continue

            eligible_jingles.append(jingle)

        if not eligible_jingles:
            return None

        # Weight-based random selection
        weights = [j.weight for j in eligible_jingles]
        selected = random.choices(eligible_jingles, weights=weights, k=1)[0]

        logger.info(
            f"🎵 Selected jingle '{selected.name}' for talk_type '{talk_type}' (position: {selected.position})"
        )
        return selected

    def has_jingles_for_talk_type(self, talk_type: str, dj_id: str) -> bool:
        """Check if there are any jingles available for this talk type and DJ"""
        if talk_type not in self.jingles_by_talk_type:
            return False

        for jingle in self.jingles_by_talk_type[talk_type]:
            if "all" in jingle.allowed_djs or dj_id in jingle.allowed_djs:
                return True

        return False
