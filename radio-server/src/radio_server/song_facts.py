import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SongFactRepository:
    """Manage song fact overrides and persistent cache."""

    def __init__(self, config_dir: Path, cache_dir: Path | None = None) -> None:
        self.config_dir = config_dir
        self.cache_dir = cache_dir or config_dir
        self.overrides_path = self.config_dir / "song_facts.json"
        self.cache_path = self.cache_dir / "song_facts_cache.json"
        self._overrides_tracks: dict[str, list[str]] = {}
        self._overrides_artists: dict[str, list[str]] = {}
        self._blocked: set[str] = set()
        self._cache: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

        self._load_overrides()
        self._load_cache()

    def build_track_key(self, artist: str, title: str, album: str | None) -> str:
        artist_part = self._normalize_segment(artist)
        title_part = self._normalize_segment(title)
        album_part = self._normalize_segment(album or "")
        return "|".join([artist_part, title_part, album_part])

    def get_override(
        self, artist: str, title: str, album: str | None
    ) -> list[str] | None:
        for key in self._candidate_track_keys(artist, title, album):
            facts = self._overrides_tracks.get(key)
            if facts:
                return list(facts)
        artist_key = self._normalize_segment(artist)
        if artist_key and artist_key in self._overrides_artists:
            return list(self._overrides_artists[artist_key])
        return None

    def get_cached(self, track_key: str) -> list[str] | None:
        normalized = self._normalize_segment(track_key)
        if normalized in self._cache:
            return list(self._cache[normalized])
        return None

    async def store_cache(self, track_key: str, facts: list[str]) -> None:
        normalized = self._normalize_segment(track_key)
        sanitized = self._sanitize_facts(facts)
        async with self._lock:
            if self._cache.get(normalized) == sanitized:
                return
            self._cache[normalized] = sanitized
            await asyncio.to_thread(self._write_cache)

    def snapshot_cache(self) -> dict[str, list[str]]:
        return {key: list(values) for key, values in self._cache.items()}

    def should_skip_api(self, artist: str, title: str, album: str | None) -> bool:
        blocked_candidates = self._blocked
        for key in self._candidate_track_keys(artist, title, album):
            if key in blocked_candidates:
                return True
        artist_key = self._normalize_segment(artist)
        return artist_key in blocked_candidates

    def _candidate_track_keys(
        self, artist: str, title: str, album: str | None
    ) -> list[str]:
        artist_part = self._normalize_segment(artist)
        title_part = self._normalize_segment(title)
        album_part = self._normalize_segment(album or "")

        candidates: list[str] = []
        if artist_part and title_part and album_part:
            candidates.append("|".join([artist_part, title_part, album_part]))
        if artist_part and title_part:
            candidates.append("|".join([artist_part, title_part, ""]))
        if artist_part:
            candidates.append(artist_part)
        return candidates

    def _load_overrides(self) -> None:
        data = self._read_json(self.overrides_path)
        if not isinstance(data, dict):
            self._overrides_tracks = {}
            self._overrides_artists = {}
            self._blocked = set()
            return

        tracks = data.get("tracks", {})
        if isinstance(tracks, dict):
            self._overrides_tracks = {
                self._normalize_segment(key): self._sanitize_facts(value)
                for key, value in tracks.items()
                if isinstance(key, str)
            }
        else:
            self._overrides_tracks = {}

        artists = data.get("artists", {})
        if isinstance(artists, dict):
            self._overrides_artists = {
                self._normalize_segment(key): self._sanitize_facts(value)
                for key, value in artists.items()
                if isinstance(key, str)
            }
        else:
            self._overrides_artists = {}

        blocked_raw = data.get("blocked", [])
        if isinstance(blocked_raw, list):
            self._blocked = {
                self._normalize_segment(entry)
                for entry in blocked_raw
                if isinstance(entry, str)
            }
        else:
            self._blocked = set()

    def _load_cache(self) -> None:
        data = self._read_json(self.cache_path)
        if isinstance(data, dict):
            self._cache = {
                self._normalize_segment(key): self._sanitize_facts(value)
                for key, value in data.items()
                if isinstance(key, str)
            }
        else:
            self._cache = {}

    def _write_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("w", encoding="utf-8") as handle:
                json.dump(self._cache, handle, indent=2, ensure_ascii=True)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Failed to write song fact cache: %s", exc)

    def _read_json(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            return None

    def _sanitize_facts(self, values: list[str]) -> list[str]:
        sanitized: list[str] = []
        for value in values[:3] if isinstance(values, list) else []:
            if isinstance(value, (str, int, float)):
                text = str(value).strip()
                if text:
                    sanitized.append(text)
        return sanitized

    def _normalize_segment(self, value: str | None) -> str:
        if value is None:
            return ""
        return value.strip().lower()
