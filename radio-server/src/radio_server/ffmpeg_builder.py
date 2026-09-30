"""
FFmpeg command builder and executor for audio processing.
Centralizes all FFmpeg command construction and execution with proper error handling.
"""

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .exceptions import FFmpegError, FFmpegTimeoutError

logger = logging.getLogger(__name__)


@dataclass
class AudioNormalizationConfig:
    """Configuration for audio normalization."""

    target_lufs: float = -16.0  # Target loudness (EBU R128 standard for music)
    measured_i: float | None = None  # Integrated loudness measurement
    measured_lra: float | None = None  # Loudness range
    measured_tp: float | None = None  # True peak


@dataclass
class AudioOutputConfig:
    """Configuration for audio output encoding."""

    codec: str = "libmp3lame"
    bitrate: str = "192k"
    sample_rate: int = 44100
    channels: int = 2


class FFmpegCommandBuilder:
    """
    Builder for FFmpeg commands with consistent error handling and timeouts.
    Centralizes all FFmpeg command construction to eliminate duplication.
    """

    @staticmethod
    def build_normalize_filter(
        lufs_target: float = -16.0,
        measured_i: float | None = None,
        measured_tp: float | None = None,
        measured_lra: float | None = None,
    ) -> str:
        """
        Build loudnorm filter for audio normalization.

        Args:
            lufs_target: Target loudness in LUFS
            measured_i: Measured integrated loudness (for two-pass)
            measured_tp: Measured true peak (for two-pass)
            measured_lra: Measured loudness range (for two-pass)

        Returns:
            Filter string for FFmpeg
        """
        if measured_i is not None:
            # Two-pass normalization (more accurate)
            return (
                f"loudnorm=I={lufs_target}:TP=-1.5:LRA=11:linear=true:"
                f"measured_I={measured_i}:measured_TP={measured_tp}:measured_LRA={measured_lra}"
            )
        else:
            # Single-pass normalization
            return f"loudnorm=I={lufs_target}:TP=-1.5:LRA=11"

    @staticmethod
    def build_output_options(config: AudioOutputConfig) -> list[str]:
        """
        Build standard output encoding options.

        Args:
            config: Output configuration

        Returns:
            List of FFmpeg arguments
        """
        return [
            "-c:a",
            config.codec,
            "-b:a",
            config.bitrate,
            "-ar",
            str(config.sample_rate),
            "-ac",
            str(config.channels),
        ]

    @staticmethod
    async def run_ffmpeg_async(
        cmd: list[str], operation_name: str, timeout: int = 300
    ) -> tuple[int, str, str]:
        """
        Run FFmpeg command asynchronously with timeout and error handling.

        Args:
            cmd: FFmpeg command as list of arguments
            operation_name: Name of operation for logging
            timeout: Timeout in seconds (default 300 = 5 minutes)

        Returns:
            Tuple of (returncode, stdout, stderr)

        Raises:
            FFmpegTimeoutError: If command times out
            FFmpegError: If FFmpeg fails
        """
        logger.debug(f"Running FFmpeg for {operation_name}: {' '.join(cmd[:10])}...")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                raise FFmpegTimeoutError(
                    f"FFmpeg {operation_name} timed out after {timeout}s"
                ) from None

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            if process.returncode != 0:
                logger.error(
                    f"FFmpeg {operation_name} failed with return code {process.returncode}"
                )
                logger.error(f"FFmpeg stderr: {stderr_str[-500:]}")  # Last 500 chars
                raise FFmpegError(
                    f"FFmpeg {operation_name} failed: {stderr_str[-200:]}"
                )

            logger.debug(f"FFmpeg {operation_name} completed successfully")
            return process.returncode, stdout_str, stderr_str

        except FileNotFoundError:
            raise FFmpegError("FFmpeg not found - is it installed?") from None
        except (FFmpegTimeoutError, FFmpegError):
            raise
        except Exception as e:
            raise FFmpegError(
                f"FFmpeg {operation_name} error: {type(e).__name__}: {e}"
            ) from e

    @staticmethod
    async def run_ffmpeg_safe(
        cmd: list[str], operation_name: str, timeout: int = 300
    ) -> tuple[bool, str, str]:
        """
        Run FFmpeg with error handling, returning success boolean.

        Args:
            cmd: FFmpeg command as list of arguments
            operation_name: Name of operation for logging
            timeout: Timeout in seconds

        Returns:
            Tuple of (success, stdout, stderr)
        """
        try:
            returncode, stdout, stderr = await FFmpegCommandBuilder.run_ffmpeg_async(
                cmd, operation_name, timeout
            )
            return True, stdout, stderr
        except (FFmpegError, FFmpegTimeoutError) as e:
            logger.error(f"FFmpeg {operation_name} failed: {e}")
            return False, "", str(e)


class AudioMixingBuilder:
    """
    High-level audio mixing operations using FFmpeg.
    Provides common mixing patterns used in radio production.
    """

    def __init__(self, builder: FFmpegCommandBuilder = None):
        self.builder = builder or FFmpegCommandBuilder()

    def _build_crossfade_filter(
        self,
        input_count: int,
        crossfade_duration: float,
        precondition_inputs: bool = True,
    ) -> str:
        filter_parts: list[str] = []
        input_labels: list[str] = []

        if precondition_inputs:
            for i in range(input_count):
                prepared_label = f"a{i}"
                filter_parts.append(
                    f"[{i}:a]aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"aresample=44100,asetpts=N/SR/TB[{prepared_label}]"
                )
                input_labels.append(f"[{prepared_label}]")
        else:
            input_labels = [f"[{i}:a]" for i in range(input_count)]

        current_input = input_labels[0]
        for i in range(1, input_count):
            output_label = "[out]" if i == input_count - 1 else f"[cf{i}]"
            filter_parts.append(
                f"{current_input}{input_labels[i]}acrossfade=d={crossfade_duration}:c1=tri:c2=tri{output_label}"
            )
            current_input = output_label

        return ";".join(filter_parts)

    def _build_crossfade_command(
        self,
        input_files: list[str],
        output_file: str,
        crossfade_duration: float,
    ) -> list[str]:
        ffmpeg_inputs: list[str] = []
        for song in input_files:
            ffmpeg_inputs.extend(
                [
                    "-thread_queue_size",
                    "1024",
                    "-fflags",
                    "+discardcorrupt+genpts",
                    "-err_detect",
                    "ignore_err",
                    "-i",
                    song,
                ]
            )

        output_config = AudioOutputConfig()
        filter_complex = self._build_crossfade_filter(
            len(input_files), crossfade_duration, precondition_inputs=True
        )

        return (
            ["ffmpeg", "-loglevel", "error", "-y"]
            + ffmpeg_inputs
            + [
                "-filter_complex",
                filter_complex,
                "-map",
                "[out]",
                "-vn",
                "-sn",
                "-dn",
            ]
            + self.builder.build_output_options(output_config)
            + [
                "-max_muxing_queue_size",
                "2048",
                "-f",
                "mp3",
                output_file,
            ]
        )

    async def _sanitize_crossfade_inputs(
        self, song_files: list[str], output_parent: Path
    ) -> list[str] | None:
        sanitized_files: list[str] = []
        suffix = int(time.time() * 1000)

        try:
            for index, song_file in enumerate(song_files):
                sanitized_path = output_parent / f".crossfade_src_{suffix}_{index}.wav"
                cmd = [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-y",
                    "-fflags",
                    "+discardcorrupt+genpts",
                    "-err_detect",
                    "ignore_err",
                    "-i",
                    song_file,
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-af",
                    "aformat=sample_fmts=s16:channel_layouts=stereo,aresample=44100:async=0:first_pts=0",
                    "-c:a",
                    "pcm_s16le",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    str(sanitized_path),
                ]

                success, _, _ = await self.builder.run_ffmpeg_safe(
                    cmd, "crossfade_sanitize_input", timeout=120
                )
                if not success or not sanitized_path.exists():
                    logger.error("Failed to sanitize crossfade input: %s", song_file)
                    return None

                if sanitized_path.stat().st_size < 4096:
                    logger.error(
                        "Sanitized crossfade input is too small: %s", sanitized_path
                    )
                    return None

                sanitized_files.append(str(sanitized_path))

            return sanitized_files
        except Exception as exc:
            logger.error(f"Error sanitizing crossfade inputs: {exc}")
            return None

    async def normalize_audio(
        self, input_file: str, output_file: str, target_lufs: float = -16.0
    ) -> bool:
        """
        Normalize audio to target loudness.

        Args:
            input_file: Input audio file path
            output_file: Output audio file path
            target_lufs: Target loudness in LUFS

        Returns:
            True if successful
        """
        # Two-pass normalization for better results
        # First pass: measure
        measure_cmd = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            input_file,
            "-af",
            self.builder.build_normalize_filter(target_lufs),
            "-f",
            "null",
            "-",
        ]

        success, stdout, stderr = await self.builder.run_ffmpeg_safe(
            measure_cmd, "normalize_measure", timeout=60
        )

        if not success:
            return False

        # Parse measurements from stderr
        measured_i = None
        measured_tp = None
        measured_lra = None

        for line in stderr.split("\n"):
            if "Input Integrated:" in line:
                with suppress(ValueError, IndexError):
                    measured_i = float(line.split(":")[-1].strip().split()[0])
            elif "Input True Peak:" in line:
                with suppress(ValueError, IndexError):
                    measured_tp = float(line.split(":")[-1].strip().split()[0])
            elif "Input LRA:" in line:
                with suppress(ValueError, IndexError):
                    measured_lra = float(line.split(":")[-1].strip().split()[0])

        # Second pass: apply normalization
        output_config = AudioOutputConfig()
        normalize_filter = self.builder.build_normalize_filter(
            target_lufs, measured_i, measured_tp, measured_lra
        )

        normalize_cmd = (
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                input_file,
                "-vn",
                "-af",
                normalize_filter,
            ]
            + self.builder.build_output_options(output_config)
            + [output_file]
        )

        success, stdout, stderr = await self.builder.run_ffmpeg_safe(
            normalize_cmd, "normalize_apply", timeout=60
        )

        return success and Path(output_file).exists()

    async def mix_voice_over_music(
        self,
        music_file: str,
        voice_file: str,
        output_file: str,
        duck_volume: float = 0.2,
        music_lufs: float = -16.0,
        voice_lufs: float = -12.0,
    ) -> bool:
        """
        Mix voice over music with ducking.

        Args:
            music_file: Background music file
            voice_file: Voice/DJ talk file
            output_file: Output mixed file
            duck_volume: Music volume during voice (0.0-1.0)
            music_lufs: Target loudness for music
            voice_lufs: Target loudness for voice

        Returns:
            True if successful
        """
        # Build complex filter for voice-over with ducking
        filter_complex = (
            f"[0:a]loudnorm=I={music_lufs}:TP=-1.5:LRA=11[music];"
            f"[1:a]loudnorm=I={voice_lufs}:TP=-1.5:LRA=11[voice];"
            f"[music][voice]sidechaincompress=threshold=0.1:ratio=4:attack=100:release=500:level_sc={duck_volume}[compressed];"
            f"[compressed][voice]amix=inputs=2:duration=longest:weights=1 {1 - duck_volume}[out]"
        )

        output_config = AudioOutputConfig()
        cmd = (
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                music_file,
                "-i",
                voice_file,
                "-filter_complex",
                filter_complex,
                "-map",
                "[out]",
            ]
            + self.builder.build_output_options(output_config)
            + [output_file]
        )

        success, stdout, stderr = await self.builder.run_ffmpeg_safe(
            cmd, "voice_over_music", timeout=120
        )

        return success and Path(output_file).exists()

    async def crossfade_songs(
        self, song_files: list[str], output_file: str, crossfade_duration: float = 5.0
    ) -> bool:
        """
        Crossfade multiple songs together.

        Args:
            song_files: List of song file paths
            output_file: Output crossfaded file
            crossfade_duration: Crossfade duration in seconds

        Returns:
            True if successful
        """
        if len(song_files) < 2:
            logger.error("Need at least 2 songs for crossfade")
            return False

        # Verify input files exist
        for song_file in song_files:
            if not Path(song_file).exists():
                logger.error(f"Input file not found for crossfade: {song_file}")
                return False

        cmd = self._build_crossfade_command(song_files, output_file, crossfade_duration)
        success, _, _ = await self.builder.run_ffmpeg_safe(
            cmd, "crossfade_songs", timeout=180
        )

        if not success:
            logger.warning(
                "Primary crossfade failed, retrying with sanitized PCM inputs"
            )
            sanitized_files: list[str] | None = None
            output_parent = Path(output_file).parent
            try:
                sanitized_files = await self._sanitize_crossfade_inputs(
                    song_files, output_parent
                )
                if not sanitized_files:
                    return False

                retry_cmd = self._build_crossfade_command(
                    sanitized_files, output_file, crossfade_duration
                )
                success, _, _ = await self.builder.run_ffmpeg_safe(
                    retry_cmd, "crossfade_songs_sanitized_retry", timeout=240
                )
            finally:
                if sanitized_files:
                    for sanitized_file in sanitized_files:
                        with suppress(OSError):
                            Path(sanitized_file).unlink()

        # Verify output exists and has reasonable size
        if success and Path(output_file).exists():
            file_size = Path(output_file).stat().st_size
            if file_size < 10000:  # Less than 10KB is suspicious
                logger.error(
                    f"Crossfade output file is suspiciously small: {file_size} bytes"
                )
                return False
            return True

        return False

    async def trim_audio(
        self,
        input_file: str,
        output_file: str,
        start: float = 0,
        duration: float = None,
    ) -> bool:
        """
        Extract a segment of audio.

        Args:
            input_file: Input audio file
            output_file: Output audio file
            start: Start time in seconds
            duration: Duration in seconds (None = to end)

        Returns:
            True if successful
        """
        output_config = AudioOutputConfig()
        cmd = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(start),
            "-i",
            input_file,
        ]

        if duration:
            cmd.extend(["-t", str(duration)])

        # Suppress any video streams (e.g. embedded album art in MP3s)
        cmd.append("-vn")

        cmd.extend(self.builder.build_output_options(output_config) + [output_file])

        success, stdout, stderr = await self.builder.run_ffmpeg_safe(
            cmd, "trim_audio", timeout=60
        )

        return success and Path(output_file).exists()

    async def concatenate_audio(self, input_files: list[str], output_file: str) -> bool:
        """
        Concatenate multiple audio files.

        Args:
            input_files: List of input audio files
            output_file: Output concatenated file

        Returns:
            True if successful
        """
        if len(input_files) < 2:
            logger.error("Need at least 2 files to concatenate")
            return False

        # Normalize inputs to a common format before concatenation
        filter_parts = []
        for i in range(len(input_files)):
            filter_parts.append(
                f"[{i}:a]aresample=44100,"
                "aformat=sample_fmts=s16:channel_layouts=stereo,"
                "asetpts=N/SR/TB"
                f"[a{i}]"
            )

        inputs = "".join([f"[a{i}]" for i in range(len(input_files))])
        filter_parts.append(f"{inputs}concat=n={len(input_files)}:v=0:a=1[out]")
        filter_complex = ";".join(filter_parts)

        ffmpeg_inputs = []
        for file in input_files:
            ffmpeg_inputs.extend(["-i", file])

        output_config = AudioOutputConfig()
        cmd = (
            ["ffmpeg", "-loglevel", "error", "-y"]
            + ffmpeg_inputs
            + ["-filter_complex", filter_complex, "-map", "[out]"]
            + self.builder.build_output_options(output_config)
            + [output_file]
        )

        success, stdout, stderr = await self.builder.run_ffmpeg_safe(
            cmd, "concatenate_audio", timeout=120
        )

        return success and Path(output_file).exists()
