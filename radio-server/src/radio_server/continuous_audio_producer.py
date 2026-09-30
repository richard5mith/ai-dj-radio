import asyncio
import logging
import math
import os
import stat
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audio_utils import artwork_url_for_track, open_fifo_for_writing

logger = logging.getLogger(__name__)


class PCMRelay:
    """Simple TCP relay that rebroadcasts PCM audio frames to connected clients."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._lock = asyncio.Lock()
        self._client_peers: dict[asyncio.StreamWriter, str] = {}
        self._client_slow_writes: dict[asyncio.StreamWriter, int] = {}
        self._client_last_slow_log: dict[asyncio.StreamWriter, float] = {}
        self._max_write_buffer_bytes = 512 * 1024
        self._slow_log_interval_seconds = max(
            1.0, float(os.getenv("PCM_RELAY_SLOW_LOG_INTERVAL_SECONDS", "30.0"))
        )
        self._max_skipped_writes_before_disconnect = max(
            0, int(os.getenv("PCM_RELAY_MAX_SKIPPED_WRITES", "0"))
        )
        self._wait_closed_timeout = 3.0

    async def start(self) -> None:
        if self._server:
            return

        self._server = await asyncio.start_server(
            self._handle_client, host=self.host, port=self.port
        )
        logger.info(
            "PCM relay listening on %s:%s for video pipeline audio",
            self.host,
            self.port,
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        async with self._lock:
            clients = list(self._clients)
            self._clients.clear()
            self._client_peers.clear()
            self._client_slow_writes.clear()
            self._client_last_slow_log.clear()

        for writer in clients:
            writer.close()
            with suppress(Exception):
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=self._wait_closed_timeout
                )

    async def broadcast(self, data: bytes) -> None:
        if not data:
            return

        async with self._lock:
            clients = list(self._clients)

        if not clients:
            return

        stale_clients: set[asyncio.StreamWriter] = set()
        stale_clients_to_close: list[tuple[asyncio.StreamWriter, str]] = []

        for writer in clients:
            if writer.is_closing():
                stale_clients.add(writer)
                continue

            try:
                transport = writer.transport
                if not transport:
                    stale_clients.add(writer)
                    continue

                if transport.get_write_buffer_size() > self._max_write_buffer_bytes:
                    slow_count = self._client_slow_writes.get(writer, 0) + 1
                    self._client_slow_writes[writer] = slow_count
                    now = time.monotonic()
                    last_logged = self._client_last_slow_log.get(writer, 0.0)
                    if (now - last_logged) >= self._slow_log_interval_seconds:
                        peer = self._client_peers.get(writer, "(unknown)")
                        logger.warning(
                            "PCM relay client behind (%s); skipped %s writes (buffer=%s bytes)",
                            peer,
                            slow_count,
                            transport.get_write_buffer_size(),
                        )
                        self._client_last_slow_log[writer] = now
                    if (
                        self._max_skipped_writes_before_disconnect > 0
                        and slow_count >= self._max_skipped_writes_before_disconnect
                    ):
                        peer = self._client_peers.get(writer, "(unknown)")
                        logger.warning(
                            "Disconnecting lagging PCM relay client (%s) after %s skipped writes",
                            peer,
                            slow_count,
                        )
                        stale_clients.add(writer)
                    continue

                self._client_slow_writes[writer] = 0
                writer.write(data)
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.debug(f"PCM relay write failed: {exc}")
                stale_clients.add(writer)

        if stale_clients:
            async with self._lock:
                for writer in stale_clients:
                    self._clients.discard(writer)
                    peer = self._client_peers.pop(writer, "(unknown)")
                    self._client_slow_writes.pop(writer, None)
                    self._client_last_slow_log.pop(writer, None)
                    stale_clients_to_close.append((writer, peer))

        for writer, peer in stale_clients_to_close:
            if not writer.is_closing():
                writer.close()
            asyncio.create_task(self._finalize_writer_close(writer, peer))

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        async with self._lock:
            self._clients.add(writer)
            peer_label = str(peer) if peer else "(unknown)"
            self._client_peers[writer] = peer_label
            self._client_slow_writes[writer] = 0
            self._client_last_slow_log[writer] = 0.0
        logger.info("PCM relay client connected: %s", peer)

        try:
            while not reader.at_eof():
                await reader.read(1024)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug(f"PCM relay client error: {exc}")
        finally:
            async with self._lock:
                self._clients.discard(writer)
                self._client_peers.pop(writer, None)
                self._client_slow_writes.pop(writer, None)
                self._client_last_slow_log.pop(writer, None)
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            logger.info("PCM relay client disconnected: %s", peer)

    async def _finalize_writer_close(
        self, writer: asyncio.StreamWriter, peer: str
    ) -> None:
        try:
            await asyncio.wait_for(
                writer.wait_closed(), timeout=self._wait_closed_timeout
            )
        except TimeoutError:
            logger.debug("Timed out waiting for PCM relay client %s to close", peer)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug(f"Error closing PCM client {peer}: {exc}")


class ContinuousAudioProducer:
    """Continuous audio streaming with proper metadata support."""

    def __init__(self):
        self.stream_process: asyncio.subprocess.Process | None = None
        self.stream_log_task: asyncio.Task | None = None
        self.is_streaming = False
        self.current_metadata = {}
        self.metadata_callback: Callable | None = None
        self.metadata_callback_task: asyncio.Task | None = None
        self.metadata_callback_timeout_seconds = max(
            0.5, float(os.getenv("METADATA_CALLBACK_TIMEOUT_SECONDS", "3.0"))
        )

        # Optional callback fired when a queued audio item finishes streaming.
        # Receives (metadata: dict, actual_duration_seconds: float). The metadata
        # dict carries timeline_id when the item came from a TimelineItem, so the
        # owner can mark the corresponding item completed.
        self.on_item_finished: Callable | None = None

        # Audio queue for seamless playback
        self.audio_queue = asyncio.Queue()
        self.queue_processor_task: asyncio.Task | None = None
        self.silence_filler_task: asyncio.Task | None = None
        self.stream_monitor_task: asyncio.Task | None = None
        self.queue_health_task: asyncio.Task | None = None

        # Playback timing and state management
        self.current_track_start_time: float | None = None
        self.current_track_duration: float | None = None
        self.is_playing_audio = False
        self.playback_finished_event = asyncio.Event()
        self.active_metadata_tasks: list[asyncio.Task] = []
        self._metadata_offsets: list[float] = []
        self.current_file_path: str | None = None
        self._last_queue_activity = time.time()
        self._queue_stall_threshold = 15.0
        self._queue_stall_check_interval = 5.0
        self._last_queue_restart = 0.0
        self._queue_restart_cooldown = 10.0

        # Temp directory for audio processing
        self.temp_dir = Path("/app/temp-audio")
        self.temp_dir.mkdir(exist_ok=True)

        # Local PCM relay for sharing audio with the video pipeline
        self._pcm_relay_host = "127.0.0.1"
        self._pcm_relay_port = 8765
        self.pcm_relay: PCMRelay | None = None

        # Audio-only HLS output configuration
        self.hls_output_dir = Path("/app/audio-output")
        self.hls_output_dir.mkdir(parents=True, exist_ok=True)
        self.base_playlist_path = self.hls_output_dir / "live_base.m3u8"
        self.playlist_path = self.hls_output_dir / "live.m3u8"
        self.hls_segment_seconds = max(
            1.0, float(os.getenv("AUDIO_HLS_SEGMENT_SECONDS", "6"))
        )
        self.hls_target_window_seconds = max(
            30.0, float(os.getenv("AUDIO_HLS_TARGET_WINDOW_SECONDS", "120"))
        )
        env_list_size = os.getenv("AUDIO_HLS_LIST_SIZE")
        if env_list_size:
            self.hls_list_size = max(6, int(env_list_size))
        else:
            self.hls_list_size = max(
                6,
                int(
                    math.ceil(self.hls_target_window_seconds / self.hls_segment_seconds)
                ),
            )
        env_delete_threshold = os.getenv("AUDIO_HLS_DELETE_THRESHOLD")
        if env_delete_threshold:
            self.hls_delete_threshold = max(3, int(env_delete_threshold))
        else:
            self.hls_delete_threshold = max(3, self.hls_list_size // 2)
        self.hls_refresh_task: asyncio.Task | None = None
        self._hls_last_base_playlist: str | None = None
        self._metadata_events: list[dict[str, str]] = []
        self._hls_last_metadata_signature: tuple[str, ...] = ()
        self._metadata_revision = 0
        self.id3_enabled = os.getenv("AUDIO_ID3_METADATA", "0").lower() in {
            "1",
            "true",
            "yes",
        }
        self.id3_pipe_path = self.hls_output_dir / "metadata.fftd"
        self.id3_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.id3_writer_task: asyncio.Task | None = None
        self._id3_pipe_fd: int | None = None
        self.id3_packet_size = max(512, int(os.getenv("AUDIO_ID3_PACKET_SIZE", "4096")))

    def _ensure_worker_task(
        self,
        attr_name: str,
        task_name: str,
        coroutine_factory: Callable[[], Awaitable[Any]],
    ) -> None:
        """Make sure long-running helper tasks stay alive."""

        if not self.is_streaming:
            return

        task = getattr(self, attr_name, None)
        if task and not task.done():
            return

        if task and task.done():
            try:
                task.result()
            except asyncio.CancelledError:
                logger.debug(f"{task_name} task cancelled")
            except Exception as exc:
                logger.error(f"{task_name} task crashed: {exc}")

        new_task = asyncio.create_task(coroutine_factory())
        setattr(self, attr_name, new_task)
        logger.warning(f"Restarted {task_name} task to keep audio flowing")

    def _ensure_background_tasks(self) -> None:
        """Ensure supporting tasks keep running after recoveries."""

        self._ensure_worker_task(
            "queue_processor_task", "audio queue processor", self._process_audio_queue
        )
        self._ensure_worker_task(
            "silence_filler_task", "silence filler", self._fill_with_silence
        )
        self._ensure_worker_task(
            "hls_refresh_task", "HLS playlist refresh", self._refresh_hls_playlist_loop
        )
        self._ensure_worker_task(
            "queue_health_task",
            "audio queue health monitor",
            self._monitor_queue_health,
        )
        if self.id3_enabled:
            self._ensure_worker_task(
                "id3_writer_task", "ID3 metadata writer", self._id3_writer_loop
            )

    def _mark_queue_activity(self) -> None:
        """Record recent progress in queue processing."""
        self._last_queue_activity = time.time()

    def _build_stream_command(self) -> list[str]:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-f",
            "s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-i",
            "pipe:0",  # Raw PCM audio from stdin
        ]

        if self.id3_enabled:
            cmd += [
                "-use_wallclock_as_timestamps",
                "1",
                "-f",
                "data",
                "-raw_packet_size",
                str(self.id3_packet_size),
                "-i",
                str(self.id3_pipe_path),
            ]

        cmd += ["-map", "0:a:0"]
        if self.id3_enabled:
            cmd += ["-map", "1:0", "-c:d", "copy"]

        cmd += [
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
            "-hls_segment_filename",
            str(self.hls_output_dir / "segment_%05d.ts"),
            "-start_number",
            "0",
            str(self.base_playlist_path),
        ]

        return cmd

    async def _log_ffmpeg_output(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if text:
                    logger.warning("audio ffmpeg: %s", text)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug(f"Error reading audio ffmpeg output: {exc}")

    async def _start_stream_process(self) -> bool:
        cmd = self._build_stream_command()

        try:
            self.stream_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"Failed to start audio stream process: {exc}")
            self.stream_process = None
            return False

        if self.stream_log_task:
            self.stream_log_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.stream_log_task
            self.stream_log_task = None

        if self.stream_process.stderr:
            self.stream_log_task = asyncio.create_task(
                self._log_ffmpeg_output(self.stream_process.stderr)
            )

        return True

    async def _stop_stream_process(self) -> None:
        if self.stream_log_task:
            self.stream_log_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.stream_log_task
            self.stream_log_task = None

        process = self.stream_process
        self.stream_process = None

        if not process:
            return

        try:
            if process.stdin:
                process.stdin.close()
                await asyncio.wait_for(process.stdin.wait_closed(), timeout=2)
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            logger.warning("Stream process did not terminate in time, killing")
            process.kill()
            with suppress(Exception):
                await process.wait()
        except Exception as exc:
            logger.error(f"Error stopping stream process: {exc}")

    async def start_streaming(self, metadata_callback: Callable | None = None):
        """Start continuous streaming to audio-only HLS with metadata support."""
        if self.is_streaming:
            logger.warning("Already streaming")
            return

        self.metadata_callback = metadata_callback
        logger.info("Starting continuous audio stream...")

        try:
            self._reset_hls_output()
            if self.id3_enabled:
                self._ensure_id3_pipe()
            if not await self._start_stream_process():
                raise RuntimeError("Failed to start audio stream process")

            self.is_streaming = True
            self._mark_queue_activity()
            self._last_queue_restart = 0.0

            # Start the queue processor and silence filler
            self.queue_processor_task = asyncio.create_task(self._process_audio_queue())
            self.silence_filler_task = asyncio.create_task(self._fill_with_silence())
            self.stream_monitor_task = asyncio.create_task(
                self._monitor_stream_health()
            )
            self.hls_refresh_task = asyncio.create_task(
                self._refresh_hls_playlist_loop()
            )
            if self.id3_enabled:
                self.id3_writer_task = asyncio.create_task(self._id3_writer_loop())

            self._ensure_background_tasks()

            # Start PCM relay for video pipeline consumption
            try:
                self.pcm_relay = PCMRelay(
                    host=self._pcm_relay_host, port=self._pcm_relay_port
                )
                await self.pcm_relay.start()
            except Exception as exc:
                logger.error(f"Failed to start PCM relay: {exc}")
                self.pcm_relay = None

            logger.info("Continuous audio stream started")

        except Exception as e:
            logger.error(f"Error starting continuous stream: {e}")
            if self.pcm_relay:
                await self.pcm_relay.stop()
                self.pcm_relay = None
            await self._stop_stream_process()
            self.is_streaming = False

    async def stop_streaming(self):
        """Stop the continuous stream."""
        logger.info("Stopping continuous audio stream...")

        self.is_streaming = False

        await self._cancel_active_metadata_tasks()
        await self._cancel_metadata_callback_task()
        self.current_file_path = None

        # Stop background tasks
        for task in [
            self.queue_processor_task,
            self.silence_filler_task,
            self.stream_monitor_task,
            self.queue_health_task,
            self.hls_refresh_task,
            self.id3_writer_task,
        ]:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self.hls_refresh_task = None
        self.id3_writer_task = None

        if self.id3_enabled:
            await self._close_id3_pipe()

        if self.pcm_relay:
            await self.pcm_relay.stop()
            self.pcm_relay = None

        await self._stop_stream_process()

        logger.info("Continuous audio stream stopped")

    async def _cancel_metadata_callback_task(self) -> None:
        """Cancel any in-flight metadata callback without blocking audio teardown."""
        task = self.metadata_callback_task
        self.metadata_callback_task = None
        if not task:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug(f"Metadata callback cleanup error: {exc}")

    async def queue_audio_file(
        self,
        file_path: str,
        metadata: dict[str, Any],
        metadata_schedule: list[dict[str, Any]] | None = None,
    ) -> None:
        """Validate and queue one file with its metadata and optional song offsets."""
        await self.queue_audio_files(
            [
                {
                    "file_path": file_path,
                    "metadata": metadata,
                    "metadata_schedule": metadata_schedule,
                }
            ]
        )

    async def queue_audio_files(self, files: list[dict[str, Any]]) -> None:
        """Validate all files before atomically appending a complete timeline item.

        Args:
            files: Audio parts containing file_path, metadata and optional offsets.
        Returns:
            None. Invalid audio raises before any part reaches the queue.
        """
        if not self.is_streaming:
            raise RuntimeError("Cannot queue audio before streaming starts")
        prepared = []
        for part in files:
            file_path = part["file_path"]
            if not Path(file_path).is_file():
                raise FileNotFoundError(file_path)
            duration = await self._get_audio_duration(file_path)
            if duration <= 0:
                raise ValueError(f"Audio file has no playable duration: {file_path}")
            metadata = part["metadata"].copy()
            content_type = metadata.get("content_type")
            if not content_type:
                if (metadata.get("album") or "").lower() == "station jingles":
                    content_type = "JINGLE"
                elif "DJ" in (metadata.get("artist") or "").upper():
                    content_type = "DJ_TALK"
                else:
                    content_type = "MUSIC"
            metadata["content_type"] = str(content_type).upper()
            metadata.setdefault("file_path", file_path)
            prepared.append(
                {
                    "file_path": file_path,
                    "metadata": metadata,
                    "duration": duration,
                    "queued_at": time.time(),
                    "content_type": metadata["content_type"],
                    "metadata_schedule": part.get("metadata_schedule") or [],
                }
            )
        # The producer queue is unbounded. No await between these puts means
        # playback cannot consume an incomplete compound item.
        for part in prepared:
            self.audio_queue.put_nowait(part)

    def get_audio_files_in_use(self) -> set[str]:
        """Return files owned by queued, retrying, or currently playing audio."""
        parts = list(self.audio_queue._queue)
        pending = getattr(self, "_pending_audio_item", None)
        if pending:
            parts.append(pending)
        paths = {part["file_path"] for part in parts}
        if self.current_file_path:
            paths.add(self.current_file_path)
        return paths

    async def _wait_for_audio_boundary(self, metadata: dict[str, Any]) -> None:
        """Keep the stream alive until the scheduled handover may be heard."""
        if not metadata.get("not_before"):
            return
        boundary = datetime.fromisoformat(metadata["not_before"])
        self._waiting_for_boundary = True
        try:
            while self.is_streaming:
                remaining = (boundary - datetime.now(boundary.tzinfo)).total_seconds()
                if remaining <= 0:
                    break
                duration = min(0.1, remaining)
                await self._generate_silence_chunk(duration)
                await asyncio.sleep(duration)
                self._mark_queue_activity()
        finally:
            self._waiting_for_boundary = False

    async def _process_audio_queue(self):
        """Process the audio queue and stream files continuously."""
        logger.info("Starting audio queue processor...")
        # Keep ownership across queue-task restarts as well as playback errors.
        audio_item = getattr(self, "_pending_audio_item", None)

        while self.is_streaming:
            try:
                # Wait for next audio item (with timeout to check if we should stop)
                try:
                    if audio_item is None:
                        audio_item = await asyncio.wait_for(
                            self.audio_queue.get(), timeout=1.0
                        )
                        self._pending_audio_item = audio_item
                except TimeoutError:
                    continue
                self._mark_queue_activity()

                # Wait for previous track to finish if still playing
                if self.is_playing_audio:
                    logger.info(
                        "⏳ WAITING for previous track to finish before starting next..."
                    )
                    await self.playback_finished_event.wait()

                # Check if file still exists before streaming
                if not Path(audio_item["file_path"]).exists():
                    raise FileNotFoundError(audio_item["file_path"])

                await self._wait_for_audio_boundary(audio_item["metadata"])

                # Update metadata BEFORE starting the file
                await self._cancel_active_metadata_tasks()
                self.current_file_path = audio_item["file_path"]
                # Set playback state
                self.is_playing_audio = True
                self.current_track_start_time = time.time()
                self.current_track_duration = audio_item["duration"]
                self.playback_finished_event.clear()

                content_type = audio_item.get("content_type", "UNKNOWN")
                logger.info(
                    f"🎵 NOW STREAMING [{content_type}]: {audio_item['metadata'].get('title', 'Unknown')} - {audio_item['metadata'].get('artist', 'Unknown')} (duration: {audio_item['duration']:.1f}s) | Queue remaining: {self.audio_queue.qsize()}"
                )

                # Stream the audio file as raw PCM to main process
                await self._stream_file_as_pcm(
                    audio_item["file_path"], audio_item["duration"]
                )

                # Capture timing before clearing playback state — callback uses these
                finished_at = time.time()
                play_started_at = self.current_track_start_time
                actual_duration = (
                    finished_at - play_started_at if play_started_at else 0.0
                )

                # Mark playback as finished
                await self._cancel_active_metadata_tasks()
                self.current_file_path = None
                self.is_playing_audio = False
                self.current_track_start_time = None
                self.current_track_duration = None
                self.playback_finished_event.set()

                content_type = audio_item.get("content_type", "UNKNOWN")
                logger.info(
                    f"✅ FINISHED [{content_type}]: {audio_item['metadata'].get('title', 'Unknown')} - {audio_item['metadata'].get('artist', 'Unknown')} | Queue remaining: {self.audio_queue.qsize()}"
                )

                # Fire the finished callback so the owner can mark the source
                # TimelineItem completed and emit completion-side skew metrics.
                # Errors here must not break the audio loop.
                if self.on_item_finished:
                    try:
                        result = self.on_item_finished(
                            audio_item.get("metadata") or {}, actual_duration
                        )
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error(
                            f"on_item_finished callback failed: {e}", exc_info=True
                        )

                # Mark task as done
                self.audio_queue.task_done()
                audio_item = None
                self._pending_audio_item = None
                self._mark_queue_activity()

            except Exception as e:
                logger.error(
                    "Audio failed; retaining the current item for retry: %s", e
                )
                # Reset playback state on error
                await self._cancel_active_metadata_tasks()
                self.current_file_path = None
                self.is_playing_audio = False
                self.playback_finished_event.set()
                await asyncio.sleep(1)

        logger.info("Audio queue processor stopped")

    async def _stream_file_as_pcm(self, file_path: str, duration: float):
        """Convert audio file to PCM and stream to main process."""
        try:
            logger.info(
                f"Streaming file as PCM: {file_path} (duration: {duration:.1f}s)"
            )

            # Convert audio to raw PCM using FFmpeg with loudness normalization
            pcm_cmd = [
                "ffmpeg",
                "-i",
                file_path,
                "-af",
                "loudnorm=I=-16.0:TP=-1.5:LRA=11",  # Normalize to -16 LUFS (broadcast standard)
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-loglevel",
                "error",
                "pipe:1",
            ]

            # Use async subprocess for non-blocking I/O
            pcm_process = await asyncio.create_subprocess_exec(
                *pcm_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            # Check if FFmpeg process started successfully
            await asyncio.sleep(0.1)  # Give FFmpeg a moment to start
            if pcm_process.returncode not in (None, 0):
                # Process already terminated, check for errors
                _, stderr = await pcm_process.communicate()
                if stderr:
                    logger.error(f"❌ FFmpeg PCM conversion failed: {stderr.decode()}")
                else:
                    logger.error(
                        f"❌ FFmpeg PCM process terminated unexpectedly for: {file_path}"
                    )
                raise RuntimeError(f"PCM conversion failed for {file_path}")

            # Stream PCM data to main FFmpeg process with proper real-time control
            start_time = time.time()
            bytes_written = 0
            chunk_size = 8192  # 8KB chunks

            # Calculate streaming rate for real-time playback
            sample_rate = 44100
            channels = 2
            bytes_per_sample = 2
            bytes_per_second = (
                sample_rate * channels * bytes_per_sample
            )  # 176,400 bytes/sec

            try:
                while True:
                    # Use async read to avoid blocking
                    chunk = await asyncio.wait_for(
                        pcm_process.stdout.read(chunk_size), timeout=30
                    )
                    if not chunk:
                        logger.debug("FFmpeg PCM conversion finished - no more data")
                        break

                    broadcast_start = time.perf_counter()
                    await self._broadcast_pcm_chunk(chunk)
                    broadcast_elapsed = time.perf_counter() - broadcast_start
                    if broadcast_elapsed > 0.08:
                        logger.warning(
                            f"PCM relay broadcast slow: {broadcast_elapsed:.3f}s"
                        )

                    if self.stream_process and self.stream_process.stdin:
                        try:
                            # Use async write to avoid blocking the event loop
                            self.stream_process.stdin.write(chunk)
                            await asyncio.wait_for(
                                self.stream_process.stdin.drain(), timeout=10
                            )
                            bytes_written += len(chunk)
                        except BrokenPipeError as e:
                            logger.warning(
                                f"🔧 Stream pipe broken during playback: {e}"
                            )

                            # Wait for restart and retry with exponential backoff
                            logger.info(
                                "⏳ Waiting for stream restart before continuing..."
                            )
                            retry_count = 0
                            base_delay = 0.5
                            while retry_count < 15:  # Wait up to ~16 seconds total
                                delay = min(
                                    base_delay * (2**retry_count), 2.0
                                )  # Cap at 2s
                                await asyncio.sleep(delay)

                                if (
                                    self.stream_process
                                    and self.stream_process.returncode is None
                                ):
                                    logger.info(
                                        "🔄 Stream restarted, resuming playback"
                                    )
                                    # Continue with the same chunk that failed
                                    try:
                                        self.stream_process.stdin.write(chunk)
                                        await asyncio.wait_for(
                                            self.stream_process.stdin.drain(),
                                            timeout=10,
                                        )
                                        bytes_written += len(chunk)
                                        break  # Successfully resumed
                                    except Exception as retry_e:
                                        logger.warning(f"Retry failed: {retry_e}")
                                        retry_count += 1
                                        continue

                                retry_count += 1
                            else:
                                logger.error(
                                    "Stream restart timed out, stopping file playback"
                                )
                                raise RuntimeError("Stream restart timed out")
                        except Exception as e:
                            logger.warning(f"Error writing to stream process: {e}")
                            raise
                    else:
                        raise RuntimeError("Main stream process not available")

                    # Control streaming rate to match real-time playback
                    elapsed_time = time.time() - start_time
                    expected_bytes = int(elapsed_time * bytes_per_second)

                    if bytes_written > expected_bytes:
                        # We're ahead of real-time, need to slow down
                        sleep_time = (bytes_written - expected_bytes) / bytes_per_second
                        if sleep_time > 0:
                            await asyncio.sleep(
                                min(sleep_time, 0.1)
                            )  # Cap sleep to 100ms

                    if bytes_written == len(chunk):
                        # Publish a play only after PCM actually reached the
                        # output. Decode failures must not enter play history.
                        part = getattr(self, "_pending_audio_item", None)
                        if part and part["file_path"] == file_path:
                            await self._update_metadata(part["metadata"])
                            await self._schedule_metadata_updates(
                                part.get("metadata_schedule", []), file_path
                            )

                returncode = await asyncio.wait_for(pcm_process.wait(), timeout=10)
                if returncode != 0 or bytes_written == 0:
                    raise RuntimeError(f"PCM conversion failed for {file_path}")
                # Log completion stats
                elapsed_time = time.time() - start_time
                expected_duration = bytes_written / bytes_per_second
                logger.info(
                    f"✅ PCM streaming completed: {bytes_written} bytes in {elapsed_time:.1f}s (expected: {expected_duration:.1f}s)"
                )

            except Exception as e:
                logger.error(f"Error streaming PCM data: {e}")
                elapsed_time = time.time() - start_time
                logger.error(
                    f"Failed after {elapsed_time:.1f}s, {bytes_written} bytes written"
                )
                raise
            finally:
                # Clean up PCM process
                if pcm_process:
                    try:
                        pcm_process.terminate()
                        await asyncio.wait_for(pcm_process.wait(), timeout=5)
                        logger.debug("PCM process terminated successfully")
                    except TimeoutError:
                        logger.warning("PCM process cleanup timed out, killing")
                        pcm_process.kill()
                        await asyncio.wait_for(pcm_process.wait(), timeout=5)
                    except ProcessLookupError:
                        # Process already exited - this is normal
                        logger.debug("PCM process already exited")
                    except Exception as e:
                        logger.warning(
                            f"Error cleaning up PCM process: {type(e).__name__}: {e}"
                        )
                        try:
                            pcm_process.kill()
                            await asyncio.wait_for(pcm_process.wait(), timeout=5)
                        except Exception:
                            pass

        except Exception as e:
            logger.error(f"Error streaming file as PCM: {e}")
            raise

    async def _fill_with_silence(self):
        """Fill gaps with silence to maintain continuous stream."""
        logger.info("Starting silence filler...")
        silence_interval = 0.5

        while self.is_streaming:
            try:
                # Only generate silence if queue is empty AND nothing is currently playing
                if (
                    self.audio_queue.empty()
                    and not self.is_playing_audio
                    and not getattr(self, "_waiting_for_boundary", False)
                ):
                    logger.debug("Queue empty and nothing playing, generating silence")
                    await self._generate_silence_chunk(silence_interval)
                    await asyncio.sleep(silence_interval)
                    continue

                await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"Error in silence filler: {e}")
                await asyncio.sleep(1)

    async def _monitor_stream_health(self):
        """Monitor the health of the main stream process and restart if needed."""
        logger.info("Starting stream health monitor...")

        while self.is_streaming:
            try:
                self._ensure_background_tasks()

                await asyncio.sleep(5.0)  # Check every 5 seconds

                if self.stream_process:
                    # Check if the process is still alive
                    poll_result = self.stream_process.returncode

                    if poll_result is not None:
                        # Process has died - classify the error type
                        error_type = "unknown"
                        if poll_result == 224:
                            error_type = "broken_pipe"
                        elif poll_result == -32:
                            error_type = "pipe_error"
                        elif poll_result == 1:
                            error_type = "general_error"

                        logger.error(
                            f"📡 Main stream process died with exit code: {poll_result} (type: {error_type})"
                        )

                        if poll_result in [224, -32]:
                            logger.info(
                                "🔧 Detected HLS encoder pipe issue - restarting output process"
                            )
                            await asyncio.sleep(1)

                        # Restart the stream process
                        await self._restart_stream_process()

                else:
                    logger.warning("Stream process is None - attempting restart")
                    await self._restart_stream_process()

                self._ensure_background_tasks()

            except Exception as e:
                logger.error(f"Error in stream health monitor: {e}")
                await asyncio.sleep(5)

    async def _monitor_queue_health(self) -> None:
        """Detect and recover from audio queue stalls."""
        logger.info("Starting audio queue health monitor...")

        while self.is_streaming:
            try:
                await asyncio.sleep(self._queue_stall_check_interval)

                if not self.is_streaming:
                    break

                queue_size = self.audio_queue.qsize()
                if queue_size == 0 or self.is_playing_audio:
                    continue

                stalled_for = time.time() - self._last_queue_activity
                if stalled_for < self._queue_stall_threshold:
                    continue

                now = time.time()
                if now - self._last_queue_restart < self._queue_restart_cooldown:
                    continue

                logger.warning(
                    "⚠️ Audio queue stalled for %.1fs with %s items - restarting queue processor",
                    stalled_for,
                    queue_size,
                )
                self._last_queue_restart = now
                await self._restart_queue_processor(
                    f"queue stalled for {stalled_for:.1f}s"
                )
            except Exception as e:
                logger.error(f"Error in queue health monitor: {e}")
                await asyncio.sleep(2)

    async def _restart_queue_processor(self, reason: str) -> None:
        """Restart the audio queue processor task."""
        task = self.queue_processor_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(f"Audio queue processor cleanup failed: {exc}")

        self.is_playing_audio = False
        self.playback_finished_event.set()
        self._mark_queue_activity()
        self.queue_processor_task = asyncio.create_task(self._process_audio_queue())
        logger.warning("Restarted audio queue processor (%s)", reason)

    async def _restart_stream_process(self):
        """Restart the main stream process."""
        logger.info("🔄 RESTARTING main stream process...")

        try:
            # Clean up old process
            await self._stop_stream_process()
            await self._close_id3_pipe()

            self._reset_hls_output()
            self._ensure_id3_pipe()
            if not await self._start_stream_process():
                raise RuntimeError("Failed to restart audio stream process")

            logger.info("✅ Main stream process restarted successfully")

            # If we were in the middle of playing something, resume it
            if (
                self.is_playing_audio
                and hasattr(self, "current_file_path")
                and self.current_file_path
            ):
                logger.info("🔄 Resuming playback after stream restart")
                # Clear the playing flag so the queue processor can resume
                self.is_playing_audio = False
                self.playback_finished_event.set()

            self._ensure_background_tasks()

        except Exception as e:
            logger.error(f"Failed to restart stream process: {e}")
            self.stream_process = None

    async def _generate_silence_chunk(self, duration_seconds: float = 0.5):
        """Generate a small chunk of silence."""
        try:
            # Generate a short chunk of silence as raw PCM to keep realtime pacing
            silence_samples = int(
                44100 * 2 * 2 * duration_seconds
            )  # 44.1kHz * 2 channels * 2 bytes per sample
            silence_chunk = b"\x00" * silence_samples

            if self.stream_process and self.stream_process.stdin:
                self.stream_process.stdin.write(silence_chunk)
                await (
                    self.stream_process.stdin.drain()
                )  # Use async drain instead of flush
                logger.debug(
                    "Generated %.2fs silence chunk",
                    duration_seconds,
                )

            await self._broadcast_pcm_chunk(silence_chunk)

        except Exception as e:
            logger.debug(f"Error generating silence: {e}")

    async def _broadcast_pcm_chunk(self, chunk: bytes) -> None:
        """Send PCM data to the relay feeding the video pipeline."""
        if not chunk or not self.pcm_relay:
            return

        try:
            await self.pcm_relay.broadcast(chunk)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug(f"PCM relay broadcast error: {exc}")

    async def _update_metadata(self, metadata: dict[str, Any]):
        """Update stream metadata."""
        try:
            metadata_payload = metadata.copy()
            self._metadata_revision += 1
            metadata_payload["metadata_revision"] = self._metadata_revision

            artwork_url = self._artwork_url_for(metadata_payload)
            if artwork_url:
                metadata_payload["artwork_url"] = self._append_query_param(
                    artwork_url,
                    "v",
                    str(self._metadata_revision),
                )

            # Songs inside a crossfaded mix arrive as scheduled metadata updates,
            # so this stamps when the current song started, not just the file.
            metadata_payload["started_at"] = datetime.now(UTC).isoformat()

            self.current_metadata = metadata_payload
            self._record_metadata_event(metadata_payload)
            if self.id3_enabled:
                self._enqueue_id3_metadata(metadata_payload)

            self._dispatch_metadata_callback(metadata_payload)
            logger.debug(
                f"Updated metadata: {metadata_payload.get('artist', 'Unknown')} - {metadata_payload.get('title', 'Unknown')}"
            )

        except Exception as e:
            logger.error(f"Error updating metadata: {e}")

    def _dispatch_metadata_callback(self, metadata: dict[str, Any]) -> None:
        """Run metadata callbacks in the background so overlay work can't stall audio."""
        if not self.metadata_callback:
            return

        task = self.metadata_callback_task
        if task and not task.done():
            task.cancel()

        callback_metadata = metadata.copy()
        self.metadata_callback_task = asyncio.create_task(
            self._run_metadata_callback(callback_metadata)
        )

    async def _run_metadata_callback(self, metadata: dict[str, Any]) -> None:
        callback = self.metadata_callback
        if not callback:
            return

        try:
            await asyncio.wait_for(
                callback(metadata), timeout=self.metadata_callback_timeout_seconds
            )
        except TimeoutError:
            logger.warning(
                "Metadata callback timed out after %.1fs; continuing audio playback",
                self.metadata_callback_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Metadata callback failed: {exc}")

    async def _cancel_active_metadata_tasks(self):
        """Cancel any scheduled metadata update tasks."""
        if not self.active_metadata_tasks:
            return

        tasks = self.active_metadata_tasks.copy()
        self.active_metadata_tasks.clear()
        self._metadata_offsets.clear()

        for task in tasks:
            task.cancel()

        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug(f"Metadata task cleanup error: {exc}")

    async def _schedule_metadata_updates(
        self,
        schedule: list[dict[str, Any]],
        file_path: str,
    ) -> None:
        """Schedule future metadata updates tied to the active track."""
        if not schedule:
            return

        for entry in schedule:
            metadata_update = entry.get("metadata")
            if not metadata_update:
                continue

            offset_raw = entry.get("offset", 0.0)
            try:
                offset_seconds = max(0.0, float(offset_raw))
            except (TypeError, ValueError):
                offset_seconds = 0.0

            task = asyncio.create_task(
                self._run_metadata_update_after_delay(
                    offset_seconds, metadata_update, file_path
                )
            )
            self.active_metadata_tasks.append(task)
            self._metadata_offsets.append(offset_seconds)

    async def _run_metadata_update_after_delay(
        self,
        delay_seconds: float,
        metadata: dict[str, Any],
        file_path: str,
    ) -> None:
        """Apply a metadata update after a delay if the same track is still active."""
        try:
            await asyncio.sleep(delay_seconds)

            if not self.is_streaming:
                return

            if self.current_file_path != file_path:
                return

            await self._update_metadata(metadata)

        except asyncio.CancelledError:
            logger.debug("Scheduled metadata update cancelled")
            raise
        except Exception as exc:
            logger.error(f"Scheduled metadata update failed: {exc}")

    def get_public_path(self) -> str:
        """Public path for the audio HLS playlist."""
        return "/audio/live.m3u8"

    def _reset_hls_output(self) -> None:
        """Clear HLS output directory for a fresh playlist."""
        self.hls_output_dir.mkdir(parents=True, exist_ok=True)
        for file_path in self.hls_output_dir.glob("segment_*.ts"):
            with suppress(Exception):
                file_path.unlink()
        with suppress(Exception):
            self.base_playlist_path.unlink()
        with suppress(Exception):
            self.playlist_path.unlink()
        self._hls_last_base_playlist = None
        self._hls_last_metadata_signature = ()

    def _record_metadata_event(self, metadata: dict[str, Any]) -> None:
        """Store metadata updates for HLS playlist tags."""
        title = str(metadata.get("title") or "Unknown Title")
        artist = str(metadata.get("artist") or "Unknown Artist")
        album = str(metadata.get("album") or "")
        event_id = f"track-{int(time.time() * 1000)}"
        artwork_url = self._artwork_url_for(metadata)
        if artwork_url:
            artwork_url = self._append_query_param(artwork_url, "t", event_id)
        self._metadata_events.append(
            {
                "id": event_id,
                "start": datetime.now(UTC).isoformat(),
                "title": title,
                "artist": artist,
                "album": album,
                "artwork_url": artwork_url,
            }
        )
        if len(self._metadata_events) > 20:
            self._metadata_events = self._metadata_events[-20:]

    async def _refresh_hls_playlist_loop(self) -> None:
        """Continuously refresh the audio playlist with timed metadata tags."""
        while self.is_streaming:
            try:
                await self._refresh_hls_playlist()
            except Exception as exc:
                logger.debug(f"HLS playlist refresh error: {exc}")
            await asyncio.sleep(2.0)

    async def _refresh_hls_playlist(self) -> None:
        if not self.base_playlist_path.exists():
            return

        base_content = self.base_playlist_path.read_text()
        metadata_signature = tuple(event["id"] for event in self._metadata_events)

        if (
            base_content == self._hls_last_base_playlist
            and metadata_signature == self._hls_last_metadata_signature
        ):
            return

        lines = base_content.splitlines()
        if not lines:
            return

        header_lines: list[str] = []
        body_lines: list[str] = []
        inserted = False
        for index, line in enumerate(lines):
            if index == 0:
                header_lines.append(line)
                continue
            if (
                line.startswith("#EXTINF")
                or line.startswith("#EXT-X-PROGRAM-DATE-TIME")
            ) and not inserted:
                body_lines = lines[index:]
                inserted = True
                break
            if line.startswith("#EXT-X-DATERANGE"):
                continue
            header_lines.append(line)

        if not inserted:
            body_lines = []

        metadata_lines = self._build_hls_metadata_lines()
        extinf_title = self._build_hls_extinf_title()
        if extinf_title:
            body_lines = [
                self._rewrite_extinf_line(line, extinf_title) for line in body_lines
            ]
        combined = header_lines + metadata_lines + body_lines
        self.playlist_path.write_text("\n".join(combined) + "\n")
        self._hls_last_base_playlist = base_content
        self._hls_last_metadata_signature = metadata_signature

    def _build_hls_extinf_title(self) -> str:
        title = str(self.current_metadata.get("title") or "").strip()
        artist = str(self.current_metadata.get("artist") or "").strip()
        if title and artist:
            return f"{artist} - {title}"
        return title or artist

    def _rewrite_extinf_line(self, line: str, title: str) -> str:
        if not line.startswith("#EXTINF:"):
            return line
        if not title:
            return line
        if "," in line:
            duration = line.split(",", 1)[0]
            return f"{duration},{title}"
        return f"{line},{title}"

    def _build_hls_metadata_lines(self) -> list[str]:
        if not self._metadata_events:
            return []

        lines: list[str] = []
        for event in self._metadata_events:
            attributes = [
                f'ID="{self._escape_hls_attribute(event["id"])}"',
                'CLASS="now-playing"',
                f'START-DATE="{event["start"]}"',
                "END-ON-NEXT=YES",
                f'X-TITLE="{self._escape_hls_attribute(event["title"])}"',
                f'X-ARTIST="{self._escape_hls_attribute(event["artist"])}"',
            ]
            if event.get("album"):
                attributes.append(
                    f'X-ALBUM="{self._escape_hls_attribute(event["album"])}"'
                )
            if event.get("artwork_url"):
                artwork = self._escape_hls_attribute(event["artwork_url"])
                attributes.append(f'X-IMAGE="{artwork}"')
                attributes.append(f'X-ARTWORK-URL="{artwork}"')
            lines.append("#EXT-X-DATERANGE:" + ",".join(attributes))

        return lines

    def _escape_hls_attribute(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _append_query_param(self, url: str, key: str, value: str) -> str:
        if not url:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{key}={value}"

    def _artwork_url_for(self, metadata: dict[str, Any]) -> str:
        """Per-track artwork URL for a metadata payload.

        Honors an explicitly-set artwork_url if present, otherwise defaults to
        the track's content-addressed artwork file (or the shared placeholder
        for tracks with no file_path). Using a stable per-track URL means a
        listener sitting behind the live edge loads the art for the track they
        are hearing, not the one being encoded.

        Args:
            metadata: Track metadata payload.

        Returns:
            Public artwork URL under /artwork/.
        """
        explicit = metadata.get("artwork_url")
        if explicit:
            return str(explicit).strip()
        return artwork_url_for_track(metadata.get("file_path"))

    def _ensure_id3_pipe(self) -> None:
        try:
            if self.id3_pipe_path.exists():
                mode = self.id3_pipe_path.stat().st_mode
                if not stat.S_ISFIFO(mode):
                    self.id3_pipe_path.unlink()
            if not self.id3_pipe_path.exists():
                os.mkfifo(self.id3_pipe_path, 0o666)
        except Exception as exc:
            logger.warning(f"Unable to prepare ID3 metadata pipe: {exc}")

    def _open_id3_pipe_for_write(self) -> bool:
        if self._id3_pipe_fd is not None:
            return True
        try:
            self._id3_pipe_fd = open_fifo_for_writing(self.id3_pipe_path)
        except OSError as exc:
            logger.debug(f"Failed to open ID3 pipe: {exc}")
            return False
        return self._id3_pipe_fd is not None

    async def _close_id3_pipe(self) -> None:
        if self._id3_pipe_fd is None:
            return
        try:
            os.close(self._id3_pipe_fd)
        except Exception as exc:
            logger.debug(f"Failed to close ID3 pipe: {exc}")
        self._id3_pipe_fd = None

    async def _id3_writer_loop(self) -> None:
        while self.is_streaming:
            if not self._open_id3_pipe_for_write():
                await asyncio.sleep(0.5)
                continue

            try:
                payload = await self.id3_queue.get()
            except asyncio.CancelledError:
                break

            if payload is None:
                break

            try:
                if self._id3_pipe_fd is not None:
                    await asyncio.to_thread(os.write, self._id3_pipe_fd, payload)
            except BrokenPipeError:
                await self._close_id3_pipe()
            except Exception as exc:
                logger.debug(f"Failed to write ID3 metadata: {exc}")

        await self._close_id3_pipe()

    def _enqueue_id3_metadata(self, metadata: dict[str, Any]) -> None:
        if not self.is_streaming:
            return

        payload = self._build_id3_tag(metadata)
        if not payload:
            return

        if len(payload) > self.id3_packet_size:
            logger.debug(
                "ID3 metadata payload too large (%s bytes > %s); skipping",
                len(payload),
                self.id3_packet_size,
            )
            return

        padded = payload.ljust(self.id3_packet_size, b"\x00")
        try:
            self.id3_queue.put_nowait(padded)
        except asyncio.QueueFull:
            logger.debug("ID3 metadata queue full; dropping metadata update")

    def _syncsafe(self, value: int) -> bytes:
        return bytes(
            [
                (value >> 21) & 0x7F,
                (value >> 14) & 0x7F,
                (value >> 7) & 0x7F,
                value & 0x7F,
            ]
        )

    def _build_id3_tag(self, metadata: dict[str, Any]) -> bytes:
        title = str(metadata.get("title") or "").strip()
        artist = str(metadata.get("artist") or "").strip()
        album = str(metadata.get("album") or "").strip()
        artwork_url = self._artwork_url_for(metadata)
        if artwork_url:
            cache_stamp = int(time.time() * 1000)
            artwork_url = self._append_query_param(artwork_url, "t", str(cache_stamp))

        frames: list[bytes] = []

        def add_text_frame(frame_id: str, value: str) -> None:
            if not value:
                return
            data = b"\x03" + value.encode("utf-8")
            frames.append(
                frame_id.encode("ascii")
                + self._syncsafe(len(data))
                + b"\x00\x00"
                + data
            )

        def add_txxx_frame(description: str, value: str) -> None:
            if not value:
                return
            data = (
                b"\x03" + description.encode("utf-8") + b"\x00" + value.encode("utf-8")
            )
            frames.append(b"TXXX" + self._syncsafe(len(data)) + b"\x00\x00" + data)

        add_text_frame("TIT2", title)
        add_text_frame("TPE1", artist)
        add_text_frame("TALB", album)

        stream_title = " - ".join([part for part in [artist, title] if part])
        add_txxx_frame("StreamTitle", stream_title)
        add_txxx_frame("ARTWORK_URL", artwork_url)

        if not frames:
            return b""

        tag_size = sum(len(frame) for frame in frames)
        header = b"ID3" + b"\x04\x00" + b"\x00" + self._syncsafe(tag_size)
        return header + b"".join(frames)

    def get_queue_size(self) -> int:
        """Get the current queue size."""
        return self.audio_queue.qsize()

    def get_pcm_audio_url(self) -> str:
        """Endpoint the video pipeline should connect to for PCM audio."""
        return f"tcp://{self._pcm_relay_host}:{self._pcm_relay_port}"

    def get_current_metadata(self) -> dict[str, Any]:
        """Get the current metadata."""
        return self.current_metadata.copy()

    def get_on_air_remaining(self) -> float:
        """Get how long the audio currently on air has left.

        Inside a crossfaded mix the file keeps playing while the metadata moves
        on to the next song, so the next scheduled metadata offset is what ends
        the current song. Otherwise the file's own remaining time does.

        Returns:
            Seconds until the audio on air changes, or 0.0 when idle.
        """
        if not self.is_playing_audio or not self.current_track_start_time:
            return 0.0

        elapsed = time.time() - self.current_track_start_time
        upcoming = [offset for offset in self._metadata_offsets if offset > elapsed]
        if upcoming:
            return min(upcoming) - elapsed
        return max(0.0, (self.current_track_duration or 0.0) - elapsed)

    def is_queue_empty(self) -> bool:
        """Check if the audio queue is empty."""
        return self.audio_queue.empty()

    async def _get_audio_duration(self, file_path: str) -> float:
        """Get the duration of an audio file using ffprobe."""
        from .audio_utils import get_audio_duration

        duration = await get_audio_duration(file_path)
        return duration if duration > 0 else 180.0  # Default 3 minutes

    def get_current_playback_info(self) -> dict[str, Any]:
        """Get information about current playback state."""
        if not self.is_playing_audio or not self.current_track_start_time:
            return {
                "is_playing": False,
                "elapsed_time": 0,
                "remaining_time": 0,
                "total_duration": 0,
            }

        elapsed = time.time() - self.current_track_start_time
        remaining = (
            max(0, self.current_track_duration - elapsed)
            if self.current_track_duration
            else 0
        )

        return {
            "is_playing": True,
            "elapsed_time": elapsed,
            "remaining_time": remaining,
            "total_duration": self.current_track_duration or 0,
        }
