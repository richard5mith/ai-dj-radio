"""
Professional radio audio mixing operations.
Provides broadcast-quality audio mixing using FFmpeg with consistent quality settings.
"""

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio_utils import get_audio_duration
from .exceptions import AudioProcessingError
from .ffmpeg_builder import (
    AudioMixingBuilder,
    AudioOutputConfig,
    FFmpegCommandBuilder,
)

logger = logging.getLogger(__name__)


class AudioConstants:
    """Broadcasting standard audio constants - all magic numbers centralized here."""

    # Normalization targets (EBU R128 standards)
    MUSIC_LUFS = -16.0  # Standard music loudness
    DJ_LUFS = -12.0  # Higher for voice clarity
    FINAL_MIX_LUFS = -18.0  # Slightly lower for headroom

    # Volume levels
    MUSIC_DUCK_VOLUME = 0.3  # 30% volume during DJ intro
    MUSIC_DUCK_VOLUME_OUTRO = 0.3  # 30% volume during DJ outro (less aggressive)

    # Fade durations
    FADE_IN_DURATION = 2  # Quick fade in
    FADE_OUT_DURATION = 3.0  # Smooth fade out

    # DJ overlap durations (seconds)
    INTRO_OVERLAP_MIN = 0.0  # Minimum overlap at start of song
    INTRO_OVERLAP_MAX = 4.0  # Maximum overlap at start of song
    OUTRO_OVERLAP_MIN = 0.0  # Minimum overlap at end of song (how early DJ starts)
    OUTRO_OVERLAP_MAX = 4.0  # Maximum overlap at end of song (how early DJ starts)


class FilterChainBuilder:
    """
    Build FFmpeg filter_complex chains using composable primitives.
    Eliminates repetitive filter string construction.
    """

    @staticmethod
    def normalize(input_label: str, output_label: str, lufs: float) -> str:
        """Create loudness normalization filter."""
        return f"[{input_label}]loudnorm=I={lufs}:TP=-1.5:LRA=11[{output_label}]"

    @staticmethod
    def volume(input_label: str, output_label: str, level: float) -> str:
        """Adjust volume level (0.0-1.0)."""
        return f"[{input_label}]volume={level}[{output_label}]"

    @staticmethod
    def pad(input_label: str, output_label: str, duration: float) -> str:
        """Pad audio to specific duration with silence."""
        return f"[{input_label}]apad=whole_dur={duration}[{output_label}]"

    @staticmethod
    def delay(input_label: str, output_label: str, delay_ms: int) -> str:
        """Delay audio by milliseconds."""
        return f"[{input_label}]adelay={delay_ms}|{delay_ms}[{output_label}]"

    @staticmethod
    def split(input_label: str, output_labels: list[str]) -> str:
        """Split audio into multiple streams."""
        outputs = "".join(f"[{label}]" for label in output_labels)
        return f"[{input_label}]asplit={len(output_labels)}{outputs}"

    @staticmethod
    def mix(
        input_labels: list[str],
        output_label: str,
        duration: str = "first",
        weights: list[float] | None = None,
    ) -> str:
        """Mix multiple audio streams with optional per-input weights."""
        inputs = "".join(f"[{label}]" for label in input_labels)
        mix_params = f"inputs={len(input_labels)}:duration={duration}"
        if weights:
            weights_str = " ".join(str(w) for w in weights)
            mix_params += f":weights={weights_str}"
        return f"{inputs}amix={mix_params}[{output_label}]"

    @staticmethod
    def chain(filters: list[str]) -> str:
        """Chain multiple filters together with semicolons."""
        return ";".join(filters)

    @staticmethod
    def pipe(input_label: str, operations: list[str], output_label: str) -> str:
        """
        Pipe operations on a single stream (comma-separated operations).
        Example: pipe("0:a", ["loudnorm=I=-16:TP=-1.5:LRA=11", "volume=0.5"], "out")
        Returns: [0:a]loudnorm=I=-16:TP=-1.5:LRA=11,volume=0.5[out]
        """
        ops = ",".join(operations)
        return f"[{input_label}]{ops}[{output_label}]"

    @staticmethod
    def normalize_and_trim_start(
        input_label: str, output_label: str, lufs: float, start_time: float
    ) -> str:
        """Combined normalize and trim from start (common pattern)."""
        ops = [
            f"atrim=start={start_time}",
            "asetpts=PTS-STARTPTS",
            f"loudnorm=I={lufs}:LRA=11:tp=-1.5",
        ]
        return FilterChainBuilder.pipe(input_label, ops, output_label)

    @staticmethod
    def normalize_and_trim_end(
        input_label: str, output_label: str, lufs: float, end_time: float
    ) -> str:
        """Combined normalize and trim to end (common pattern)."""
        ops = [f"atrim=end={end_time}", f"loudnorm=I={lufs}:LRA=11:tp=-1.5"]
        return FilterChainBuilder.pipe(input_label, ops, output_label)


@dataclass
class MixedAudioResult:
    """Result of an audio mixing operation."""

    success: bool
    output_file: str | None = None
    duration: float = 0.0
    error_message: str | None = None
    details: dict[str, Any] | None = None


class RadioAudioMixer:
    """
    Professional audio mixing for radio broadcasting.
    Handles all complex audio operations with consistent quality.
    """

    def __init__(self):
        self.ffmpeg_builder = FFmpegCommandBuilder()
        self.mixing_builder = AudioMixingBuilder(self.ffmpeg_builder)
        self.temp_dir = Path("/app/temp-audio")
        self.temp_dir.mkdir(exist_ok=True)
        self.filter_builder = FilterChainBuilder()

    def _generate_temp_filename(self, prefix: str) -> str:
        """Generate unique temporary filename."""
        timestamp = f"{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
        return str(self.temp_dir / f"{prefix}_{timestamp}.mp3")

    async def _run_filter_command(
        self,
        input_files: list[str],
        filter_complex: str,
        output_file: str,
        operation_name: str = "audio_operation",
        timeout: int = 120,
    ) -> tuple[bool, str]:
        """
        Run FFmpeg command with filter_complex (eliminates command building boilerplate).

        Args:
            input_files: List of input file paths
            filter_complex: The filter_complex string
            output_file: Output file path
            operation_name: Name for logging
            timeout: Command timeout in seconds

        Returns:
            Tuple of (success, error_message)
        """
        output_config = AudioOutputConfig()
        cmd = ["ffmpeg", "-y"]

        # Add all inputs
        for input_file in input_files:
            cmd.extend(["-i", input_file])

        # Add filter and output
        cmd.extend(
            ["-filter_complex", filter_complex, "-map", "[out]"]
            + self.ffmpeg_builder.build_output_options(output_config)
            + [output_file]
        )

        success, stdout, stderr = await self.ffmpeg_builder.run_ffmpeg_safe(
            cmd, operation_name, timeout=timeout
        )

        if not success:
            return False, f"FFmpeg {operation_name} failed: {stderr[:200]}"

        if not Path(output_file).exists():
            return False, f"Output file not created for {operation_name}"

        return True, ""

    async def _run_simple_filter_command(
        self,
        input_file: str,
        audio_filter: str,
        output_file: str,
        operation_name: str = "audio_operation",
        start_time: float | None = None,
        duration: float | None = None,
        timeout: int = 60,
    ) -> tuple[bool, str]:
        """
        Run FFmpeg command with simple -af filter (for single-input operations).

        Args:
            input_file: Input file path
            audio_filter: The -af filter string (comma-separated operations)
            output_file: Output file path
            operation_name: Name for logging
            start_time: Optional start time (-ss) in seconds
            duration: Optional duration (-t) in seconds
            timeout: Command timeout in seconds

        Returns:
            Tuple of (success, error_message)
        """
        output_config = AudioOutputConfig()
        cmd = ["ffmpeg", "-y"]

        # Add start time if specified
        if start_time is not None:
            cmd.extend(["-ss", str(start_time)])

        # Add input
        cmd.extend(["-i", input_file])

        # Suppress any video streams (e.g. embedded album art in MP3s)
        cmd.append("-vn")

        # Add duration if specified
        if duration is not None:
            cmd.extend(["-t", str(duration)])

        # Add audio filter and output
        cmd.extend(
            ["-af", audio_filter]
            + self.ffmpeg_builder.build_output_options(output_config)
            + [output_file]
        )

        success, stdout, stderr = await self.ffmpeg_builder.run_ffmpeg_safe(
            cmd, operation_name, timeout=timeout
        )

        if not success:
            return False, f"FFmpeg {operation_name} failed: {stderr[:200]}"

        if not Path(output_file).exists():
            return False, f"Output file not created for {operation_name}"

        return True, ""

    async def _run_simple_copy_command(
        self,
        input_file: str,
        output_file: str,
        operation_name: str = "audio_copy",
        start_time: float | None = None,
        duration: float | None = None,
        timeout: int = 60,
    ) -> tuple[bool, str]:
        """
        Run FFmpeg command for simple copy/trim operations (no filtering).

        Args:
            input_file: Input file path
            output_file: Output file path
            operation_name: Name for logging
            start_time: Optional start time (-ss) in seconds
            duration: Optional duration (-t) in seconds
            timeout: Command timeout in seconds

        Returns:
            Tuple of (success, error_message)
        """
        output_config = AudioOutputConfig()
        cmd = ["ffmpeg", "-y"]

        # Add start time if specified
        if start_time is not None:
            cmd.extend(["-ss", str(start_time)])

        # Add input
        cmd.extend(["-i", input_file])

        # Suppress any video streams (e.g. embedded album art in MP3s)
        cmd.append("-vn")

        # Add duration if specified
        if duration is not None:
            cmd.extend(["-t", str(duration)])

        # Add output
        cmd.extend(
            self.ffmpeg_builder.build_output_options(output_config) + [output_file]
        )

        success, stdout, stderr = await self.ffmpeg_builder.run_ffmpeg_safe(
            cmd, operation_name, timeout=timeout
        )

        if not success:
            return False, f"FFmpeg {operation_name} failed: {stderr[:200]}"

        if not Path(output_file).exists():
            return False, f"Output file not created for {operation_name}"

        return True, ""

    async def create_intro_mix(
        self, song_path: str, dj_audio_path: str, metadata: dict
    ) -> MixedAudioResult:
        """
        Create professional intro mix: DJ talk with random overlap on song intro.

        Example: 20s DJ talk, 8s random overlap:
        - 12s DJ talk over silence
        - 8s DJ talk over ducked song intro
        - Rest of song at full volume

        Returns only the intro portion (DJ talk + overlap), not the full song.

        Args:
            song_path: Path to the song file
            dj_audio_path: Path to DJ intro audio
            metadata: Song metadata (title, artist, etc.)

        Returns:
            MixedAudioResult with output file path and duration
        """
        try:
            output_file = self._generate_temp_filename("mixed")

            # Get durations
            song_duration = await get_audio_duration(song_path)
            dj_duration = await get_audio_duration(dj_audio_path)

            if song_duration <= 0 or dj_duration <= 0:
                return MixedAudioResult(
                    success=False,
                    error_message="Invalid audio file durations",
                )

            # DJ starts at song start up to a few seconds in
            dj_start = random.uniform(
                AudioConstants.INTRO_OVERLAP_MIN, AudioConstants.INTRO_OVERLAP_MAX
            )
            dj_start = max(0.0, min(dj_start, max(0.0, song_duration - 1.0)))

            intro_segment_duration = dj_start + dj_duration
            logger.info(
                f"🎙️ Intro mix: {dj_duration:.1f}s DJ starts at t=+{dj_start:.1f}s, "
                f"total {intro_segment_duration:.1f}s"
            )

            # Ensure we don't exceed song duration
            if intro_segment_duration >= song_duration:
                intro_segment_duration = song_duration * 0.8
                dj_start = max(0.0, intro_segment_duration - dj_duration)

            # Build filter using primitives
            fc = self.filter_builder

            filters = [
                # Normalize both inputs
                fc.normalize("0:a", "music", AudioConstants.MUSIC_LUFS),
                fc.normalize("1:a", "dj", AudioConstants.DJ_LUFS),
                # Take intro-length slice from song and duck it
                fc.pipe(
                    "music",
                    [
                        f"atrim=0:{intro_segment_duration}",
                        f"volume={AudioConstants.MUSIC_DUCK_VOLUME}",
                    ],
                    "music_ducked",
                ),
                # Delay DJ to start after the song begins
                fc.delay("dj", "dj_delayed", int(dj_start * 1000)),
                # Mix DJ with ducked music
                fc.mix(
                    ["dj_delayed", "music_ducked"],
                    "out",
                    duration="first",
                    weights=[1.0, 1.0],
                ),
            ]

            filter_complex = fc.chain(filters)

            # Run command using helper
            success, error = await self._run_filter_command(
                [song_path, dj_audio_path], filter_complex, output_file, "intro_mix"
            )

            if not success:
                return MixedAudioResult(success=False, error_message=error)

            duration = await get_audio_duration(output_file)

            logger.info(
                f"✅ Created intro mix: {metadata.get('title', 'Unknown')} ({duration:.1f}s)"
            )

            return MixedAudioResult(
                success=True, output_file=output_file, duration=duration
            )

        except AudioProcessingError as e:
            logger.error(f"Audio processing error in intro mix: {e}")
            return MixedAudioResult(success=False, error_message=str(e))
        except Exception as e:
            logger.error(f"Unexpected error in intro mix ({type(e).__name__}): {e}")
            return MixedAudioResult(success=False, error_message=str(e))

    async def create_outro_mix(
        self, song_path: str, dj_audio_path: str, metadata: dict
    ) -> MixedAudioResult:
        """
        Create professional outro mix: DJ talk over song ending.

        The full song plays, then DJ voice comes in over the ending with music ducked.

        Args:
            song_path: Path to the song file
            dj_audio_path: Path to DJ outro audio
            metadata: Song metadata

        Returns:
            MixedAudioResult with output file path and duration
        """
        try:
            output_file = self._generate_temp_filename("mixed_outro")

            # Get durations
            song_duration = await get_audio_duration(song_path)
            dj_duration = await get_audio_duration(dj_audio_path)

            if song_duration <= 0 or dj_duration <= 0:
                return MixedAudioResult(
                    success=False, error_message="Invalid audio durations"
                )

            # Calculate outro timing:
            # OUTRO_OVERLAP_* is how close to the end the DJ should finish (0-4s before end).
            outro_overlap = random.uniform(
                AudioConstants.OUTRO_OVERLAP_MIN, AudioConstants.OUTRO_OVERLAP_MAX
            )
            outro_overlap = min(outro_overlap, max(0.0, song_duration))

            dj_end_target = max(0.0, song_duration - outro_overlap)
            dj_start_time = max(0.0, dj_end_target - dj_duration)

            # DJ audio plays for its full duration starting at dj_start_time
            dj_end_time = dj_start_time + dj_duration

            # Final duration: enough to contain both the full song AND the full DJ audio
            final_duration = max(song_duration, dj_end_time)

            logger.info(
                f"🎙️ Outro mix: song={song_duration:.1f}s, DJ={dj_duration:.1f}s starts at t={dj_start_time:.1f}s, "
                f"ends at t={dj_end_time:.1f}s (target end t={dj_end_target:.1f}s), final={final_duration:.1f}s"
            )

            # Calculate how much to pad the music (if DJ extends beyond song)
            music_pad_duration = max(0, final_duration - song_duration)

            # Music should fade down from 100% to ducked volume when DJ starts
            # Use a short fade duration for smooth transition
            duck_fade_duration = (
                AudioConstants.FADE_IN_DURATION
            )  # 2 seconds to duck down

            # Build filter using primitives
            fc = self.filter_builder
            filters = [
                # Normalize inputs
                fc.normalize("0:a", "music", AudioConstants.MUSIC_LUFS),
                fc.normalize("1:a", "dj", AudioConstants.DJ_LUFS),
                # Pad music with silence if DJ extends beyond song
                fc.pad("music", "music_padded", music_pad_duration),
                # Fade music volume from 100% down to ducked level when DJ starts
                # Before dj_start_time: 1.0
                # During fade (dj_start_time to dj_start_time+duck_fade_duration): ramp from 1.0 to MUSIC_DUCK_VOLUME_OUTRO
                # After fade: MUSIC_DUCK_VOLUME_OUTRO
                fc.pipe(
                    "music_padded",
                    [
                        f"volume='if(lt(t,{dj_start_time}),1.0,"
                        f"if(lt(t,{dj_start_time + duck_fade_duration}),"
                        f"1.0-((1.0-{AudioConstants.MUSIC_DUCK_VOLUME_OUTRO})*(t-{dj_start_time})/{duck_fade_duration}),"
                        f"{AudioConstants.MUSIC_DUCK_VOLUME_OUTRO}))':eval=frame"
                    ],
                    "music_ducked",
                ),
                # Delay DJ to start at dj_start_time
                fc.delay("dj", "dj_delayed", int(dj_start_time * 1000)),
                # Mix DJ over the ducked music - use "longest" to avoid cutting DJ speech
                fc.mix(
                    ["music_ducked", "dj_delayed"],
                    "out",
                    duration="longest",
                    weights=[1.0, 1.0],
                ),
            ]

            filter_complex = fc.chain(filters)

            # Run command using helper
            success, error = await self._run_filter_command(
                [song_path, dj_audio_path], filter_complex, output_file, "outro_mix"
            )

            if not success:
                return MixedAudioResult(success=False, error_message=error)

            duration = await get_audio_duration(output_file)

            logger.info(
                f"✅ Created outro mix: {metadata.get('title', 'Unknown')} ({duration:.1f}s)"
            )

            return MixedAudioResult(
                success=True, output_file=output_file, duration=duration
            )

        except AudioProcessingError as e:
            logger.error(f"Audio processing error in outro mix: {e}")
            return MixedAudioResult(success=False, error_message=str(e))
        except Exception as e:
            logger.error(f"Unexpected error in outro mix ({type(e).__name__}): {e}")
            return MixedAudioResult(success=False, error_message=str(e))

    async def create_bed_mix(
        self,
        dj_audio_path: str,
        bed_path: str,
        bed_volume: float = 0.2,
        bed_offset_seconds: float | None = None,
    ) -> MixedAudioResult:
        """Mix DJ talk with a looping bed track under it."""
        try:
            if not Path(dj_audio_path).exists():
                return MixedAudioResult(
                    success=False, error_message="DJ audio file not found"
                )
            if not Path(bed_path).exists():
                return MixedAudioResult(
                    success=False, error_message="Bed audio file not found"
                )

            output_file = self._generate_temp_filename("bed_mix")
            bed_volume = max(0.0, min(1.0, bed_volume))

            fc = self.filter_builder
            bed_offset_seconds = max(0.0, float(bed_offset_seconds or 0.0))
            if bed_offset_seconds > 0:
                bed_filter = fc.normalize_and_trim_start(
                    "0:a", "bed_norm", AudioConstants.MUSIC_LUFS, bed_offset_seconds
                )
            else:
                bed_filter = fc.normalize("0:a", "bed_norm", AudioConstants.MUSIC_LUFS)
            filters = [
                bed_filter,
                fc.volume("bed_norm", "bed_quiet", bed_volume),
                fc.normalize("1:a", "dj_norm", AudioConstants.DJ_LUFS),
                fc.mix(["bed_quiet", "dj_norm"], "mixed", duration="shortest"),
                fc.normalize("mixed", "out", AudioConstants.FINAL_MIX_LUFS),
            ]
            filter_str = fc.chain(filters)

            output_config = AudioOutputConfig()
            cmd = (
                [
                    "ffmpeg",
                    "-y",
                    "-stream_loop",
                    "-1",
                    "-i",
                    bed_path,
                    "-i",
                    dj_audio_path,
                    "-filter_complex",
                    filter_str,
                    "-map",
                    "[out]",
                ]
                + self.ffmpeg_builder.build_output_options(output_config)
                + [output_file]
            )

            success, _, stderr = await self.ffmpeg_builder.run_ffmpeg_safe(
                cmd, "bed_mix", timeout=60
            )

            if not success:
                return MixedAudioResult(
                    success=False,
                    error_message=f"Bed mix failed: {stderr[:200]}",
                )

            duration = await get_audio_duration(output_file)
            return MixedAudioResult(
                success=True, output_file=output_file, duration=duration
            )

        except Exception as e:
            logger.error(f"Error creating bed mix: {e}")
            return MixedAudioResult(success=False, error_message=str(e))

    async def create_crossfade_mix(
        self, song_files: list[str], crossfade_duration: float = None
    ) -> MixedAudioResult:
        """
        Create seamless crossfade between multiple songs.

        Args:
            song_files: List of song file paths to crossfade
            crossfade_duration: Crossfade duration in seconds (None = auto)

        Returns:
            MixedAudioResult with output file path and duration
        """
        try:
            if len(song_files) < 2:
                return MixedAudioResult(
                    success=False, error_message="Need at least 2 songs for crossfade"
                )

            output_file = self._generate_temp_filename("crossfade")

            # Determine crossfade duration
            if crossfade_duration is None:
                if len(song_files) == 2:
                    crossfade_duration = random.uniform(3.0, 8.0)
                else:
                    crossfade_duration = random.uniform(2.0, 5.0)

            # Use the builder's crossfade method
            success = await self.mixing_builder.crossfade_songs(
                song_files, output_file, crossfade_duration
            )

            if not success or not Path(output_file).exists():
                return MixedAudioResult(
                    success=False, error_message="Crossfade creation failed"
                )

            duration = await get_audio_duration(output_file)

            logger.info(
                f"✅ Created crossfade mix: {len(song_files)} songs ({duration:.1f}s)"
            )

            return MixedAudioResult(
                success=True,
                output_file=output_file,
                duration=duration,
                details={"crossfade_duration": crossfade_duration},
            )

        except Exception as e:
            logger.error(f"Error in crossfade mix ({type(e).__name__}): {e}")
            return MixedAudioResult(success=False, error_message=str(e))

    async def concatenate_audio(self, input_files: list[str]) -> MixedAudioResult:
        """Concatenate multiple audio files into a single output file."""
        try:
            if len(input_files) < 2:
                return MixedAudioResult(
                    success=False,
                    error_message="Need at least 2 files to concatenate",
                )

            output_file = self._generate_temp_filename("concat")
            success = await self.mixing_builder.concatenate_audio(
                input_files, output_file
            )
            if not success:
                return MixedAudioResult(
                    success=False, error_message="Concatenation failed"
                )

            duration = await get_audio_duration(output_file)
            if duration <= 0:
                return MixedAudioResult(
                    success=False,
                    error_message="Concatenation produced invalid duration",
                )

            logger.info(f"✅ Concatenated {len(input_files)} files ({duration:.1f}s)")

            return MixedAudioResult(
                success=True, output_file=output_file, duration=duration
            )

        except Exception as e:
            logger.error(f"Error concatenating audio ({type(e).__name__}): {e}")
            return MixedAudioResult(success=False, error_message=str(e))

    async def create_transition_segments(
        self, song1_path: str, song2_path: str, dj_audio_path: str
    ) -> list[str] | None:
        """
        Create transition segments: song1 ending, DJ transition, song2 beginning.

        Args:
            song1_path: Path to first song
            song2_path: Path to second song
            dj_audio_path: Path to DJ transition audio

        Returns:
            List of segment file paths, or None if failed
        """
        try:
            # Get durations
            song1_duration = await get_audio_duration(song1_path)
            song2_duration = await get_audio_duration(song2_path)
            dj_duration = await get_audio_duration(dj_audio_path)

            # Create segment 1: Last portion of song1
            segment1_file = self._generate_temp_filename("trans_seg1")
            segment1_duration = min(15.0, song1_duration * 0.3)
            segment1_start = max(0, song1_duration - segment1_duration)

            success1, error1 = await self._run_simple_filter_command(
                song1_path,
                f"afade=t=out:st={(segment1_duration - 3.0)}:d=3.0",
                segment1_file,
                "transition_seg1",
                start_time=segment1_start,
                duration=segment1_duration,
            )

            if not success1:
                logger.error(f"Failed to create transition segment 1: {error1}")
                return None

            # Create segment 2: DJ transition with fade in/out
            segment2_file = self._generate_temp_filename("trans_seg2")

            success2, error2 = await self._run_simple_filter_command(
                dj_audio_path,
                f"afade=t=in:st=0:d=0.5,afade=t=out:st={(dj_duration - 0.5)}:d=0.5",
                segment2_file,
                "transition_seg2",
            )

            if not success2:
                logger.error(f"Failed to create transition segment 2: {error2}")
                return None

            # Create segment 3: Beginning of song2
            segment3_file = self._generate_temp_filename("trans_seg3")
            segment3_duration = min(20.0, song2_duration * 0.3)

            success3, error3 = await self._run_simple_filter_command(
                song2_path,
                "afade=t=in:st=0:d=0.5",
                segment3_file,
                "transition_seg3",
                duration=segment3_duration,
            )

            if not success3:
                logger.error(f"Failed to create transition segment 3: {error3}")
                return None

            return [segment1_file, segment2_file, segment3_file]

        except Exception as e:
            logger.error(f"Error creating transition segments: {e}")
            return None

    async def normalize_audio_file(
        self, input_file: str, output_file: str, target_lufs: float = -16.0
    ) -> bool:
        """
        Normalize audio file to target loudness.

        Args:
            input_file: Input file path
            output_file: Output file path
            target_lufs: Target loudness in LUFS

        Returns:
            True if successful
        """
        try:
            return await self.mixing_builder.normalize_audio(
                input_file, output_file, target_lufs
            )
        except Exception as e:
            logger.error(f"Error normalizing audio: {e}")
            return False

    async def create_audio_remainder(
        self,
        audio_path: str,
        skip_seconds: float,
        _metadata: dict,
        audio_type: str = "song",
    ) -> MixedAudioResult:
        """
        Create remainder file starting from skip_seconds.

        Args:
            audio_path: Path to audio file
            skip_seconds: Start time in seconds
            _metadata: Audio metadata (unused, kept for API compatibility)
            audio_type: "song" or "dj" for appropriate processing

        Returns:
            MixedAudioResult with output file path
        """
        try:
            prefix = "remainder" if audio_type == "song" else "dj_remainder"
            output_file = self._generate_temp_filename(prefix)

            # Different LUFS targets for song vs DJ
            target_lufs = (
                AudioConstants.FINAL_MIX_LUFS
                if audio_type == "song"
                else AudioConstants.MUSIC_LUFS
            )

            # For song remainders after intro, fade from ducked volume (20%) to full volume (100%)
            # This creates a smooth transition from the ducked intro overlap
            # Using volume expression: starts at MUSIC_DUCK_VOLUME and ramps up to 1.0 over FADE_IN_DURATION
            if audio_type == "song":
                fade_duration = AudioConstants.FADE_IN_DURATION
                start_vol = AudioConstants.MUSIC_DUCK_VOLUME
                # Volume ramp: if t < fade_duration, interpolate from start_vol to 1.0, else 1.0
                volume_expr = f"if(lt(t,{fade_duration}),{start_vol}+(1-{start_vol})*t/{fade_duration},1.0)"
                filter_str = (
                    f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11,"
                    f"volume='{volume_expr}':eval=frame"
                )
            else:
                filter_str = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"

            timeout = 60 if audio_type == "song" else 30
            success, error = await self._run_simple_filter_command(
                audio_path,
                filter_str,
                output_file,
                f"{audio_type}_remainder",
                start_time=skip_seconds,
                timeout=timeout,
            )

            if not success:
                return MixedAudioResult(success=False, error_message=error)

            duration = await get_audio_duration(output_file)

            logger.info(
                f"✅ Created {audio_type} remainder from {skip_seconds:.1f}s ({duration:.1f}s)"
            )

            return MixedAudioResult(
                success=True, output_file=output_file, duration=duration
            )

        except Exception as e:
            logger.error(f"Error creating {audio_type} remainder: {e}")
            return MixedAudioResult(success=False, error_message=str(e))

    async def create_auto_crossfade(
        self, song_path: str, metadata: dict
    ) -> MixedAudioResult:
        """
        Create auto-crossfaded file with fade-in for seamless transitions.

        Args:
            song_path: Path to song file
            metadata: Song metadata

        Returns:
            MixedAudioResult with output file path
        """
        try:
            output_file = self._generate_temp_filename("auto_crossfade")

            # Get song duration
            song_duration = await get_audio_duration(song_path)
            if song_duration <= 0:
                return MixedAudioResult(
                    success=False, error_message="Invalid song duration"
                )

            # Variable crossfade timing
            crossfade_duration = random.uniform(2.0, 6.0)
            fade_in_duration = min(crossfade_duration, song_duration / 4)

            # Build filter for fade-in and normalization
            filter_str = (
                f"loudnorm=I={AudioConstants.MUSIC_LUFS}:TP=-1.5:LRA=11,"
                f"afade=t=in:st=0:d={fade_in_duration}:curve=exp"
            )

            success, error = await self._run_simple_filter_command(
                song_path, filter_str, output_file, "auto_crossfade", timeout=60
            )

            if not success:
                return MixedAudioResult(success=False, error_message=error)

            duration = await get_audio_duration(output_file)

            logger.info(
                f"✅ Created auto-crossfade: {metadata.get('title', 'Unknown')} (fade-in: {fade_in_duration:.1f}s)"
            )

            return MixedAudioResult(
                success=True, output_file=output_file, duration=duration
            )

        except Exception as e:
            logger.error(f"Error creating auto-crossfade: {e}")
            return MixedAudioResult(success=False, error_message=str(e))

    async def create_outro_segments(
        self, song_path: str, dj_audio_path: str, _metadata: dict
    ) -> list[dict] | None:
        """
        Create 3-part outro structure: song solo + overlay + DJ solo.

        Args:
            song_path: Path to song file
            dj_audio_path: Path to DJ audio
            _metadata: Song metadata (unused, kept for API compatibility)

        Returns:
            List of segment dicts with 'file', 'type', 'duration' keys, or None if failed
        """
        try:
            # Get durations
            song_duration = await get_audio_duration(song_path)
            dj_duration = await get_audio_duration(dj_audio_path)

            if song_duration <= 0 or dj_duration <= 0:
                logger.error(
                    f"Invalid durations: song={song_duration}, dj={dj_duration}"
                )
                return None

            # Calculate outro timing
            overlay_duration = min(dj_duration * 0.6, 15.0)  # Max 15s overlap
            dj_solo_duration = dj_duration - overlay_duration
            song_solo_duration = song_duration - overlay_duration

            if song_solo_duration < 10:  # Need at least 10s of song solo
                logger.warning("Song too short for outro mixing")
                return None

            segments = []

            logger.info(
                f"🎛️ Creating outro segments: song_solo={song_solo_duration:.1f}s, "
                f"overlay={overlay_duration:.1f}s, dj_solo={dj_solo_duration:.1f}s"
            )

            # SEGMENT 1: Song solo (beginning until outro starts)
            song_solo_file = self._generate_temp_filename("outro_part1_song_solo")

            success1, error1 = await self._run_simple_copy_command(
                song_path,
                song_solo_file,
                "song_solo_segment",
                duration=song_solo_duration,
                timeout=30,
            )

            if success1 and Path(song_solo_file).exists():
                segments.append(
                    {
                        "file": song_solo_file,
                        "type": "song_solo",
                        "duration": song_solo_duration,
                    }
                )
                logger.info(f"✅ Created song solo segment: {song_solo_duration:.1f}s")
            else:
                logger.error(f"❌ Failed to create song solo segment: {error1}")
                return None

            # SEGMENT 2: Song outro + DJ overlay
            overlay_file = self._generate_temp_filename("outro_part2_overlay")
            fc = self.filter_builder
            filters = [
                fc.normalize_and_trim_start(
                    "0:a", "song_outro", AudioConstants.MUSIC_LUFS, song_solo_duration
                ),
                fc.normalize_and_trim_end(
                    "1:a", "dj_overlay", AudioConstants.DJ_LUFS, overlay_duration
                ),
                fc.volume(
                    "song_outro", "song_ducked", AudioConstants.MUSIC_DUCK_VOLUME_OUTRO
                ),
                fc.mix(["song_ducked", "dj_overlay"], "mixed", weights=[1.0, 1.0]),
                fc.normalize("mixed", "final", AudioConstants.MUSIC_LUFS),
            ]
            filter_str = fc.chain(filters)

            # Need custom command for -t option
            output_config = AudioOutputConfig()
            cmd2 = (
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    song_path,
                    "-i",
                    dj_audio_path,
                    "-filter_complex",
                    filter_str,
                    "-t",
                    str(overlay_duration),
                    "-map",
                    "[final]",
                ]
                + self.ffmpeg_builder.build_output_options(output_config)
                + [overlay_file]
            )

            success2, _, stderr2 = await self.ffmpeg_builder.run_ffmpeg_safe(
                cmd2, "overlay_segment", timeout=30
            )
            if success2 and Path(overlay_file).exists():
                segments.append(
                    {
                        "file": overlay_file,
                        "type": "song_dj_overlay",
                        "duration": overlay_duration,
                    }
                )
                logger.info(f"✅ Created overlay segment: {overlay_duration:.1f}s")
            else:
                logger.error(f"❌ Failed to create overlay segment: {stderr2[:200]}")
                return None

            # SEGMENT 3: DJ solo (remaining DJ talk after overlay)
            if dj_solo_duration > 1.0:
                dj_solo_file = self._generate_temp_filename("outro_part3_dj_solo")

                success3, error3 = await self._run_simple_copy_command(
                    dj_audio_path,
                    dj_solo_file,
                    "dj_solo_segment",
                    start_time=overlay_duration,
                    duration=dj_solo_duration,
                    timeout=30,
                )

                if success3 and Path(dj_solo_file).exists():
                    segments.append(
                        {
                            "file": dj_solo_file,
                            "type": "dj_solo",
                            "duration": dj_solo_duration,
                        }
                    )
                    logger.info(f"✅ Created DJ solo segment: {dj_solo_duration:.1f}s")

            logger.info(f"✅ Created {len(segments)} outro segments")
            return segments

        except Exception as e:
            logger.error(f"Error creating outro segments: {e}")
            return None

    async def create_mixed_transition(
        self,
        prev_song_path: str,
        current_song_path: str,
        dj_audio_path: str,
        _metadata: dict,
    ) -> MixedAudioResult:
        """
        Create seamless single-file transition between songs with DJ talk.

        Args:
            prev_song_path: Path to previous song
            current_song_path: Path to current song
            dj_audio_path: Path to DJ transition audio
            _metadata: Song metadata (unused, kept for API compatibility)

        Returns:
            MixedAudioResult with seamless transition file
        """
        try:
            output_file = self._generate_temp_filename("mixed_transition")

            # Get durations
            prev_duration = await get_audio_duration(prev_song_path)
            current_duration = await get_audio_duration(current_song_path)
            dj_duration = await get_audio_duration(dj_audio_path)

            if prev_duration <= 0 or current_duration <= 0 or dj_duration <= 0:
                return MixedAudioResult(
                    success=False,
                    error_message=f"Invalid durations: prev={prev_duration}, current={current_duration}, dj={dj_duration}",
                )

            # Calculate transition timing
            transition_buffer = random.uniform(4.0, 10.0)
            outro_duration = min(dj_duration / 2 + transition_buffer, 15.0)
            intro_duration = min(dj_duration / 2 + transition_buffer, 15.0)
            total_duration = outro_duration + intro_duration - transition_buffer
            outro_start = max(0, prev_duration - outro_duration)

            logger.info(
                f"🎛️ Creating seamless transition: outro={outro_duration:.1f}s, "
                f"intro={intro_duration:.1f}s, total={total_duration:.1f}s"
            )

            # Build filter using primitives
            fc = self.filter_builder
            filters = [
                fc.normalize_and_trim_start(
                    "0:a", "prev_outro", AudioConstants.MUSIC_LUFS, outro_start
                ),
                fc.normalize_and_trim_end(
                    "1:a", "current_intro", AudioConstants.MUSIC_LUFS, intro_duration
                ),
                fc.normalize("2:a", "dj_norm", AudioConstants.DJ_LUFS),
                # Duck previous song at transition point
                fc.pipe(
                    "prev_outro",
                    [
                        f"volume={AudioConstants.MUSIC_DUCK_VOLUME_OUTRO}:enable='gte(t,{outro_duration - transition_buffer})'"
                    ],
                    "prev_ducked",
                ),
                # Delay and duck current song intro
                fc.pipe(
                    "current_intro",
                    [
                        f"adelay={int(outro_duration * 1000)}|{int(outro_duration * 1000)}",
                        f"volume={AudioConstants.MUSIC_DUCK_VOLUME_OUTRO}:enable='lte(t,{transition_buffer})'",
                    ],
                    "current_delayed",
                ),
                # Mix all three streams with weights
                "[prev_ducked][current_delayed][dj_norm]amix=inputs=3:duration=longest:weights=0.8 0.8 1.0[mixed]",
                fc.normalize("mixed", "final", AudioConstants.MUSIC_LUFS),
            ]
            filter_str = fc.chain(filters)

            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                prev_song_path,
                "-i",
                current_song_path,
                "-i",
                dj_audio_path,
                "-filter_complex",
                filter_str,
                "-t",
                str(total_duration),
                "-map",
                "[final]",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-ar",
                "44100",
                "-ac",
                "2",
                output_file,
            ]

            success, stdout, stderr = await self.ffmpeg_builder.run_ffmpeg_safe(
                cmd, "mixed_transition", timeout=90
            )

            if not success or not Path(output_file).exists():
                return MixedAudioResult(
                    success=False,
                    error_message=f"Transition creation failed: {stderr[:200] if stderr else 'Unknown error'}",
                )

            duration = await get_audio_duration(output_file)

            logger.info(
                f"✅ Created seamless transition: {output_file} ({duration:.1f}s)"
            )

            return MixedAudioResult(
                success=True, output_file=output_file, duration=duration
            )

        except Exception as e:
            logger.error(f"Error creating mixed transition: {e}")
            return MixedAudioResult(success=False, error_message=str(e))

    async def create_full_transition_segments(
        self,
        prev_song_path: str,
        current_song_path: str,
        dj_audio_path: str,
    ) -> list[dict] | None:
        """
        Create 4-part segmented transition: prev_outro + dj_bridge + current_intro + current_remainder.

        Args:
            prev_song_path: Path to previous song
            current_song_path: Path to current song
            dj_audio_path: Path to DJ transition audio

        Returns:
            List of segment dicts with 'file', 'type', 'duration' keys, or None if failed
        """
        try:
            # Get durations
            prev_duration = await get_audio_duration(prev_song_path)
            current_duration = await get_audio_duration(current_song_path)
            dj_duration = await get_audio_duration(dj_audio_path)

            if prev_duration <= 0 or current_duration <= 0 or dj_duration <= 0:
                logger.error(
                    f"Invalid durations: prev={prev_duration}, current={current_duration}, dj={dj_duration}"
                )
                return None

            # Calculate transition timing
            overlap_per_side = min(dj_duration * 0.4, 12.0)  # Max 12s overlap each side
            dj_bridge_duration = max(0, dj_duration - (overlap_per_side * 2))

            segments = []

            logger.info(
                f"🎛️ Creating transition segments: overlap={overlap_per_side:.1f}s each side, "
                f"dj_bridge={dj_bridge_duration:.1f}s"
            )

            # SEGMENT 1: Previous song outro + DJ talk start overlay
            prev_outro_start = prev_duration - overlap_per_side
            outro_overlay_file = self._generate_temp_filename("transition_part1_outro")

            fc = self.filter_builder
            filters_1 = [
                fc.normalize_and_trim_start(
                    "0:a", "prev_outro", AudioConstants.MUSIC_LUFS, prev_outro_start
                ),
                fc.normalize_and_trim_end(
                    "1:a", "dj_start", AudioConstants.DJ_LUFS, overlap_per_side
                ),
                fc.volume(
                    "prev_outro", "prev_ducked", AudioConstants.MUSIC_DUCK_VOLUME_OUTRO
                ),
                fc.mix(["prev_ducked", "dj_start"], "mixed", weights=[1.0, 1.0]),
                fc.normalize("mixed", "final", AudioConstants.MUSIC_LUFS),
            ]
            filter_str_1 = fc.chain(filters_1)

            # Need custom command for -t option
            output_config = AudioOutputConfig()
            cmd1 = (
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    prev_song_path,
                    "-i",
                    dj_audio_path,
                    "-filter_complex",
                    filter_str_1,
                    "-t",
                    str(overlap_per_side),
                    "-map",
                    "[final]",
                ]
                + self.ffmpeg_builder.build_output_options(output_config)
                + [outro_overlay_file]
            )

            success1, _, stderr1 = await self.ffmpeg_builder.run_ffmpeg_safe(
                cmd1, "transition_outro_overlay", timeout=30
            )
            if success1 and Path(outro_overlay_file).exists():
                segments.append(
                    {
                        "file": outro_overlay_file,
                        "type": "prev_outro_dj_overlay",
                        "duration": overlap_per_side,
                    }
                )
                logger.info(
                    f"✅ Created outro overlay segment: {overlap_per_side:.1f}s"
                )

            # SEGMENT 2: DJ bridge (middle of DJ talk, if exists)
            if dj_bridge_duration > 1.0:
                dj_bridge_file = self._generate_temp_filename("transition_part2_bridge")

                success2, error2 = await self._run_simple_copy_command(
                    dj_audio_path,
                    dj_bridge_file,
                    "transition_dj_bridge",
                    start_time=overlap_per_side,
                    duration=dj_bridge_duration,
                    timeout=30,
                )

                if success2 and Path(dj_bridge_file).exists():
                    segments.append(
                        {
                            "file": dj_bridge_file,
                            "type": "dj_bridge",
                            "duration": dj_bridge_duration,
                        }
                    )
                    logger.info(
                        f"✅ Created DJ bridge segment: {dj_bridge_duration:.1f}s"
                    )

            # SEGMENT 3: Current song intro + DJ talk end overlay
            intro_overlay_file = self._generate_temp_filename("transition_part3_intro")
            dj_end_start = overlap_per_side + dj_bridge_duration

            filters_3 = [
                fc.normalize_and_trim_end(
                    "0:a", "current_intro", AudioConstants.MUSIC_LUFS, overlap_per_side
                ),
                fc.normalize_and_trim_start(
                    "1:a", "dj_end", AudioConstants.DJ_LUFS, dj_end_start
                ),
                fc.volume(
                    "current_intro",
                    "current_ducked",
                    AudioConstants.MUSIC_DUCK_VOLUME_OUTRO,
                ),
                fc.mix(["current_ducked", "dj_end"], "mixed", weights=[1.0, 1.0]),
                fc.normalize("mixed", "final", AudioConstants.MUSIC_LUFS),
            ]
            filter_str_3 = fc.chain(filters_3)

            # Need custom command for -t option
            cmd3 = (
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    current_song_path,
                    "-i",
                    dj_audio_path,
                    "-filter_complex",
                    filter_str_3,
                    "-t",
                    str(overlap_per_side),
                    "-map",
                    "[final]",
                ]
                + self.ffmpeg_builder.build_output_options(output_config)
                + [intro_overlay_file]
            )

            success3, _, stderr3 = await self.ffmpeg_builder.run_ffmpeg_safe(
                cmd3, "transition_intro_overlay", timeout=30
            )
            if success3 and Path(intro_overlay_file).exists():
                segments.append(
                    {
                        "file": intro_overlay_file,
                        "type": "current_intro_dj_overlay",
                        "duration": overlap_per_side,
                    }
                )
                logger.info(
                    f"✅ Created intro overlay segment: {overlap_per_side:.1f}s"
                )

            # SEGMENT 4: Current song remainder (after intro overlay)
            song_remainder_duration = current_duration - overlap_per_side
            if song_remainder_duration > 10.0:  # Only if significant remainder exists
                remainder_file = self._generate_temp_filename(
                    "transition_part4_remainder"
                )

                success4, error4 = await self._run_simple_copy_command(
                    current_song_path,
                    remainder_file,
                    "transition_remainder",
                    start_time=overlap_per_side,
                    duration=song_remainder_duration,
                    timeout=30,
                )

                if success4 and Path(remainder_file).exists():
                    segments.append(
                        {
                            "file": remainder_file,
                            "type": "current_song_remainder",
                            "duration": song_remainder_duration,
                        }
                    )
                    logger.info(
                        f"✅ Created remainder segment: {song_remainder_duration:.1f}s"
                    )

            logger.info(f"✅ Created {len(segments)} transition segments")
            return segments

        except Exception as e:
            logger.error(f"Error creating transition segments: {e}")
            return None
