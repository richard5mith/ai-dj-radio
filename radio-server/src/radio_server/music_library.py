import asyncio
import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.id3 import ID3NoHeaderError

from .audio_utils import get_audio_duration

logger = logging.getLogger(__name__)


@dataclass
class MusicTrack:
    """Represents a music track with metadata."""

    file_path: str
    metadata: dict[str, Any]
    duration: float


class MusicLibrary:
    """Manages the music library and track selection."""

    def __init__(self):
        self.tracks: list[MusicTrack] = []
        self.track_paths: set[str] = set()
        self.current_folders: list[str] = []
        self.last_played_tracks: list[str] = []
        self.max_history = 50  # Avoid repeating recent tracks
        self.seen_songs = set()  # Track (artist, title) to prevent duplicate songs

    async def initialize(self, music_folders: list[str]):
        """Initialize the music library with tracks from specified folders."""
        logger.info(f"Initializing music library with folders: {music_folders}")

        self.current_folders = music_folders
        self.tracks = []
        self.track_paths = set()
        self.seen_songs = set()  # Track (artist, title) to prevent duplicate songs

        for folder in music_folders:
            await self._scan_folder(folder)
            await asyncio.sleep(0)

        logger.info(f"Music library initialized with {len(self.tracks)} unique tracks")

    async def _scan_folder(self, folder_path: str):
        """Scan a folder for music files and extract metadata."""
        folder = Path("/app/music") / folder_path

        if not folder.exists():
            logger.warning(f"Music folder not found: {folder}")
            return

        logger.debug(f"Scanning folder: {folder}")

        # Find all supported audio files recursively
        supported_extensions = ["*.mp3", "*.m4a", "*.mp4", "*.aac"]
        audio_files = []

        for extension in supported_extensions:
            audio_files.extend(folder.rglob(extension))
            # Also check uppercase extensions
            audio_files.extend(folder.rglob(extension.upper()))

        for index, audio_file in enumerate(audio_files):
            try:
                track = await self._process_music_file(audio_file)
                if track:
                    self._register_track(track)
            except Exception as e:
                logger.warning(f"Error processing {audio_file}: {e}")
            if index % 200 == 0:
                await asyncio.sleep(0)

        logger.debug(f"Found {len(audio_files)} audio files in {folder}")

    async def _process_music_file(self, file_path: Path) -> MusicTrack | None:
        """Process a single music file and extract metadata."""
        try:
            # Load file with mutagen
            audio_file = MutagenFile(str(file_path))
        except ID3NoHeaderError:
            logger.debug(f"No ID3 header found in {file_path}, using fallback metadata")
            return await self._build_fallback_track(file_path)
        except (MutagenError, OSError, struct.error, ValueError) as exc:
            logger.debug(
                f"Metadata parsing failed for {file_path}, using fallback metadata: {exc}"
            )
            return await self._build_fallback_track(file_path)

        if audio_file is None:
            logger.warning(f"Could not read audio file: {file_path}")
            return None

        try:
            # Extract metadata
            metadata = self._extract_metadata(audio_file)

            # Get duration in seconds
            duration = getattr(getattr(audio_file, "info", None), "length", 0.0) or 0.0
        except Exception as e:
            logger.debug(
                f"Metadata extraction failed for {file_path}, using fallback metadata: {e}"
            )
            return await self._build_fallback_track(file_path)

        # Mutagen sometimes returns 0 length for files it otherwise parses fine.
        # Fall back to ffprobe so the timeline scheduler has a real estimate;
        # without this, song_blocks containing zero-duration tracks scheduled
        # subsequent items at the same timestamp, causing large drift jumps.
        if duration <= 0:
            duration = await get_audio_duration(str(file_path))

        return MusicTrack(file_path=str(file_path), metadata=metadata, duration=duration)

    async def _build_fallback_track(self, file_path: Path) -> MusicTrack:
        """Build a track using path-derived metadata when tag parsing fails.

        Args:
            file_path: Absolute path to the audio file.

        Returns:
            A track with filename and folder-derived metadata.
        """
        # ffprobe can still read duration even when mutagen can't parse tags.
        duration = await get_audio_duration(str(file_path))
        return MusicTrack(
            file_path=str(file_path),
            metadata=self._fallback_metadata(file_path),
            duration=duration,
        )

    def _fallback_metadata(self, file_path: Path) -> dict[str, str]:
        """Infer minimal metadata from the file path.

        Args:
            file_path: Absolute path to the audio file.

        Returns:
            Metadata inferred from the filename and parent folders.
        """
        metadata = {
            "title": file_path.stem,
            "artist": "Unknown Artist",
            "album": "Unknown Album",
        }

        try:
            relative_file = file_path.relative_to(Path("/app/music"))
        except ValueError:
            return metadata

        parent_parts = relative_file.parts[:-1]
        if len(parent_parts) >= 2:
            metadata["artist"] = parent_parts[-2]
            metadata["album"] = parent_parts[-1]
        elif len(parent_parts) == 1:
            metadata["album"] = parent_parts[-1]

        return metadata

    def _extract_metadata(self, audio_file) -> dict[str, Any]:
        """Extract metadata from an audio file."""
        metadata = {}

        if not hasattr(audio_file, "tags") or not audio_file.tags:
            # No tags available
            pass
        elif hasattr(audio_file.tags, "get"):
            # M4A/MP4 tags - try multiple possible tag formats
            tag_keys_to_try = {
                "title": ["\xa9nam", "©nam", "TIT2"],
                "artist": ["\xa9ART", "©ART", "TPE1"],
                "album": ["\xa9alb", "©alb", "TALB"],
                "albumartist": ["aART", "TPE2"],
                "date": ["\xa9day", "©day", "TDRC"],
                "genre": ["\xa9gen", "©gen", "gnre", "TCON"],
                "tracknumber": ["trkn", "TRCK"],
                "discnumber": ["disk", "TPOS"],
            }

            # Try to find values for each metadata field
            for metadata_key, possible_tags in tag_keys_to_try.items():
                if metadata_key not in metadata:  # Don't overwrite if already found
                    for tag_id in possible_tags:
                        if tag_id in audio_file.tags:
                            tag_value = audio_file.tags[tag_id]
                            if isinstance(tag_value, list) and tag_value:
                                metadata[metadata_key] = str(tag_value[0])
                            else:
                                metadata[metadata_key] = str(tag_value)
                            break  # Found a value, move to next metadata field
        else:
            # ID3 tags (MP3)
            id3_tag_mappings = {
                "TIT2": "title",  # Title
                "TPE1": "artist",  # Artist
                "TALB": "album",  # Album
                "TPE2": "albumartist",  # Album Artist
                "TDRC": "date",  # Recording Date
                "TCON": "genre",  # Genre
                "TRCK": "tracknumber",  # Track Number
                "TPOS": "discnumber",  # Disc Number
            }

            # Extract ID3 tags
            for tag_id, metadata_key in id3_tag_mappings.items():
                if tag_id in audio_file.tags:
                    tag_value = audio_file.tags[tag_id]
                    if hasattr(tag_value, "text"):
                        metadata[metadata_key] = (
                            str(tag_value.text[0]) if tag_value.text else ""
                        )
                    else:
                        metadata[metadata_key] = str(tag_value)

        # Fallback for missing essential metadata
        if "title" not in metadata or not metadata["title"]:
            # Use filename as title
            file_path = (
                Path(audio_file.filename)
                if hasattr(audio_file, "filename")
                else Path("unknown")
            )
            metadata["title"] = file_path.stem

        if "artist" not in metadata or not metadata["artist"]:
            metadata["artist"] = "Unknown Artist"

        if "album" not in metadata or not metadata["album"]:
            metadata["album"] = "Unknown Album"

        return metadata

    def get_all_tracks(self) -> list[MusicTrack]:
        """Get all tracks in the library."""
        return list(self.tracks)

    def has_file(self, file_path: str) -> bool:
        """Check if a file path is already tracked in the library."""
        return file_path in self.track_paths

    def _register_track(self, track: MusicTrack) -> bool:
        """Add a track to the library if it is not a duplicate.

        Args:
            track: Track to add to the in-memory library.

        Returns:
            True if the track was added, otherwise False.
        """
        song_id = self._song_key(track.metadata)
        if song_id in self.seen_songs:
            logger.debug(
                f"Skipping duplicate song: {song_id[0]} - {song_id[1]} (found at {track.file_path})"
            )
            return False

        self.tracks.append(track)
        self.track_paths.add(track.file_path)
        self.seen_songs.add(song_id)
        return True

    def _song_key(self, metadata: dict[str, Any]) -> tuple[str, str]:
        """Normalize the song key for duplicate detection."""
        artist = metadata.get("artist", "Unknown Artist").lower().strip()
        title = metadata.get("title", "Unknown").lower().strip()
        return (artist, title)

    async def add_single_file(self, file_path: str) -> bool:
        """Add a single music file to the library without full reload.

        Args:
            file_path: Absolute path to the music file

        Returns:
            True if file was added, False if skipped (duplicate or invalid)
        """
        try:
            path = Path(file_path)
            if not path.exists():
                logger.warning(f"File not found: {file_path}")
                return False

            if self.has_file(file_path):
                logger.debug(f"Skipping known file: {file_path}")
                return False

            track = await self._process_music_file(path)
            if not track:
                return False

            if not self._register_track(track):
                return False

            logger.info(
                f"✅ Added new track: {track.metadata['artist']} - {track.metadata['title']} ({track.duration:.1f}s)"
            )
            return True

        except Exception as e:
            logger.error(f"Error adding music file {file_path}: {e}")
            return False

    def remove_single_file(self, file_path: str) -> bool:
        """Remove a single music file from the library if it exists."""
        if file_path not in self.track_paths:
            return False

        for index, track in enumerate(self.tracks):
            if track.file_path == file_path:
                removed_track = self.tracks.pop(index)
                self.track_paths.discard(file_path)
                song_key = self._song_key(removed_track.metadata)
                if not any(self._song_key(t.metadata) == song_key for t in self.tracks):
                    self.seen_songs.discard(song_key)
                logger.info(
                    f"🗑️ Removed track: {removed_track.metadata.get('artist', 'Unknown Artist')} - "
                    f"{removed_track.metadata.get('title', 'Unknown')}"
                )
                return True

        self.track_paths.discard(file_path)
        return False

    async def reload(self) -> None:
        """Reload the music library with current folders."""
        logger.info("🔄 Reloading music library...")
        await self.initialize(self.current_folders)
