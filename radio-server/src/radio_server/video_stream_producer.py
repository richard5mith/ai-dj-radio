import asyncio
import hashlib
import json
import logging
import math
import os
import signal
import stat
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp

from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3

from .audio_utils import artwork_filename_for, open_fifo_for_writing
from .song_facts import SongFactRepository

try:
    from PIL import Image, ImageFont  # type: ignore[import]
except ImportError:  # pragma: no cover - optional dependency at runtime
    ImageFont = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _parse_encode_threads(env_value: str | None) -> int | None:
    """Parse VIDEO_ENCODE_THREADS.

    Args:
        env_value: Raw env value; "0" or unset means "let x264 choose".

    Returns:
        Thread count to pass to ffmpeg, or None for x264's auto setting.
    """
    try:
        requested = int(env_value) if env_value else 0
    except ValueError:
        requested = 0
    return requested or None


class VideoStreamProducer:
    """Generate an HLS video feed with dynamic overlays."""

    def __init__(
        self,
        audio_source: str = "tcp://127.0.0.1:8765",
        overlay_dir: Path | None = None,
        output_dir: Path | None = None,
        station_name: str = "Radio Station",
    ) -> None:
        self.station_name = station_name
        self.audio_source = audio_source
        self.audio_source_type = (
            "pcm" if str(audio_source).startswith("tcp://") else "http"
        )
        self.audio_sample_rate = 44100
        self.audio_channels = 2
        self.overlay_dir = overlay_dir or Path("/app/video-overlay")
        self.output_dir = output_dir or Path("/app/video-output")
        self.ffmpeg_process: asyncio.subprocess.Process | None = None
        self.monitor_task: asyncio.Task | None = None
        self._running = False
        self._lock = asyncio.Lock()
        self.font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        self.overlay_margin_side = 80
        self.overlay_margin_top = 160
        self.overlay_bottom_margin = 80
        self.overlay_main_spacing = 160
        self.overlay_sub_spacing = 90
        self.video_fps = max(1, int(os.getenv("VIDEO_FPS", "30")))
        _valid_presets = {
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
        }
        env_preset = os.getenv("VIDEO_ENCODE_PRESET", "ultrafast")
        self.video_encode_preset = (
            env_preset if env_preset in _valid_presets else "ultrafast"
        )
        env_threads = os.getenv("VIDEO_ENCODE_THREADS")
        # 0 (or unset) means "let x264 pick" — pinning to 1 saturates a single
        # core and stalls segment production whenever anything else needs CPU.
        self.video_encode_threads: int | None = _parse_encode_threads(env_threads)
        self.video_bitrate = self._normalize_rate(
            os.getenv("VIDEO_VIDEO_BITRATE", "2500k"), "2500k"
        )
        self.video_maxrate = self._normalize_rate(
            os.getenv("VIDEO_VIDEO_MAXRATE", "2750k"), "2750k"
        )
        self.video_bufsize = self._normalize_rate(
            os.getenv("VIDEO_VIDEO_BUFSIZE", "5000k"), "5000k"
        )

        self.hls_segment_seconds = max(
            1.0, float(os.getenv("VIDEO_HLS_SEGMENT_SECONDS", "2"))
        )
        self.hls_target_window_seconds = max(
            30.0, float(os.getenv("VIDEO_HLS_TARGET_WINDOW_SECONDS", "120"))
        )
        env_list_size = os.getenv("VIDEO_HLS_LIST_SIZE")
        if env_list_size:
            self.hls_list_size = max(6, int(env_list_size))
        else:
            self.hls_list_size = max(
                6,
                int(
                    math.ceil(self.hls_target_window_seconds / self.hls_segment_seconds)
                ),
            )
        env_delete_threshold = os.getenv("VIDEO_HLS_DELETE_THRESHOLD")
        if env_delete_threshold:
            self.hls_delete_threshold = max(3, int(env_delete_threshold))
        else:
            self.hls_delete_threshold = max(3, self.hls_list_size // 2)
        env_output_stall = os.getenv("VIDEO_OUTPUT_STALL_SECONDS")
        if env_output_stall:
            self.output_stall_seconds = max(15.0, float(env_output_stall))
        else:
            self.output_stall_seconds = max(90.0, self.hls_target_window_seconds * 0.75)
        self.hls_keyframe_interval = max(
            1, int(round(self.video_fps * self.hls_segment_seconds))
        )
        inline_env = os.getenv("VIDEO_INLINE_BACKGROUND")
        if inline_env is not None:
            self.use_inline_background = inline_env.lower() in ("1", "true", "yes")
        else:
            self.use_inline_background = self.hls_segment_seconds > 2.0

        # Load video theme configuration first
        self.video_theme_config = self._load_video_theme_config()
        self._apply_theme_config()

        self.current_state: dict[str, str] = {
            "dj": self.station_name,
            "main": "Generating Video Feed",
            "sub": "",
            "facts": "",
        }
        self.background_color: str = "#1a1a2e"  # overwritten by _apply_theme_config
        self.current_background_mode, self.current_background_params = (
            self._select_background(self.station_name)
        )
        self.current_track_signature: str | None = None
        self.base_main_font_size = 42
        self.base_sub_font_size = 32
        self.main_font_size = self.base_main_font_size
        self.sub_font_size = self.base_sub_font_size
        self.max_text_width = 1280 - (2 * self.overlay_margin_side) - 120
        self._font_metrics_supported = ImageFont is not None
        self._font_metrics_warning_logged = False
        self._font_cache: dict[int, Any] = {}
        self.fact_generator: (
            Callable[[str, str, str | None], Awaitable[list[str]]] | None
        ) = None
        config_dir = Path(os.getenv("RADIO_CONFIG_DIR", "/app/config"))
        # Store song facts cache in temp-audio so users know it's temporary/regeneratable
        cache_dir = Path("/app/temp-audio")
        self.fact_repository = SongFactRepository(config_dir, cache_dir)
        self.fact_cache: dict[str, list[str]] = self.fact_repository.snapshot_cache()
        self.fact_fetch_task: asyncio.Task | None = None
        self.fact_cycle_task: asyncio.Task | None = None
        self.background_pipe_path = self.overlay_dir / "background.pipe"
        self.background_process: asyncio.subprocess.Process | None = None
        self.background_monitor_task: asyncio.Task | None = None
        self._background_lock = asyncio.Lock()
        self.ffmpeg_log_task: asyncio.Task | None = None
        self.background_log_task: asyncio.Task | None = None
        self._background_keepalive_fd: int | None = None
        self.current_artwork_path: Path | None = None  # Track current artwork file
        # file_path of the track the latest metadata event names. Publishes
        # from superseded handlers (whose to_thread extracts outlive their
        # cancelled tasks) must not touch the live overlay for older tracks.
        self._live_artwork_file_path: str | None = None
        # Store artwork in temp-audio so it's visible and gets cleaned up
        self.artwork_dir = Path("/app/temp-audio/artwork")
        self.artwork_dir.mkdir(parents=True, exist_ok=True)
        # Fixed filename for current artwork that FFmpeg will continuously read
        self.current_artwork_file = self.artwork_dir / "current.jpg"
        self.artwork_pipe_path = self.overlay_dir / "artwork.pipe"
        self.artwork_feeder_task: asyncio.Task | None = None
        self._artwork_feeder_running = False
        # Bumped on every feeder start/stop; a feeder thread exits once its
        # captured generation no longer matches, so an orphaned to_thread
        # feeder (task.cancel can't stop it) can never write alongside a
        # newer feeder on the same pipe.
        self._artwork_feeder_generation = 0
        self._artwork_frame_interval = 1.0
        # Create the shared placeholder + live overlay synchronously during init
        try:
            if Image:
                img = Image.new("RGB", (200, 200), (0, 0, 0))
                self._publish_artwork_image(img, None)
        except Exception as e:
            logger.warning(f"Could not create initial placeholder artwork: {e}")

        # Create overlay directory and all required text files to prevent FFmpeg errors
        self.overlay_dir.mkdir(parents=True, exist_ok=True)
        # Create individual fact line files (workaround for FFmpeg 7.x newline rendering bug)
        for i in range(self._get_max_fact_lines()):
            self._write_overlay_text_file_sync(
                self.overlay_dir / f"facts_{i + 1}.txt", ""
            )
        self._write_overlay_text_file_sync(self.overlay_dir / "dj.txt", self.station_name)
        self._write_overlay_text_file_sync(
            self.overlay_dir / "main.txt", "Generating Video Feed"
        )
        self._write_overlay_text_file_sync(self.overlay_dir / "sub.txt", "")
        self._ffmpeg_noise_tokens = (
            "header missing",
            "invalid data found when processing input",
            "stream ends prematurely",
            "using bitrate for duration",
            "no accelerated colorspace conversion",
            "passing a number to -vsync is deprecated",
            "failed to compensate for timestamp delta",
            "last message repeated",
            "overread",
        )
        self.output_monitor_task: asyncio.Task | None = None
        self._last_output_timestamp: float = 0.0
        self._last_output_activity: float = 0.0
        self._output_stall_threshold = 20.0
        self._output_check_interval = 5.0
        self._ffmpeg_recent_lines = deque(maxlen=80)

    def _normalize_rate(self, value: str, default: str) -> str:
        """Normalize ffmpeg rate strings to accept plain integers as kbps."""
        normalized = (value or "").strip()
        if not normalized:
            return default
        if normalized.isdigit():
            return f"{normalized}k"
        return normalized

    @property
    def playlist_path(self) -> Path:
        return self.output_dir / "live.m3u8"

    def get_public_path(self) -> str:
        return "/video/live.m3u8"

    def configure_pcm_audio(
        self, endpoint: str, sample_rate: int = 44100, channels: int = 2
    ) -> None:
        """Switch audio input to a local PCM relay exposed over TCP."""

        if not endpoint.startswith("tcp://"):
            endpoint = f"tcp://{endpoint}"

        if self.audio_source_type != "pcm" or self.audio_source != endpoint:
            logger.info("Configured video audio source to PCM relay at %s", endpoint)

        self.audio_source = endpoint
        self.audio_source_type = "pcm"
        self.audio_sample_rate = sample_rate
        self.audio_channels = channels

    def _build_audio_input_args(self) -> list[str]:
        """Configure the live audio input without probing known PCM parameters.

        Returns:
            FFmpeg arguments for the configured audio source.
        """
        if self.audio_source_type == "pcm":
            return [
                "-f",
                "s16le",
                "-ar",
                str(self.audio_sample_rate),
                "-ac",
                str(self.audio_channels),
                # The relay format is explicit. Default probing holds several
                # seconds of audio before the artwork input can even open.
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-i",
                self.audio_source,
            ]

        return [
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_at_eof",
            "1",
            "-reconnect_on_network_error",
            "1",
            "-reconnect_delay_max",
            "15",
            "-reconnect_on_http_error",
            "403,404,500,502,503,504",
            "-seekable",
            "0",
            "-timeout",
            "60000000",
            "-rw_timeout",
            "60000000",
            "-i",
            self.audio_source,
        ]

    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return

            self.overlay_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir.mkdir(parents=True, exist_ok=True)

            self._cleanup_previous_output()

            await self._write_overlay_files(initial=True)
            if not self.use_inline_background:
                await self._ensure_background_pipe()
            await self._ensure_artwork_pipe()

            # Set running flag before launching processes
            self._running = True

            await self._start_artwork_feeder()
            if not self.use_inline_background:
                # Start background generator FIRST so pipe has data before main FFmpeg connects
                await self._launch_background_generator()

            # Launch main FFmpeg with delay to let background generator start producing frames
            await self._launch_ffmpeg(
                wait_for_background=not self.use_inline_background
            )

            self.monitor_task = asyncio.create_task(self._monitor_process())
            self._last_output_timestamp = self._get_latest_output_timestamp()
            self._last_output_activity = time.time()
            self.output_monitor_task = asyncio.create_task(
                self._monitor_output_health()
            )

            logger.info("Video stream producer started")

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            self._cancel_fact_tasks()

            if self.monitor_task:
                self.monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.monitor_task
                self.monitor_task = None

            if self.ffmpeg_process:
                try:
                    self.ffmpeg_process.terminate()
                    await asyncio.wait_for(self.ffmpeg_process.wait(), timeout=5)
                except TimeoutError:
                    logger.warning("Video ffmpeg process did not terminate, killing")
                    self.ffmpeg_process.kill()
                    await self.ffmpeg_process.wait()
                except Exception as exc:
                    logger.error(f"Error stopping video ffmpeg process: {exc}")
                finally:
                    self.ffmpeg_process = None

            if self.ffmpeg_log_task:
                self.ffmpeg_log_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.ffmpeg_log_task
                self.ffmpeg_log_task = None

            if self.output_monitor_task:
                self.output_monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.output_monitor_task
                self.output_monitor_task = None

            if not self.use_inline_background:
                await self._stop_background_generator()
            await self._stop_artwork_feeder()

            if not self.use_inline_background:
                self._close_background_keepalive()

            if not self.use_inline_background and self.background_pipe_path.exists():
                try:
                    self.background_pipe_path.unlink()
                except OSError as exc:
                    logger.warning(
                        f"Could not remove background pipe {self.background_pipe_path}: {exc}"
                    )

            logger.info("Video stream producer stopped")

    async def update_overlay(
        self,
        dj: str | None = None,
        main: str | None = None,
        sub: str | None = None,
    ) -> bool:
        updates: dict[str, str | None] = {
            "dj": dj,
            "main": main,
            "sub": sub,
        }

        normalized: dict[str, str] = {}
        tasks = []
        for key, value in updates.items():
            if value is None:
                continue

            text = value.strip() if value else ""
            if key == "main":
                text = self._normalize_overlay_text(text, self.main_font_size)
            elif key == "sub":
                text = self._normalize_overlay_text(text, self.sub_font_size)

            self.current_state[key] = text
            file_path = self.overlay_dir / f"{key}.txt"
            normalized[key] = text
            tasks.append(self._write_text_file(file_path, text))

        font_changed = False

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=False)

        return font_changed

    async def update_dj(self, dj_id: str) -> None:
        name = dj_id.replace("_", " ").title()
        await self.update_overlay(dj=name)

    async def handle_metadata_update(self, metadata: dict[str, Any]) -> None:
        try:
            # Claim the live overlay for this item before any await: a
            # superseded handler's late publishes must not revert it.
            self._live_artwork_file_path = metadata.get("file_path")
            content_type = str(metadata.get("content_type", "UNKNOWN")).upper()
            title = metadata.get("title", "")
            artist = metadata.get("artist", "")

            album = metadata.get("album")
            restart_main = False

            is_music = content_type == "MUSIC" or (
                content_type == "UNKNOWN" and (artist or title)
            )

            if is_music:
                artist_key = artist or "Unknown Artist"
                title_text = title or "Unknown Title"
                track_key = self.fact_repository.build_track_key(
                    artist_key, title_text, album
                )

                if track_key != self.current_track_signature:
                    background_mode, params = self._select_background(track_key)
                    if (
                        background_mode != self.current_background_mode
                        or params != self.current_background_params
                    ):
                        self.current_background_mode = background_mode
                        self.current_background_params = params
                        # Don't restart background generator - it runs continuously
                    self.current_track_signature = track_key
                    self._schedule_song_facts(track_key, artist_key, title_text, album)

                    # Extract album artwork if file_path is available
                    file_path = metadata.get("file_path")
                    if file_path:
                        artwork_path = await self._extract_album_artwork(
                            file_path, artist_key, title_text
                        )
                        if artwork_path and artwork_path != self.current_artwork_path:
                            self.current_artwork_path = artwork_path
                            logger.info(f"📀 Updated artwork: {artwork_path.name}")
                        elif not artwork_path:
                            # No artwork found, write a per-track placeholder so
                            # the track's published artwork URL still resolves.
                            self.current_artwork_path = None
                            await self._create_placeholder_artwork(file_path)
                            logger.debug("No artwork found, using placeholder")
                    else:
                        self.current_artwork_path = None
                        await self._create_placeholder_artwork()

                font_changed = await self.update_overlay(
                    main=artist_key, sub=title_text
                )
                restart_main = restart_main or font_changed

            elif content_type == "DJ_TALK":
                self.current_track_signature = None
                self.current_artwork_path = None
                # Write the placeholder to this item's per-track file so the
                # artwork URL published in its metadata resolves (the TTS file
                # has no embedded art, but the URL is keyed on its path).
                await self._create_placeholder_artwork(metadata.get("file_path"))
                self._cancel_fact_tasks()
                # Restore DJ name when not playing music
                await self._write_text_file(
                    self.overlay_dir / "dj.txt", self.current_state["dj"]
                )

                lowercase_title = title.lower()
                if "weather" in lowercase_title:
                    font_changed = await self.update_overlay(
                        main="Weather Update", sub=""
                    )
                elif "sponsor" in lowercase_title:
                    font_changed = await self.update_overlay(
                        main="Sponsor Message", sub=""
                    )
                else:
                    font_changed = await self.update_overlay(
                        main=title or "DJ Talk", sub=""
                    )
                restart_main = restart_main or font_changed

            elif content_type == "JINGLE":
                self.current_track_signature = None
                self.current_artwork_path = None
                # Write the placeholder to this jingle's per-track file so the
                # artwork URL published in its metadata resolves.
                await self._create_placeholder_artwork(metadata.get("file_path"))
                self._cancel_fact_tasks()
                # Restore DJ name when not playing music
                await self._write_text_file(
                    self.overlay_dir / "dj.txt", self.current_state["dj"]
                )

                font_changed = await self.update_overlay(
                    main=title or "Station Jingle", sub=""
                )
                restart_main = restart_main or font_changed

            else:
                self.current_track_signature = None
                self.current_artwork_path = None
                # Unknown content type: still resolve the published artwork URL
                # (keyed on the file path) to the placeholder.
                await self._create_placeholder_artwork(metadata.get("file_path"))
                self._cancel_fact_tasks()
                # Restore DJ name for unknown content types
                await self._write_text_file(
                    self.overlay_dir / "dj.txt", self.current_state["dj"]
                )

            # Background generator runs continuously - no restarts needed
            # Only restart main FFmpeg if font changed (rare)
            if restart_main and self._running:
                await self._restart_ffmpeg()

        except Exception as exc:
            logger.error(f"Error handling metadata update for video overlay: {exc}")

    async def _write_overlay_files(self, initial: bool = False) -> None:
        defaults = {
            "dj": self.current_state["dj"],
            "main": self.current_state["main"],
            "sub": self.current_state["sub"],
        }

        tasks = []
        for key, text in defaults.items():
            file_path = self.overlay_dir / f"{key}.txt"
            if initial or not file_path.exists():
                tasks.append(self._write_text_file(file_path, text))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=False)

    async def _ensure_background_pipe(self) -> None:
        def _create_pipe() -> None:
            if self.background_pipe_path.exists():
                try:
                    self._close_background_keepalive()
                    mode = self.background_pipe_path.stat().st_mode
                    if stat.S_ISFIFO(mode):
                        return
                    self.background_pipe_path.unlink()
                except FileNotFoundError:
                    return
                except OSError as exc:
                    logger.warning(
                        f"Failed to remove old background pipe {self.background_pipe_path}: {exc}"
                    )
            os.mkfifo(self.background_pipe_path, 0o666)

        try:
            await asyncio.to_thread(_create_pipe)
        except Exception as exc:
            logger.error(
                f"Unable to prepare background pipe {self.background_pipe_path}: {exc}"
            )
            raise

    async def _ensure_artwork_pipe(self) -> None:
        def _create_pipe() -> None:
            if self.artwork_pipe_path.exists():
                try:
                    mode = self.artwork_pipe_path.stat().st_mode
                    if stat.S_ISFIFO(mode):
                        return
                    self.artwork_pipe_path.unlink()
                except FileNotFoundError:
                    return
                except OSError as exc:
                    logger.warning(
                        f"Failed to remove old artwork pipe {self.artwork_pipe_path}: {exc}"
                    )
            os.mkfifo(self.artwork_pipe_path, 0o666)

        try:
            await asyncio.to_thread(_create_pipe)
        except Exception as exc:
            logger.error(
                f"Unable to prepare artwork pipe {self.artwork_pipe_path}: {exc}"
            )
            raise

    async def _start_artwork_feeder(self) -> None:
        await self._ensure_artwork_pipe()
        if self.artwork_feeder_task and not self.artwork_feeder_task.done():
            return
        self._artwork_feeder_generation += 1
        self._artwork_feeder_running = True
        self.artwork_feeder_task = asyncio.create_task(
            self._run_artwork_feeder(self._artwork_feeder_generation)
        )

    async def _stop_artwork_feeder(self) -> None:
        self._artwork_feeder_running = False
        # Invalidate every outstanding generation so an orphaned feeder
        # thread exits on its next loop check instead of surviving a
        # restart and double-writing the new pipe.
        self._artwork_feeder_generation += 1
        if self.artwork_feeder_task:
            self.artwork_feeder_task.cancel()
            try:
                await asyncio.wait_for(self.artwork_feeder_task, timeout=3)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                logger.warning("Artwork feeder did not stop within timeout")
            self.artwork_feeder_task = None

        if self.artwork_pipe_path.exists():
            try:
                self.artwork_pipe_path.unlink()
            except OSError as exc:
                logger.warning(
                    f"Could not remove artwork pipe {self.artwork_pipe_path}: {exc}"
                )

    async def _run_artwork_feeder(self, generation: int) -> None:
        try:
            await asyncio.to_thread(self._stream_artwork_frames, generation)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"Artwork feeder stopped unexpectedly: {exc}")

    def _is_feeder_current(self, generation: int) -> bool:
        """Whether a feeder thread still owns the pipe.

        Args:
            generation: Generation captured when the feeder thread started.

        Returns:
            True only while the producer is running, the feeder is enabled,
            and no newer feeder has been started or this one stopped.
        """
        return (
            self._running
            and self._artwork_feeder_running
            and self._artwork_feeder_generation == generation
        )

    def _stream_artwork_frames(self, generation: int) -> None:
        """Feed artwork on fixed deadlines so video cannot drift behind audio.

        Args:
            generation: Feeder generation that owns the artwork pipe.

        Returns:
            None when this feeder stops or is superseded.
        """
        last_valid_frame: bytes | None = None
        last_artwork_signature: tuple[int, int] | None = None
        next_frame_at: float | None = None
        while self._is_feeder_current(generation):
            try:
                fd = open_fifo_for_writing(self.artwork_pipe_path)
            except OSError as exc:
                logger.debug(f"Artwork pipe open error: {exc}")
                time.sleep(0.2)
                continue

            if fd is None:
                # No reader connected yet (main ffmpeg restarting); retry shortly.
                time.sleep(0.1)
                continue

            try:
                with os.fdopen(fd, "wb", buffering=0) as pipe:
                    while self._is_feeder_current(generation):
                        data: bytes | None = None
                        try:
                            stat_result = self.current_artwork_file.stat()
                            artwork_signature = (
                                stat_result.st_mtime_ns,
                                stat_result.st_size,
                            )
                        except FileNotFoundError:
                            artwork_signature = None
                        except OSError as exc:
                            logger.debug(f"Artwork stat failed: {exc}")
                            artwork_signature = None

                        if (
                            artwork_signature
                            and artwork_signature != last_artwork_signature
                        ):
                            try:
                                data = self.current_artwork_file.read_bytes()
                            except FileNotFoundError:
                                data = b""
                            except OSError as exc:
                                logger.debug(f"Artwork read failed: {exc}")
                                data = b""
                            last_artwork_signature = artwork_signature

                        if data is not None:
                            if self._is_valid_jpeg_frame(data):
                                last_valid_frame = data
                            elif data:
                                logger.debug(
                                    "Skipping invalid artwork frame (%s bytes) for %s",
                                    len(data),
                                    self.current_artwork_file,
                                )

                        frame = last_valid_frame
                        if not frame:
                            time.sleep(0.1)
                            continue

                        if next_frame_at is None:
                            next_frame_at = time.monotonic()
                        pipe.write(frame)
                        pipe.flush()
                        # FFmpeg gives each image a fixed one-second timestamp.
                        # Sleeping a second AFTER the write adds I/O and wakeup
                        # delays to every frame, eventually holding audio back
                        # while drawtext already displays the next track.
                        next_frame_at += self._artwork_frame_interval
                        delay = next_frame_at - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
            except BrokenPipeError:
                time.sleep(0.2)
                continue
            except OSError as exc:
                logger.debug(f"Artwork pipe write error: {exc}")
                time.sleep(0.2)
                continue

    def _open_background_keepalive(self) -> None:
        if self._background_keepalive_fd is not None:
            return

        try:
            # Open as reader (not writer) to prevent blocking the background generator
            # This keeps the pipe open even if the main ffmpeg temporarily stops reading
            self._background_keepalive_fd = os.open(
                self.background_pipe_path, os.O_RDONLY | os.O_NONBLOCK
            )
            logger.debug("Background pipe keepalive reader opened successfully")
        except FileNotFoundError:
            logger.warning("Background pipe not available for keepalive open yet")
        except OSError as exc:
            logger.warning(
                f"Error opening keepalive reader for {self.background_pipe_path}: {exc}"
            )

    def _close_background_keepalive(self) -> None:
        if self._background_keepalive_fd is None:
            return

        try:
            os.close(self._background_keepalive_fd)
        except OSError as exc:
            logger.debug(
                f"Error closing keepalive writer for {self.background_pipe_path}: {exc}"
            )
        finally:
            self._background_keepalive_fd = None

    async def _stop_background_generator(self, cancel_monitor: bool = True) -> None:
        if cancel_monitor and self.background_monitor_task:
            self.background_monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.background_monitor_task
            self.background_monitor_task = None

        if self.background_log_task:
            self.background_log_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.background_log_task
            self.background_log_task = None

        if self.background_process and self.background_process.returncode is None:
            try:
                # For background generator, kill immediately - terminate doesn't work when blocked on pipe writes
                logger.info("Stopping background generator")
                self.background_process.kill()
                await asyncio.wait_for(self.background_process.wait(), timeout=2)
            except TimeoutError:
                logger.warning("Background generator did not terminate after kill")
            except ProcessLookupError:
                pass
            except Exception as exc:
                logger.error(f"Error stopping background generator: {exc}")

        self.background_process = None
        if cancel_monitor:
            self.background_monitor_task = None

    async def _stop_ffmpeg_process(self) -> None:
        """Terminate the main HLS ffmpeg writer if it is still running."""

        process = self.ffmpeg_process
        self.ffmpeg_process = None

        if not process:
            return

        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            logger.warning("Video ffmpeg process did not terminate in time, killing")
            process.kill()
            with suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=3)
        except Exception as exc:
            logger.error(f"Error stopping video ffmpeg process: {exc}")

    async def _kill_orphan_hls_processes(self) -> None:
        """Best-effort cleanup of stray HLS writers targeting our output path."""

        try:
            cmd = [
                "pkill",
                "-f",
                str(self.playlist_path),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except FileNotFoundError:
            # pkill unavailable; nothing to do
            return
        except Exception as exc:
            logger.debug(f"Could not clean orphan HLS writers: {exc}")

    async def _launch_background_generator(self) -> None:
        async with self._background_lock:
            if not self._running:
                return

            await self._stop_background_generator()

            command = self._build_background_generator_command()

            # Log the complete command for debugging
            logger.info(
                f"Launching background generator with command: {' '.join(command)}"
            )

            try:
                self.background_process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                logger.error("ffmpeg not found while starting background generator")
                return
            except Exception as exc:
                logger.error(f"Failed to start background generator: {exc}")
                return

            if self.background_process.stderr:
                self.background_log_task = asyncio.create_task(
                    self._log_process_stream(
                        self.background_process.stderr, "background"
                    )
                )

            self.background_monitor_task = asyncio.create_task(
                self._monitor_background_process()
            )

    async def _restart_background_generator(self) -> None:
        if not self._running:
            return
        await self._launch_background_generator()

    async def _monitor_background_process(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(5)
                process = self.background_process
                if not process:
                    continue

                if process.returncode is None:
                    continue

                logger.error(
                    "Background generator exited with code %s, scheduling restart",
                    process.returncode,
                )

                await self._stop_background_generator(cancel_monitor=False)

                if not self._running:
                    break

                asyncio.create_task(self._launch_background_generator())
                break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Error monitoring background generator: {exc}")
        finally:
            self.background_monitor_task = None

    def _build_background_generator_command(self) -> list[str]:
        filter_graph = self._build_background_filter_graph()
        logger.info(
            f"Background generator using filter graph (length {len(filter_graph)} chars)"
        )
        color_input = f"color=c={self.background_color}:s=1280x720:r={self.video_fps}"

        command: list[str] = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-thread_queue_size",
            "2048",
            "-fflags",
            "+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-y",
        ]

        command.extend(self._build_audio_input_args())
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                color_input,
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-filter_complex",
                filter_graph,
                "-map",
                "[bg_ready]",
                "-pix_fmt",
                "rgba",
                "-fps_mode",
                "cfr",
                "-f",
                "rawvideo",
                str(self.background_pipe_path),
            ]
        )

        return command

    def _build_background_filter_graph(self) -> str:
        logger.info(
            f"Building background filter graph with waveform_height={self.waveform_height}"
        )

        filter_parts: list[str] = []

        if self.waveform_enabled:
            filter_parts.append(
                "[0:a]aresample=async=1:first_pts=0,asetpts=N/SR/TB[a_primary]"
            )
            filter_parts.append(
                "[2:a]aresample=async=1:first_pts=0,asetpts=N/SR/TB[a_fallback]"
            )
            filter_parts.append(
                "[a_primary][a_fallback]amix=inputs=2:duration=longest:dropout_transition=4[a_audio]"
            )
            filter_parts.append("[a_audio]asplit=2[a_wave][a_bg]")
        else:
            filter_parts.append(
                "[0:a]aresample=async=1:first_pts=0,asetpts=N/SR/TB[a_primary]"
            )
            filter_parts.append(
                "[2:a]aresample=async=1:first_pts=0,asetpts=N/SR/TB[a_fallback]"
            )
            filter_parts.append(
                "[a_primary][a_fallback]amix=inputs=2:duration=longest:dropout_transition=4[a_bg]"
            )

        filter_parts.append("[1:v]format=rgba,split[bg_base][bg_base_fx]")

        background_filters, background_label = self._build_background_filters(
            "[a_bg]", "[bg_base]", "[bg_base_fx]"
        )
        filter_parts.extend(background_filters)

        if self.waveform_enabled:
            filter_parts.append(
                f"[a_wave]showwaves=s={self.waveform_width}x{self.waveform_height}:mode={self.waveform_mode}:rate={self.video_fps}:colors={self.waveform_colors},format=rgba[wave_ready]"
            )
            filter_parts.append(
                f"{background_label}[wave_ready]overlay=x='{self.waveform_x}':y='{self.waveform_y}'[bg_ready]"
            )
        else:
            filter_parts.append(f"{background_label}copy[bg_ready]")

        filter_parts.append("[bg_ready]format=rgba[bg_ready]")

        filter_graph = ";".join(filter_parts)
        logger.info(f"Complete background filter graph: {filter_graph}")
        return filter_graph

    async def _log_process_stream(
        self, stream: asyncio.StreamReader, prefix: str
    ) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if prefix == "video":
                    self._ffmpeg_recent_lines.append(text)
                if self._should_downgrade_ffmpeg_output(text):
                    logger.debug("%s ffmpeg: %s", prefix, text)
                else:
                    logger.warning("%s ffmpeg: %s", prefix, text)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug(f"Error reading {prefix} ffmpeg output: {exc}")

    def _should_downgrade_ffmpeg_output(self, message: str) -> bool:
        lowered = message.lower()
        return any(token in lowered for token in self._ffmpeg_noise_tokens)

    def _load_video_theme_config(self) -> dict[str, Any]:
        """Load video theme configuration from config file."""
        config_path = Path("/app/config/video_theme.json")
        default_config = {
            "fonts": {
                "artist": {
                    "path": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "size": 42,
                },
                "song": {
                    "path": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "size": 32,
                },
                "dj_name": {
                    "path": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "size": 36,
                },
                "clock": {
                    "path": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "size": 40,
                },
                "facts": {
                    "path": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "size": 36,
                },
            },
            "visualizer": {"mode": "spectrum"},
            "layout": {
                "margins": {"side": 80, "top": 160, "bottom": 80},
                "spacing": {"main": 160, "sub": 90},
                "max_text_width": 1060,
            },
        }

        try:
            if config_path.exists():
                with config_path.open() as f:
                    config = json.load(f)
                logger.info(f"Loaded video theme config from {config_path}")
                return config
            else:
                logger.warning(
                    f"Video theme config not found at {config_path}, using defaults"
                )
                return default_config
        except Exception as e:
            logger.error(f"Error loading video theme config: {e}, using defaults")
            return default_config

    def _apply_theme_config(self) -> None:
        """Apply the video theme configuration to instance variables."""
        fonts = self.video_theme_config.get("fonts", {})
        layout = self.video_theme_config.get("layout", {})

        # Apply font configurations
        self.font_path = Path(
            fonts.get("artist", {}).get(
                "path", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            )
        )

        # Apply font sizes
        self.base_main_font_size = fonts.get("artist", {}).get("size", 42)
        self.base_sub_font_size = fonts.get("song", {}).get("size", 32)

        # Apply layout settings
        margins = layout.get("margins", {})
        self.overlay_margin_side = margins.get("side", 80)
        self.overlay_margin_top = margins.get("top", 160)
        self.overlay_bottom_margin = margins.get("bottom", 80)

        spacing = layout.get("spacing", {})
        self.overlay_main_spacing = spacing.get("main", 160)
        self.overlay_sub_spacing = spacing.get("sub", 90)

        self.max_text_width = layout.get("max_text_width", 1060)

        # Background colour for the video feed
        self.background_color = self.video_theme_config.get(
            "background_color", "#1a1a2e"
        )

        # Derive default offsets for artist/song positions from margins + spacing
        _main_offset = self.overlay_bottom_margin + self.overlay_main_spacing
        _sub_offset = self.overlay_bottom_margin + self.overlay_sub_spacing
        _facts_y_base = self.overlay_margin_top + 60

        # Per-element position overrides — values are FFmpeg expressions (str) or ints
        dj_name_font = fonts.get("dj_name", {})
        self.dj_name_x = str(dj_name_font.get("x", self.overlay_margin_side))
        self.dj_name_y = str(dj_name_font.get("y", self.overlay_margin_top))

        clock_font_cfg = fonts.get("clock", {})
        self.clock_x = str(clock_font_cfg.get("x", f"w-tw-{self.overlay_margin_side}"))
        self.clock_y = str(clock_font_cfg.get("y", self.overlay_margin_top))

        artist_font_cfg = fonts.get("artist", {})
        self.artist_x = str(artist_font_cfg.get("x", self.overlay_margin_side))
        self.artist_y = str(artist_font_cfg.get("y", f"h-{_main_offset}"))

        song_font_cfg = fonts.get("song", {})
        self.song_x = str(song_font_cfg.get("x", self.overlay_margin_side))
        self.song_y = str(song_font_cfg.get("y", f"h-{_sub_offset}"))

        facts_font_cfg = fonts.get("facts", {})
        self.facts_x = str(facts_font_cfg.get("x", self.overlay_margin_side))
        self.facts_y_base = int(facts_font_cfg.get("y_base", _facts_y_base))

        # Album art settings
        album_art = self.video_theme_config.get("album_art", {})
        self.artwork_enabled = album_art.get("enabled", True)
        self.artwork_size = album_art.get("size", 200)
        self.artwork_x = str(album_art.get("x", f"W-w-{self.overlay_margin_side}"))
        self.artwork_y = str(album_art.get("y", f"H-h-{self.overlay_bottom_margin}"))

        # Waveform settings
        waveform = self.video_theme_config.get("waveform", {})
        self.waveform_enabled = waveform.get("enabled", True)
        self.waveform_width = waveform.get("width", 1280)
        self.waveform_height = waveform.get("height", 600)
        self.waveform_x = waveform.get("x", "(W-w)/2")
        self.waveform_y = waveform.get("y", "(H-h)/2")
        self.waveform_mode = waveform.get("mode", "line")
        self.waveform_colors = waveform.get(
            "colors", "red|orange|yellow|green|cyan|blue|purple|pink"
        )

        logger.info(
            f"Applied video theme: font={self.font_path.name}, visualizer={self.video_theme_config.get('visualizer', {}).get('mode', 'spectrum')}"
        )

    def _select_background(self, seed_source: str) -> tuple[str, dict[str, Any]]:
        """Select background visualizer based on configuration."""
        visualizer_config = self.video_theme_config.get("visualizer", {})
        mode = visualizer_config.get("mode", "spectrum")

        # Validate mode is available
        available_modes = visualizer_config.get(
            "available_modes", ["spectrum", "vectorscope", "nebula", "grid"]
        )
        if mode not in available_modes:
            logger.warning(
                f"Configured visualizer mode '{mode}' not available, falling back to spectrum"
            )
            mode = "spectrum"

        digest = hashlib.sha1(seed_source.encode("utf-8", "ignore")).digest()
        params: dict[str, Any] = {}

        if mode == "spectrum":
            spectrum_config = visualizer_config.get("spectrum", {})
            spectrum_modes = ("combined", "separate")
            slide_modes = ("scroll", "fullframe")

            # Use configured spectrum settings or fall back to deterministic defaults
            if "display_mode" in spectrum_config:
                params["mode"] = spectrum_config["display_mode"]
            else:
                params["mode"] = spectrum_modes[digest[1] % len(spectrum_modes)]

            if "slide_mode" in spectrum_config:
                params["slide"] = spectrum_config["slide_mode"]
            else:
                params["slide"] = slide_modes[digest[2] % len(slide_modes)]

        elif mode == "vectorscope":
            scope_modes = ("lissajous", "polar")
            params["mode"] = scope_modes[digest[1] % len(scope_modes)]
        elif mode == "nebula":
            params["freq_x"] = 18 + digest[1] % 24
            params["freq_y"] = 22 + digest[2] % 28
            params["freq_mix"] = 16 + digest[3] % 20
        elif mode == "grid":
            params["scale_x"] = 18 + digest[1] % 24
            params["scale_y"] = 22 + digest[2] % 28
            params["speed"] = 18 + digest[3] % 25

        return mode, params

    def _build_background_filters(
        self,
        audio_label: str,
        base_label: str,
        fx_base_label: str,
    ) -> tuple[list[str], str]:
        mode = self.current_background_mode or "spectrum"
        params = self.current_background_params or {}

        filters: list[str] = []
        uses_audio = False
        uses_fx_base = False

        if mode == "spectrum":
            spectrum_mode = params.get("mode", "combined")
            slide_mode = params.get("slide", "scroll")
            uses_audio = True
            filters.append(
                f"{audio_label}showspectrum=s=1280x720:mode={spectrum_mode}:color=rainbow:slide={slide_mode}:scale=log[bg_spec_raw]"
            )
            filters.append("[bg_spec_raw]format=rgba[bg_effect]")
        elif mode == "vectorscope":
            scope_mode = params.get("mode", "lissajous")
            uses_audio = True
            filters.append(
                f"{audio_label}avectorscope=s=1280x720:mode={scope_mode}:draw=line[bg_vec_raw]"
            )
            filters.append(
                "[bg_vec_raw]format=rgba,gblur=sigma=6,eq=contrast=1.35:brightness=0.05[bg_effect]"
            )
        elif mode == "nebula":
            freq_x = params.get("freq_x", 24)
            freq_y = params.get("freq_y", 30)
            freq_mix = params.get("freq_mix", 18)
            uses_fx_base = True
            filters.append(
                f"{fx_base_label}geq="
                f"r='(sin((X+T*{freq_x})*0.018)+sin((Y+T*{freq_y})*0.014)+2)/4*255':"
                f"g='(sin((X-T*{freq_y})*0.015)+sin((Y+T*{freq_mix})*0.019)+2)/4*255':"
                f"b='(sin((X+Y+T*{freq_mix})*0.012)+1)/2*255':"
                "a='255'[bg_effect]"
            )
        elif mode == "grid":
            scale_x = params.get("scale_x", 24)
            scale_y = params.get("scale_y", 30)
            speed = params.get("speed", 20)
            uses_fx_base = True
            filters.append(
                f"{fx_base_label}geq="
                f"r='(sin((X/{scale_x})+T*0.{speed})+1)/2*255':"
                f"g='(sin((Y/{scale_y})+T*0.{speed + 5})+1)/2*255':"
                f"b='(sin(((X+Y)/{(scale_x + scale_y) // 2})+T*0.{speed + 3})+1)/2*255':"
                "a='255'[bg_effect]"
            )
        else:
            uses_fx_base = True
            filters.append(f"{fx_base_label}copy[bg_effect]")

        if not uses_audio:
            filters.append(f"{audio_label}anullsink")
        if not uses_fx_base:
            filters.append(f"{fx_base_label}nullsink")

        filters.append(
            f"{base_label}[bg_effect]blend=all_mode='overlay':all_opacity=0.85[bg_out]"
        )

        return filters, "[bg_out]"

    def _cleanup_previous_output(self) -> None:
        """Remove old HLS segments and playlists before starting a new stream."""
        try:
            if not self.output_dir.exists():
                return

            patterns = ["*.ts", "*.m3u8", "*.tmp", "*.vtt"]
            for pattern in patterns:
                for file_path in self.output_dir.glob(pattern):
                    try:
                        file_path.unlink()
                    except FileNotFoundError:
                        continue
                    except Exception as exc:
                        logger.warning(
                            f"Could not remove stale video output file {file_path}: {exc}"
                        )

        except Exception as exc:
            logger.error(f"Failed to clean previous video output: {exc}")
        finally:
            self._last_output_timestamp = 0.0
            self._last_output_activity = 0.0

    def _write_overlay_text_file_sync(self, file_path: Path, text: str) -> None:
        tmp_path = file_path.with_name(f".{file_path.name}.{time.time_ns()}.tmp")
        try:
            tmp_path.write_text(text or "", encoding="utf-8")
            tmp_path.replace(file_path)
        finally:
            if tmp_path.exists():
                with suppress(OSError):
                    tmp_path.unlink()

    async def _write_text_file(self, file_path: Path, text: str) -> None:
        await asyncio.to_thread(
            self._write_overlay_text_file_sync, file_path, text or ""
        )

    def _write_current_artwork_bytes(
        self, data: bytes, target: Path | None = None
    ) -> None:
        if not data:
            return

        destination = target or self.current_artwork_file
        tmp_path = self.artwork_dir / f".current_{time.time_ns()}.jpg.tmp"
        try:
            tmp_path.write_bytes(data)
            tmp_path.replace(destination)
        finally:
            if tmp_path.exists():
                with suppress(OSError):
                    tmp_path.unlink()

    def _save_current_artwork_image(
        self,
        image: Any,
        *,
        quality: int = 85,
        optimize: bool = False,
        target: Path | None = None,
    ) -> None:
        destination = target or self.current_artwork_file
        tmp_path = self.artwork_dir / f".current_{time.time_ns()}.jpg.tmp"
        try:
            save_kwargs: dict[str, Any] = {"quality": quality}
            if optimize:
                save_kwargs["optimize"] = True
            image.save(tmp_path, "JPEG", **save_kwargs)
            tmp_path.replace(destination)
        finally:
            if tmp_path.exists():
                with suppress(OSError):
                    tmp_path.unlink()

    def _normalize_artwork_image(self, image: Any) -> Any:
        """Fit and letterbox onto a fixed 200x200 RGB canvas.

        Every frame on the artwork pipe must be exactly the same size: a
        mid-stream resolution change on the mjpeg input wedges the encoder's
        filter graph (observed: encoder busy-spins at ~80% CPU producing no
        segments until killed). Fixed geometry also keeps the on-screen
        artwork box from jumping between tracks.

        Args:
            image: Open PIL image (any mode/size).

        Returns:
            A 200x200 RGB PIL image with the source letterboxed in black.
        """
        img = image
        if img.width > 200 or img.height > 200:
            img.thumbnail((200, 200), Image.Resampling.LANCZOS)
        if img.mode in ("RGBA", "LA", "P"):
            rgb_img = Image.new("RGB", img.size, (0, 0, 0))
            if img.mode == "P":
                img = img.convert("RGBA")
            rgb_img.paste(
                img,
                mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None,
            )
            img = rgb_img
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if img.size != (200, 200):
            canvas = Image.new("RGB", (200, 200), (0, 0, 0))
            canvas.paste(img, ((200 - img.width) // 2, (200 - img.height) // 2))
            img = canvas
        return img

    def _publish_artwork_image(
        self, image: Any, file_path: str | None, *, optimize: bool = True
    ) -> Path:
        """Write normalized artwork to the per-track file and the live overlay.

        The per-track file is content-addressed by file_path (see
        artwork_filename_for) so a lagged listener loads the art matching the
        track they hear, while current.jpg keeps feeding the FFmpeg overlay.

        Args:
            image: Open (unnormalized) PIL image.
            file_path: Source audio path, or None for the shared placeholder.

        Returns:
            Path of the per-track artwork file that was written.
        """
        normalized = self._normalize_artwork_image(image)
        per_track = self.artwork_dir / artwork_filename_for(file_path)
        self._save_current_artwork_image(
            normalized, quality=85, optimize=optimize, target=per_track
        )
        if file_path == self._live_artwork_file_path:
            self._save_current_artwork_image(normalized, quality=85, optimize=optimize)
        return per_track

    def _publish_artwork_bytes(
        self, data: bytes, file_path: str | None
    ) -> Path:
        """Raw-bytes fallback (no PIL) for _publish_artwork_image.

        Args:
            data: Artwork bytes (assumed already a valid JPEG).
            file_path: Source audio path, or None for the shared placeholder.

        Returns:
            Path of the per-track artwork file that was written.
        """
        per_track = self.artwork_dir / artwork_filename_for(file_path)
        self._write_current_artwork_bytes(data, target=per_track)
        if file_path == self._live_artwork_file_path:
            self._write_current_artwork_bytes(data)
        return per_track

    def _is_valid_jpeg_frame(self, data: bytes) -> bool:
        if len(data) < 4:
            return False
        if not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
            return False
        if not Image:
            return True

        from io import BytesIO

        try:
            with Image.open(BytesIO(data)) as img:
                if img.format != "JPEG":
                    return False
                img.verify()
            return True
        except Exception:
            return False

    async def _create_placeholder_artwork(self, file_path: str | None = None) -> None:
        """Create placeholder artwork for a track with no artwork.

        Writes the placeholder to both the per-track file (so the track's
        published artwork URL always resolves) and the live overlay file.

        Args:
            file_path: Source audio path the placeholder stands in for, or
                None to use the shared placeholder image.
        """

        def _create():
            try:
                if Image:
                    # Create 200x200 black image as placeholder (JPEG doesn't support transparency)
                    img = Image.new("RGB", (200, 200), (0, 0, 0))
                    self._publish_artwork_image(img, file_path)
                    logger.debug("Created placeholder artwork")
                else:
                    # If PIL not available, create a minimal valid JPEG
                    # This is a 1x1 black pixel JPEG
                    minimal_jpeg = bytes(
                        [
                            0xFF,
                            0xD8,
                            0xFF,
                            0xE0,
                            0x00,
                            0x10,
                            0x4A,
                            0x46,
                            0x49,
                            0x46,
                            0x00,
                            0x01,
                            0x01,
                            0x00,
                            0x00,
                            0x01,
                            0x00,
                            0x01,
                            0x00,
                            0x00,
                            0xFF,
                            0xDB,
                            0x00,
                            0x43,
                            0x00,
                            0x08,
                            0x06,
                            0x06,
                            0x07,
                            0x06,
                            0x05,
                            0x08,
                            0x07,
                            0x07,
                            0x07,
                            0x09,
                            0x09,
                            0x08,
                            0x0A,
                            0x0C,
                            0x14,
                            0x0D,
                            0x0C,
                            0x0B,
                            0x0B,
                            0x0C,
                            0x19,
                            0x12,
                            0x13,
                            0x0F,
                            0x14,
                            0x1D,
                            0x1A,
                            0x1F,
                            0x1E,
                            0x1D,
                            0x1A,
                            0x1C,
                            0x1C,
                            0x20,
                            0x24,
                            0x2E,
                            0x27,
                            0x20,
                            0x22,
                            0x2C,
                            0x23,
                            0x1C,
                            0x1C,
                            0x28,
                            0x37,
                            0x29,
                            0x2C,
                            0x30,
                            0x31,
                            0x34,
                            0x34,
                            0x34,
                            0x1F,
                            0x27,
                            0x39,
                            0x3D,
                            0x38,
                            0x32,
                            0x3C,
                            0x2E,
                            0x33,
                            0x34,
                            0x32,
                            0xFF,
                            0xC0,
                            0x00,
                            0x0B,
                            0x08,
                            0x00,
                            0x01,
                            0x00,
                            0x01,
                            0x01,
                            0x01,
                            0x11,
                            0x00,
                            0xFF,
                            0xC4,
                            0x00,
                            0x14,
                            0x00,
                            0x01,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x00,
                            0x03,
                            0xFF,
                            0xDA,
                            0x00,
                            0x08,
                            0x01,
                            0x01,
                            0x00,
                            0x00,
                            0x3F,
                            0x00,
                            0x37,
                            0xFF,
                            0xD9,
                        ]
                    )
                    self._publish_artwork_bytes(minimal_jpeg, file_path)
            except Exception as e:
                logger.warning(f"Could not create placeholder artwork: {e}")

        await asyncio.to_thread(_create)

    async def _extract_album_artwork(
        self, audio_file_path: str, artist: str = "", title: str = ""
    ) -> Path | None:
        """
        Extract album artwork from MP3 file, with remote API fallback.

        Args:
            audio_file_path: Path to the audio file
            artist: Artist name for remote lookup fallback
            title: Track title for remote lookup fallback

        Returns:
            Path to extracted artwork file, or None if no artwork found
        """
        try:
            # Try local extraction first
            def _extract():
                try:
                    audio = MP3(audio_file_path, ID3=ID3)
                    if not audio.tags:
                        return None

                    # Find APIC frame (album artwork)
                    for tag in audio.tags.values():
                        if isinstance(tag, APIC):
                            # Publish to the per-track file (content-addressed by
                            # audio_file_path) plus the live current.jpg overlay.
                            if Image:
                                from io import BytesIO

                                try:
                                    img = Image.open(BytesIO(tag.data))
                                    artwork_path = self._publish_artwork_image(
                                        img, audio_file_path
                                    )
                                    logger.info(
                                        f"📀 Extracted album artwork: {artwork_path.name}"
                                    )
                                    return artwork_path
                                except Exception as e:
                                    logger.warning(f"Could not process artwork: {e}")
                                    # Fall through to raw-bytes publish below

                            # No PIL: only publish if the embedded art is already a
                            # valid JPEG; otherwise leave it to the placeholder.
                            if self._is_valid_jpeg_frame(tag.data):
                                artwork_path = self._publish_artwork_bytes(
                                    tag.data, audio_file_path
                                )
                                logger.info(
                                    f"📀 Extracted album artwork (raw): {artwork_path.name}"
                                )
                                return artwork_path
                            return None

                    return None

                except Exception as e:
                    logger.debug(f"No album artwork found in {audio_file_path}: {e}")
                    return None

            local_artwork = await asyncio.to_thread(_extract)
            if local_artwork:
                return local_artwork

            # No local artwork - try remote fallback
            if artist and title:
                remote_artwork = await self._fetch_remote_artwork(
                    artist, title, audio_file_path
                )
                if remote_artwork:
                    return remote_artwork

            return None

        except Exception as e:
            logger.debug(f"Error extracting album artwork: {e}")
            return None

    async def _fetch_remote_artwork(
        self, artist: str, title: str, audio_file_path: str
    ) -> Path | None:
        """
        Fetch album artwork from multiple sources with fallbacks.

        Tries in order:
        1. iTunes Search API (most reliable, no rate limits)
        2. Deezer API (good coverage, no auth required)
        3. MusicBrainz/Cover Art Archive (comprehensive but rate limited)

        Results are published to the per-track artwork file (content-addressed
        by audio_file_path) so the track's published URL resolves to the right
        art for lagged listeners, with a remote_<hash>.jpg cache for dedup.

        Args:
            artist: Artist name
            title: Track title
            audio_file_path: Source audio path, for the per-track filename.

        Returns:
            Path to the per-track artwork file, or None if not found
        """
        try:
            from io import BytesIO

            import aiohttp

            # Create cache key
            cache_key = f"{artist}_{title}".lower().replace(" ", "_")
            artwork_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
            artwork_path = self.artwork_dir / f"remote_{artwork_hash}.jpg"
            negative_cache_path = self.artwork_dir / f"remote_{artwork_hash}.notfound"

            # Check negative cache (don't retry failed lookups for 24 hours)
            if negative_cache_path.exists():
                cache_age = time.time() - negative_cache_path.stat().st_mtime
                if cache_age < 86400:  # 24 hours
                    logger.debug(
                        f"Skipping artwork lookup (negative cache): {artist} - {title}"
                    )
                    return None
                else:
                    negative_cache_path.unlink()  # Expired, try again

            # Check if already cached
            if artwork_path.exists() and Image:
                try:
                    img = Image.open(artwork_path)
                    per_track = self._publish_artwork_image(img, audio_file_path)
                    logger.info(f"🎨 Using cached artwork for {artist} - {title}")
                    return per_track
                except Exception as e:
                    logger.debug(f"Cached artwork invalid, refetching: {e}")
                    artwork_path.unlink()

            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # Try iTunes first (most reliable)
                artwork_url = await self._try_itunes_artwork(session, artist, title)

                # Try Deezer if iTunes failed
                if not artwork_url:
                    artwork_url = await self._try_deezer_artwork(session, artist, title)

                # Try MusicBrainz as last resort
                if not artwork_url:
                    artwork_url = await self._try_musicbrainz_artwork(
                        session, artist, title
                    )

                if not artwork_url:
                    # Mark as not found to avoid repeated lookups
                    negative_cache_path.touch()
                    logger.debug(f"No artwork found for {artist} - {title}")
                    return None

                # Download the artwork
                try:
                    async with session.get(
                        artwork_url, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status != 200:
                            negative_cache_path.touch()
                            return None

                        image_data = await resp.read()
                        if (
                            not image_data or len(image_data) < 1000
                        ):  # Too small to be valid
                            negative_cache_path.touch()
                            return None

                        # Save and process image
                        if Image:
                            img = Image.open(BytesIO(image_data))
                            # Refresh the remote cache (normalized) for future dedup.
                            self._save_current_artwork_image(
                                self._normalize_artwork_image(img),
                                quality=90,
                                target=artwork_path,
                            )
                            per_track = self._publish_artwork_image(
                                img, audio_file_path
                            )
                            logger.info(f"🎨 Downloaded artwork for {artist} - {title}")
                            return per_track
                        else:
                            # No PIL - save raw to the remote cache and per-track file
                            artwork_path.write_bytes(image_data)
                            per_track = self._publish_artwork_bytes(
                                image_data, audio_file_path
                            )
                            logger.info(f"🎨 Downloaded artwork for {artist} - {title}")
                            return per_track

                except Exception as e:
                    logger.debug(f"Failed to download artwork: {e}")
                    return None

            return None

        except Exception as e:
            logger.debug(f"Remote artwork fetch failed for {artist} - {title}: {e}")
            return None

    async def _try_itunes_artwork(
        self, session: "aiohttp.ClientSession", artist: str, title: str
    ) -> str | None:
        """Try to get artwork URL from iTunes Search API."""
        try:
            import urllib.parse

            search_term = urllib.parse.quote(f"{artist} {title}")
            url = f"https://itunes.apple.com/search?term={search_term}&media=music&limit=1"

            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                results = data.get("results", [])
                if results:
                    # Get high-res artwork (replace 100x100 with 500x500)
                    artwork_url = results[0].get("artworkUrl100", "")
                    if artwork_url:
                        return artwork_url.replace("100x100", "500x500")
        except Exception as e:
            logger.debug(f"iTunes lookup failed: {e}")
        return None

    async def _try_deezer_artwork(
        self, session: "aiohttp.ClientSession", artist: str, title: str
    ) -> str | None:
        """Try to get artwork URL from Deezer API."""
        try:
            import urllib.parse

            search_term = urllib.parse.quote(f"{artist} {title}")
            url = f"https://api.deezer.com/search?q={search_term}&limit=1"

            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                tracks = data.get("data", [])
                if tracks:
                    album = tracks[0].get("album", {})
                    # Get the large cover (500x500)
                    return album.get("cover_big") or album.get("cover_medium")
        except Exception as e:
            logger.debug(f"Deezer lookup failed: {e}")
        return None

    async def _try_musicbrainz_artwork(
        self, session: "aiohttp.ClientSession", artist: str, title: str
    ) -> str | None:
        """Try to get artwork URL from MusicBrainz/Cover Art Archive."""
        try:
            search_url = "https://musicbrainz.org/ws/2/recording/"
            params = {
                "query": f'artist:"{artist}" AND recording:"{title}"',
                "fmt": "json",
                "limit": 1,
            }
            # MusicBrainz asks for a descriptive User-Agent. Override via env var
            # to set a real contact address before deploying.
            ua = os.getenv(
                "MUSICBRAINZ_USER_AGENT",
                f"{self.station_name.replace(' ', '')}/1.0",
            )
            headers = {"User-Agent": ua}

            async with session.get(search_url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                recordings = data.get("recordings", [])
                if not recordings:
                    return None

                recording = recordings[0]
                releases = recording.get("releases", [])
                if not releases:
                    return None

                release_id = releases[0].get("id")
                if release_id:
                    return f"https://coverartarchive.org/release/{release_id}/front-500"
        except Exception as e:
            logger.debug(f"MusicBrainz lookup failed: {e}")
        return None

    def _normalize_overlay_text(self, text: str, font_size: int) -> str:
        if not text:
            return ""

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            lines = [text.strip()]

        truncated = [
            self._truncate_with_ellipsis(line, font_size, self.max_text_width)
            for line in lines
        ]
        return "\n".join(truncated)

    def set_fact_generator(
        self, generator: Callable[[str, str, str | None], Awaitable[list[str]]]
    ) -> None:
        self.fact_generator = generator

    def _format_fact_text(
        self, text: str, max_width: int = 768, max_lines: int = 5
    ) -> list[str]:
        """Format fact text into a list of lines for display.

        Returns a list of lines (without newlines) to work around FFmpeg 7.x
        bug where newline characters are rendered as visible glyphs.
        """
        if not text:
            return []

        normalized = text.replace("\n", " ").strip()
        if not normalized:
            return []

        # Use configured facts font settings
        fonts = self.video_theme_config.get("fonts", {})
        facts_font = fonts.get("facts", {})
        font_size = facts_font.get("size", 36)
        max_width = facts_font.get("max_width", 768)
        max_lines = facts_font.get("max_lines", 5)

        lines = self._wrap_text_for_width(normalized, font_size, max_width)
        if max_lines and len(lines) > max_lines:
            trimmed = lines[:max_lines]
            trimmed[-1] = self._truncate_with_ellipsis(
                trimmed[-1], font_size, max_width
            )
            lines = trimmed

        return lines

    def _wrap_text_for_width(
        self, text: str, font_size: int, max_width: int
    ) -> list[str]:
        words = text.split()
        if not words:
            return [text]

        lines: list[str] = []
        current_line = ""

        for word in words:
            candidate = f"{current_line} {word}".strip()
            if not current_line:
                if self._measure_text(word, font_size) <= max_width:
                    current_line = word
                else:
                    segments = self._split_long_word(word, font_size, max_width)
                    if segments:
                        current_line = segments.pop(0)
                        lines.extend(segments)
                    else:
                        current_line = word
                continue

            if self._measure_text(candidate, font_size) <= max_width:
                current_line = candidate
                continue

            lines.append(current_line)
            if self._measure_text(word, font_size) <= max_width:
                current_line = word
            else:
                segments = self._split_long_word(word, font_size, max_width)
                if segments:
                    current_line = segments.pop(0)
                    lines.extend(segments)
                else:
                    current_line = word

        if current_line:
            lines.append(current_line)

        return lines

    def _split_long_word(self, word: str, font_size: int, max_width: int) -> list[str]:
        if not word:
            return []

        approx_chars = max(int(max_width / max(font_size * 0.55, 1)), 1)
        segments: list[str] = []
        index = 0
        length = len(word)

        while index < length:
            end = min(length, index + approx_chars)
            best_segment = None
            while end > index:
                candidate = word[index:end]
                if self._measure_text(candidate, font_size) <= max_width:
                    best_segment = candidate
                    break
                end -= 1

            if not best_segment:
                best_segment = word[index : index + 1]
                end = index + 1

            segments.append(best_segment)
            index = end

        return segments

    def _truncate_with_ellipsis(self, text: str, font_size: int, max_width: int) -> str:
        ellipsis = "..."
        if self._measure_text(text, font_size) <= max_width:
            return text

        truncated = text.rstrip()
        while (
            truncated
            and self._measure_text(f"{truncated}{ellipsis}", font_size) > max_width
        ):
            truncated = truncated[:-1].rstrip()

        return f"{truncated}{ellipsis}" if truncated else ellipsis

    def _measure_text(self, text: str, font_size: int) -> float:
        if not text:
            return 0.0

        font = self._get_font(font_size)
        if font is not None:
            try:
                return float(font.getlength(text))
            except Exception as exc:  # pragma: no cover - defensive logging
                if not self._font_metrics_warning_logged:
                    logger.warning("Unable to measure text width: %s", exc)
                    self._font_metrics_warning_logged = True

        return len(text) * max(font_size * 0.55, 1)

    def _get_font(self, size: int):
        if not self._font_metrics_supported or ImageFont is None:
            return None

        cached = self._font_cache.get(size)
        if cached is not None:
            return cached

        try:
            font = ImageFont.truetype(str(self.font_path), size)
            self._font_cache[size] = font
            return font
        except Exception as exc:  # pragma: no cover - defensive logging
            if not self._font_metrics_warning_logged:
                logger.warning("Unable to load font metric data: %s", exc)
                self._font_metrics_warning_logged = True
            return None

    def _cancel_fact_tasks(self) -> None:
        if self.fact_fetch_task:
            self.fact_fetch_task.cancel()
            self.fact_fetch_task = None
        if self.fact_cycle_task:
            self.fact_cycle_task.cancel()
            self.fact_cycle_task = None
        # Clear facts display when cancelling - write synchronously to avoid task creation issues
        # Clear individual fact line files (workaround for FFmpeg 7.x newline rendering bug)
        try:
            for i in range(self._get_max_fact_lines()):
                self._write_overlay_text_file_sync(
                    self.overlay_dir / f"facts_{i + 1}.txt", ""
                )
        except Exception as e:
            logger.debug(f"Could not clear facts files: {e}")

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
            # Don't log errors - video viewer tracking is optional
            return 0

    def _schedule_song_facts(
        self, track_key: str, artist: str, title: str, album: str | None
    ) -> None:
        if not self.fact_generator:
            return

        self._cancel_fact_tasks()
        self.fact_fetch_task = asyncio.create_task(
            self._fetch_and_cycle_facts(track_key, artist, title, album)
        )

    async def _fetch_and_cycle_facts(
        self, track_key: str, artist: str, title: str, album: str | None
    ) -> None:
        try:
            # Check if anyone is watching - don't make API calls if no viewers
            viewer_count = await self._get_video_viewer_count()
            if viewer_count == 0:
                logger.debug(f"No video viewers, skipping facts for {artist} - {title}")
                return

            logger.debug(f"Fetching facts for {artist} - {title}")
            facts = await self._get_song_facts(track_key, artist, title, album)
            if not facts:
                logger.debug(f"No facts returned for {artist} - {title}")
                return
            logger.info(f"Got {len(facts)} facts for {artist} - {title}")
            if self.current_track_signature != track_key:
                logger.debug("Track changed before starting fact cycle")
                return
            logger.info(f"Creating fact cycle task for {track_key}")
            self.fact_cycle_task = asyncio.create_task(
                self._fact_cycle_loop(track_key, facts)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Error during song fact cycle: {exc}")

    async def _get_song_facts(
        self, track_key: str, artist: str, title: str, album: str | None
    ) -> list[str]:
        if track_key in self.fact_cache:
            return list(self.fact_cache[track_key])

        override = self.fact_repository.get_override(artist, title, album)
        if override is not None:
            self.fact_cache[track_key] = override
            await self.fact_repository.store_cache(track_key, override)
            return list(override)

        cached = self.fact_repository.get_cached(track_key)
        if cached is not None:
            self.fact_cache[track_key] = cached
            return list(cached)

        if self.fact_repository.should_skip_api(artist, title, album):
            await self.fact_repository.store_cache(track_key, [])
            self.fact_cache[track_key] = []
            logger.info(
                "No configured song facts for %s - %s; skipping API",
                artist,
                title,
            )
            return []

        if not self.fact_generator:
            return []

        try:
            facts = await self.fact_generator(artist, title, album)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Error fetching song facts: %s", exc)
            facts = []

        if isinstance(facts, list):
            raw_items = facts
        elif isinstance(facts, tuple):
            raw_items = list(facts)
        else:
            raw_items = [facts]

        cleaned = [
            str(item).strip()
            for item in raw_items
            if isinstance(item, (str, int, float)) and str(item).strip()
        ][:3]

        # Filter out responses where OpenAI doesn't know the song
        # Check for phrases indicating inability to provide facts
        invalid_phrases = [
            "can't verify",
            "can't confirm",
            "please share",
            "send the details",
            "i'll craft",
            "i'll spin",
            "once confirmed",
            "unable to verify",
            "unable to confirm",
            "don't have information",
            "couldn't find",
            "not familiar with",
        ]

        valid_facts = []
        for fact in cleaned:
            fact_lower = fact.lower()
            # Skip if it contains any invalid phrases
            if any(phrase in fact_lower for phrase in invalid_phrases):
                logger.warning(
                    f"Skipping invalid fact response for {artist} - {title}: {fact[:100]}"
                )
                continue
            valid_facts.append(fact)

        # If all facts were invalid, store empty list
        if cleaned and not valid_facts:
            logger.info(
                f"OpenAI couldn't verify song: {artist} - {title}. Storing as skip."
            )
            await self.fact_repository.store_cache(track_key, [])
            self.fact_cache[track_key] = []
            return []

        await self.fact_repository.store_cache(track_key, valid_facts)
        self.fact_cache[track_key] = valid_facts
        if not valid_facts:
            logger.info("No song facts available for %s - %s", artist, title)
        return list(valid_facts)

    async def _fact_cycle_loop(self, track_key: str, facts: list[str]) -> None:
        if not facts:
            logger.debug("No facts to cycle")
            return

        logger.info(f"Starting fact cycle with {len(facts)} facts")
        try:
            logger.debug(
                f"_running={self._running}, current_track_signature={self.current_track_signature}, track_key={track_key}"
            )
            index = 0

            while self._running and self.current_track_signature == track_key:
                fact = facts[index]
                logger.debug(f"Displaying fact: {fact}")

                # Format fact into lines and write to separate files
                # This works around FFmpeg 7.x bug where newlines render as visible glyphs
                lines = self._format_fact_text(fact)
                await self._write_fact_lines(lines)

                if not await self._sleep_with_track_check(25, track_key):
                    # Track changed or stopped - clear facts before exiting
                    await self._clear_fact_lines()
                    break

                # Move to next fact
                index = (index + 1) % len(facts)
        except Exception as e:
            logger.error(f"Error in fact cycle loop: {e}", exc_info=True)
            # Clear facts on error
            await self._clear_fact_lines()
            raise

    async def _sleep_with_track_check(self, seconds: int, track_key: str) -> bool:
        elapsed = 0
        while elapsed < seconds:
            if self.current_track_signature != track_key or not self._running:
                return False
            await asyncio.sleep(1)
            elapsed += 1
        return True

    def _get_max_fact_lines(self) -> int:
        """Get the maximum number of fact lines from config."""
        fonts = self.video_theme_config.get("fonts", {})
        facts_font = fonts.get("facts", {})
        return facts_font.get("max_lines", 5)

    async def _write_fact_lines(self, lines: list[str]) -> None:
        """Write fact lines to separate text files.

        Each line goes to facts_1.txt, facts_2.txt, etc. to work around
        FFmpeg 7.x bug where newline characters render as visible glyphs.
        """
        max_lines = self._get_max_fact_lines()
        for i in range(max_lines):
            filename = self.overlay_dir / f"facts_{i + 1}.txt"
            content = lines[i] if i < len(lines) else ""
            await self._write_text_file(filename, content)

    async def _clear_fact_lines(self) -> None:
        """Clear all fact line text files."""
        max_lines = self._get_max_fact_lines()
        for i in range(max_lines):
            filename = self.overlay_dir / f"facts_{i + 1}.txt"
            await self._write_text_file(filename, "")

    def _build_filter_graph(self, dj_file: str, main_file: str, sub_file: str) -> str:
        fonts = self.video_theme_config.get("fonts", {})

        # Get font style configurations (colours, sizes, shadows etc.)
        dj_font = fonts.get("dj_name", {})
        clock_font = fonts.get("clock", {})
        artist_font = fonts.get("artist", {})
        song_font = fonts.get("song", {})
        facts_font = fonts.get("facts", {})

        # Build drawtext commands using per-element positions from theme config
        draw_commands = [
            # DJ name
            f"drawtext=fontfile={dj_font.get('path', self.font_path)}:textfile={dj_file}:reload=1:"
            f"x={self.dj_name_x}:y={self.dj_name_y}:fontsize={dj_font.get('size', 36)}:fontcolor={dj_font.get('color', 'white')}:box=1:boxcolor={dj_font.get('box_color', '0x00000088')}:"
            f"boxborderw={dj_font.get('box_border', 8)}:shadowcolor={dj_font.get('shadow_color', '0x000000AA')}:shadowx={dj_font.get('shadow_offset', {}).get('x', 3)}:shadowy={dj_font.get('shadow_offset', {}).get('y', 3)}",
            # Clock
            f"drawtext=fontfile={clock_font.get('path', self.font_path)}:text='%{{localtime\\:%H}}\\:%{{localtime\\:%M}}':"
            f"x={self.clock_x}:y={self.clock_y}:fontsize={clock_font.get('size', 40)}:fontcolor={clock_font.get('color', 'white')}:box=1:boxcolor={clock_font.get('box_color', '0x00000055')}:"
            f"boxborderw={clock_font.get('box_border', 8)}:shadowcolor={clock_font.get('shadow_color', '0x000000AA')}:shadowx={clock_font.get('shadow_offset', {}).get('x', 3)}:shadowy={clock_font.get('shadow_offset', {}).get('y', 3)}",
        ]

        # Add separate drawtext filters for each fact line to work around FFmpeg 7.x
        # bug where newline characters in textfile are rendered as visible glyphs
        facts_font_size = facts_font.get("size", 28)
        facts_line_height = facts_font_size + 8  # font size + spacing
        max_fact_lines = self._get_max_fact_lines()

        for i in range(max_fact_lines):
            line_y = self.facts_y_base + (i * facts_line_height)
            draw_commands.append(
                f"drawtext=fontfile={facts_font.get('path', self.font_path)}:textfile={self.overlay_dir}/facts_{i + 1}.txt:reload=1:"
                f"x={self.facts_x}:y={line_y}:fontsize={facts_font_size}:fontcolor={facts_font.get('color', 'white')}:box=1:boxcolor={facts_font.get('box_color', '0x00000066')}:"
                f"boxborderw={facts_font.get('box_border', 6)}:shadowcolor={facts_font.get('shadow_color', '0x000000AA')}:shadowx={facts_font.get('shadow_offset', {}).get('x', 2)}:shadowy={facts_font.get('shadow_offset', {}).get('y', 2)}"
            )

        draw_commands.extend(
            [
                # Artist (main text)
                f"drawtext=fontfile={artist_font.get('path', self.font_path)}:textfile={main_file}:reload=1:"
                f"x={self.artist_x}:y={self.artist_y}:fontsize={self.main_font_size}:fontcolor={artist_font.get('color', 'white')}:box=1:boxcolor={artist_font.get('box_color', '0x00000088')}:"
                f"boxborderw={artist_font.get('box_border', 10)}:shadowcolor={artist_font.get('shadow_color', '0x000000AA')}:shadowx={artist_font.get('shadow_offset', {}).get('x', 3)}:shadowy={artist_font.get('shadow_offset', {}).get('y', 3)}",
                # Song title (sub text)
                f"drawtext=fontfile={song_font.get('path', self.font_path)}:textfile={sub_file}:reload=1:"
                f"x={self.song_x}:y={self.song_y}:fontsize={self.sub_font_size}:fontcolor={song_font.get('color', 'white')}:box=1:boxcolor={song_font.get('box_color', '0x00000066')}:"
                f"boxborderw={song_font.get('box_border', 8)}:shadowcolor={song_font.get('shadow_color', '0x000000AA')}:shadowx={song_font.get('shadow_offset', {}).get('x', 3)}:shadowy={song_font.get('shadow_offset', {}).get('y', 3)}",
            ]
        )

        if self.use_inline_background:
            audio_split = (
                "[a_mix]asplit=3[a_wave][a_bg][aout]"
                if self.waveform_enabled
                else "[a_mix]asplit=2[a_bg][aout]"
            )
            filter_parts = [
                "[0:a]aresample=async=1:first_pts=0,asetpts=N/SR/TB[a_primary]",
                "[2:a]aresample=async=1:first_pts=0,asetpts=N/SR/TB[a_fallback]",
                "[a_primary][a_fallback]amix=inputs=2:duration=longest:dropout_transition=4[a_mix]",
                audio_split,
                "[1:v]format=rgba,split[bg_base][bg_base_fx]",
            ]
            background_filters, background_label = self._build_background_filters(
                "[a_bg]", "[bg_base]", "[bg_base_fx]"
            )
            filter_parts.extend(background_filters)

            if self.waveform_enabled:
                filter_parts.append(
                    f"[a_wave]showwaves=s={self.waveform_width}x{self.waveform_height}:mode={self.waveform_mode}:rate={self.video_fps}:colors={self.waveform_colors},format=rgba[wave_ready]"
                )
                filter_parts.append(
                    f"{background_label}[wave_ready]overlay=x='{self.waveform_x}':y='{self.waveform_y}'[bg_ready]"
                )
            else:
                filter_parts.append(f"{background_label}copy[bg_ready]")
            filter_parts.append("[bg_ready]format=rgba[bg_ready]")
            filter_parts.append(f"[bg_ready]{','.join(draw_commands)}[with_text]")
            if self.artwork_enabled:
                filter_parts.append(f"[3:v]scale={self.artwork_size}:-1[artwork]")
                filter_parts.append(
                    f"[with_text][artwork]overlay={self.artwork_x}:{self.artwork_y}:shortest=1,format=yuv420p[vout]"
                )
            else:
                filter_parts.append("[3:v]nullsink")
                filter_parts.append("[with_text]format=yuv420p[vout]")

            return ";".join(filter_parts)

        filter_parts = [
            "[0:a]aresample=async=1:first_pts=0,asetpts=N/SR/TB[a_primary]",
            "[2:a]aresample=async=1:first_pts=0,asetpts=N/SR/TB[a_fallback]",
            "[a_primary][a_fallback]amix=inputs=2:duration=longest:dropout_transition=4[aout]",
            "[1:v]format=rgba[bg_source]",
        ]

        filter_parts.append(f"[bg_source]{','.join(draw_commands)}[with_text]")
        if self.artwork_enabled:
            filter_parts.append(f"[3:v]scale={self.artwork_size}:-1[artwork]")
            filter_parts.append(
                f"[with_text][artwork]overlay={self.artwork_x}:{self.artwork_y}:shortest=1,format=yuv420p[vout]"
            )
        else:
            filter_parts.append("[3:v]nullsink")
            filter_parts.append("[with_text]format=yuv420p[vout]")

        return ";".join(filter_parts)

    async def _launch_ffmpeg(self, wait_for_background: bool = True) -> None:
        if not self.font_path.exists():
            logger.warning(
                f"Font not found at {self.font_path}, falling back to default"
            )

        # During restarts, background generator is already running
        # Only wait on initial startup
        if wait_for_background:
            await asyncio.sleep(0.5)

        if self.ffmpeg_log_task:
            self.ffmpeg_log_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.ffmpeg_log_task
            self.ffmpeg_log_task = None

        # Make sure no stray HLS writers from previous runs are still alive
        await self._kill_orphan_hls_processes()

        dj_file = str(self.overlay_dir / "dj.txt")
        main_file = str(self.overlay_dir / "main.txt")
        sub_file = str(self.overlay_dir / "sub.txt")

        filter_graph = self._build_filter_graph(dj_file, main_file, sub_file)
        logger.debug("Video filter graph: %s", filter_graph)

        cmd: list[str] = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-thread_queue_size",
            "4096",
            "-fflags",
            "+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-y",
        ]

        cmd.extend(self._build_audio_input_args())
        if self.use_inline_background:
            cmd.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c={self.background_color}:s=1280x720:r={self.video_fps}",
                ]
            )
        else:
            cmd.extend(
                [
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgba",
                    "-s",
                    "1280x720",
                    "-r",
                    str(self.video_fps),
                    "-i",
                    str(self.background_pipe_path),
                ]
            )
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
        )
        cmd.extend(
            [
                "-thread_queue_size",
                "64",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                # Geometry and frame rate are known. Probing a live 1 fps
                # artwork feed otherwise delays all inputs by several seconds.
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-fpsprobesize",
                "0",
                "-r",
                "1",
                "-i",
                str(self.artwork_pipe_path),
                "-filter_complex",
                filter_graph,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-preset",
                self.video_encode_preset,
                "-tune",
                "zerolatency",
                "-threads",
                str(self.video_encode_threads) if self.video_encode_threads else "0",
                "-pix_fmt",
                "yuv420p",
                "-g",
                str(self.hls_keyframe_interval),
                "-keyint_min",
                str(self.hls_keyframe_interval),
                "-sc_threshold",
                "0",
                "-b:v",
                self.video_bitrate,
                "-maxrate",
                self.video_maxrate,  # Add max bitrate to prevent spikes
                "-bufsize",
                self.video_bufsize,  # Buffer size for rate control
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-f",
                "hls",
                "-hls_time",
                f"{self.hls_segment_seconds:.2f}",
                "-hls_list_size",
                str(self.hls_list_size),
                "-hls_flags",
                "delete_segments+append_list+independent_segments+program_date_time",
                "-hls_delete_threshold",
                str(self.hls_delete_threshold),
                "-hls_segment_type",
                "mpegts",
                "-force_key_frames",
                f"expr:gte(t,n_forced*{self.hls_segment_seconds:.2f})",
                "-hls_segment_filename",
                str(self.output_dir / "segment_%03d.ts"),
                "-start_number",
                "0",
                str(self.playlist_path),
            ]
        )

        logger.info("Starting video ffmpeg process")
        self._ffmpeg_recent_lines.clear()

        self.ffmpeg_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        if self.ffmpeg_process.stderr:
            self.ffmpeg_log_task = asyncio.create_task(
                self._log_process_stream(self.ffmpeg_process.stderr, "video")
            )

        if not self.use_inline_background:
            self._open_background_keepalive()
        self._last_output_activity = time.time()
        self._last_output_timestamp = self._get_latest_output_timestamp()

    async def _restart_ffmpeg(self, reason: str | None = None) -> None:
        # Try to acquire lock with timeout to prevent deadlocks
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=30.0)
        except TimeoutError:
            logger.error(
                "Could not acquire lock for ffmpeg restart - lock held too long"
            )
            return

        try:
            if not self._running:
                return

            if reason:
                logger.warning("Restarting video ffmpeg process: %s", reason)
            else:
                logger.warning("Restarting video ffmpeg process")

            await self._stop_ffmpeg_process()
            # Also restart the artwork feeder to ensure clean pipe state
            await self._stop_artwork_feeder()
            self._cleanup_previous_output()

            # Restart artwork feeder first
            await self._ensure_artwork_pipe()
            await self._start_artwork_feeder()

            await self._launch_ffmpeg()
        finally:
            self._lock.release()

    async def _monitor_process(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(5)
                process = self.ffmpeg_process
                if not process:
                    continue

                return_code = process.returncode
                if return_code is None:
                    continue

                # Ignore exits from stale processes that were already replaced/stopped
                # by another restart path (output-health restart, manual stop, etc).
                if process is not self.ffmpeg_process:
                    logger.debug(
                        "Ignoring stale video ffmpeg process exit code %s",
                        return_code,
                    )
                    continue

                signal_name = None
                if return_code < 0:
                    try:
                        signal_name = signal.Signals(-return_code).name
                    except ValueError:
                        signal_name = f"SIG{-return_code}"

                if signal_name:
                    logger.error(
                        "Video ffmpeg process exited with code %s (%s), attempting restart",
                        return_code,
                        signal_name,
                    )
                else:
                    logger.error(
                        "Video ffmpeg process exited with code %s, attempting restart",
                        return_code,
                    )

                if self._ffmpeg_recent_lines:
                    recent = list(self._ffmpeg_recent_lines)[-12:]
                    logger.error(
                        "Recent video ffmpeg stderr lines:\n%s", "\n".join(recent)
                    )

                self.ffmpeg_process = None

                try:
                    await self._restart_ffmpeg(f"ffmpeg exited with code {return_code}")
                except Exception as exc:
                    logger.error(f"Failed to restart video ffmpeg process: {exc}")
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Error monitoring video stream process: {exc}")
            # Don't exit the monitor - try to recover
            if self._running:
                logger.info(
                    "Attempting to continue video process monitoring after error"
                )
                await asyncio.sleep(10)
                # Recursively restart monitoring
                asyncio.create_task(self._monitor_process())

    async def _monitor_output_health(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._output_check_interval)

                latest_timestamp = self._get_latest_output_timestamp()

                # If no output files exist yet, we need to track how long we've been waiting
                if latest_timestamp == 0.0:
                    # Initialize activity time if not set
                    if self._last_output_activity == 0.0:
                        self._last_output_activity = time.time()
                    # Check if we've been waiting too long for first output
                    waiting_for = time.time() - self._last_output_activity
                    if waiting_for >= self.output_stall_seconds:
                        logger.warning(
                            "Video never started producing output after %.1fs (threshold %.1fs) - restarting pipeline",
                            waiting_for,
                            self.output_stall_seconds,
                        )
                        try:
                            await self._restart_full_pipeline(
                                f"no output produced for {waiting_for:.1f}s"
                            )
                        except Exception as exc:
                            logger.error(f"Failed to restart pipeline: {exc}")
                            # Reset activity time to try again after threshold
                            self._last_output_activity = time.time()
                    continue

                if latest_timestamp > self._last_output_timestamp:
                    self._last_output_timestamp = latest_timestamp
                    self._last_output_activity = time.time()
                    continue

                if self._last_output_activity == 0.0:
                    self._last_output_activity = time.time()
                    continue

                stalled_for = time.time() - self._last_output_activity
                if stalled_for >= self.output_stall_seconds:
                    logger.warning(
                        "Video output stalled for %.1fs (threshold %.1fs) - restarting pipeline",
                        stalled_for,
                        self.output_stall_seconds,
                    )
                    try:
                        await self._restart_full_pipeline(
                            f"no new segments for {stalled_for:.1f}s"
                        )
                    except Exception as exc:
                        logger.error(f"Failed to restart pipeline: {exc}")
                        # Reset activity time to try again after threshold
                        self._last_output_activity = time.time()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Error monitoring video output health: {exc}")
            # Don't exit the monitor - try to recover
            if self._running:
                logger.info("Attempting to continue video monitoring after error")
                await asyncio.sleep(10)
                # Recursively restart monitoring
                asyncio.create_task(self._monitor_output_health())

    def _get_latest_output_timestamp(self) -> float:
        latest = 0.0

        try:
            stats = self.playlist_path.stat()
            latest = max(latest, stats.st_mtime)
        except FileNotFoundError:
            pass

        for segment_path in self.output_dir.glob("segment_*.ts"):
            try:
                seg_stats = segment_path.stat()
            except FileNotFoundError:
                continue
            latest = max(latest, seg_stats.st_mtime)

        return latest

    async def _restart_full_pipeline(self, reason: str) -> None:
        if not self._running:
            return

        logger.error("Video output stalled (%s) - restarting pipeline", reason)

        # Try to acquire lock with timeout to prevent deadlocks
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=30.0)
        except TimeoutError:
            logger.error(
                "Could not acquire lock for pipeline restart - lock held too long"
            )
            return

        try:
            self._last_output_activity = time.time()
            self._last_output_timestamp = 0.0

            # Stop all pipeline components before starting fresh
            if not self.use_inline_background:
                await self._stop_background_generator()
            await self._stop_ffmpeg_process()
            await self._stop_artwork_feeder()
            self._cleanup_previous_output()

            # Restart artwork feeder first (it needs time to open the pipe)
            await self._ensure_artwork_pipe()
            await self._start_artwork_feeder()

            if not self.use_inline_background:
                await self._ensure_background_pipe()
                await self._launch_background_generator()

                # Wait for background generator to start producing frames before connecting main FFmpeg
                await asyncio.sleep(0.5)

            await self._launch_ffmpeg()
            logger.info("Video pipeline restarted successfully")
        finally:
            self._lock.release()
