"""Timeline API server — REST endpoints for viewing the timeline and schedule."""

import asyncio
import logging
import math
import mimetypes
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response, StreamingResponse
from starlette.types import Scope

from .audio_utils import artwork_url_for_track

mime_types_initialized = False

# How much already-played audio to keep in the upcoming list. Listeners sit
# a buffer behind the encoder, so the row they are hearing may already be
# finished as far as the server is concerned.
ROW_LOOKBEHIND_SECONDS = 90


def _ensure_mime_types() -> None:
    global mime_types_initialized
    if mime_types_initialized:
        return
    mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
    mimetypes.add_type("video/mp2t", ".ts")
    mime_types_initialized = True


def _expand_item_rows(item: Any) -> list[dict[str, Any]]:
    """Expand a timeline item into one row per thing a listener will hear.

    Song blocks become one row per song, so the list can show which song of a
    block is on air rather than lumping the whole block into a single entry.
    Everything else — DJ talk, jingles, transitions — is a single row.

    Args:
        item: The timeline item to expand.

    Returns:
        Rows in playback order, each describing one audible element.
    """
    base = {
        "timeline_id": item.timeline_id,
        "item_type": item.item_type,
        "status": item.status,
    }

    songs = getattr(item.content, "songs", None)
    if songs:
        # Songs with no measured duration share out the block's estimate.
        fallback = item.estimated_duration / len(songs)
        last = len(songs) - 1
        return [
            {
                **base,
                "title": song.title,
                "artist": song.artist,
                "duration": float(song.duration or fallback),
                "dj_intro": getattr(item.content, "intro_style", None)
                if index == 0
                else None,
                "dj_outro": getattr(item.content, "outro_style", None)
                if index == last
                else None,
            }
            for index, song in enumerate(songs)
        ]

    # talk_type is the config section every segment shares; prompt_style is the
    # one that says what this segment actually is (trivia, sponsor, station_id).
    if getattr(item.content, "talk_type", None):
        title = "DJ talk"
        style = getattr(item.content, "prompt_style", None)
    else:
        title = getattr(item.content, "name", None) or item.item_type.replace("_", " ")
        style = None

    return [
        {
            **base,
            "title": title,
            "artist": "",
            "style": style,
            "duration": item.estimated_duration,
        }
    ]


def _on_air_row_index(rows: list[dict[str, Any]], on_air: dict[str, Any]) -> int:
    """Find which row of the on-air item is the audio currently streaming.

    Crossfaded blocks play as one file that republishes its metadata at each
    song boundary, so the live title and artist pinpoint the song on air.

    Args:
        rows: Rows expanded from the on-air timeline item.
        on_air: Metadata the producer published for the audio it is streaming.

    Returns:
        Index of the matching row, or 0 when the track cannot be matched.
    """
    title = str(on_air.get("title") or "").casefold()
    artist = str(on_air.get("artist") or "").casefold()
    for index, row in enumerate(rows):
        if row["title"].casefold() == title and row["artist"].casefold() == artist:
            return index
    return 0


def _on_air_elapsed(on_air: dict[str, Any], now: datetime) -> float:
    """Get how long the on-air audio has been playing.

    Args:
        on_air: Metadata the producer published for the audio it is
            streaming, including when that audio started.
        now: Current time.

    Returns:
        Seconds elapsed, or 0.0 when the producer has no start time.
    """
    started_at = on_air.get("started_at")
    if started_at:
        with suppress(ValueError, TypeError):
            started = datetime.fromisoformat(started_at)
            return max(0.0, (now - started).total_seconds())
    return 0.0


def _project_row_times(
    rows: list[dict[str, Any]],
    on_air_index: int,
    now: datetime,
    on_air_elapsed: float,
    on_air_remaining: float,
) -> list[datetime]:
    """Stamp each row with when it starts and ends, in place.

    The on-air row is bounded by what the producer reports rather than by the
    song's tagged duration, which is wrong whenever DJ talk is mixed into the
    file or songs overlap in a crossfade. Rows before it are laid out backwards
    from its start, rows after it forwards from its end. Re-anchoring on every
    request keeps the projection from accumulating the drift that the scheduled
    times suffer from.

    Args:
        rows: Rows in playback order.
        on_air_index: Position of the row that is on air.
        now: Current time.
        on_air_elapsed: Seconds of the on-air row already played.
        on_air_remaining: Seconds the on-air row has left.

    Returns:
        The end time of each row, in the same order.
    """
    on_air_start = now - timedelta(seconds=on_air_elapsed)

    cursor = on_air_start
    for row in reversed(rows[:on_air_index]):
        row["projected_end"] = cursor.isoformat()
        cursor -= timedelta(seconds=row["duration"])
        row["projected_start"] = cursor.isoformat()

    cursor = on_air_start
    for index, row in enumerate(rows[on_air_index:]):
        row["projected_start"] = cursor.isoformat()
        cursor = (
            now + timedelta(seconds=on_air_remaining)
            if index == 0
            else cursor + timedelta(seconds=row["duration"])
        )
        row["projected_end"] = cursor.isoformat()

    return [datetime.fromisoformat(row["projected_end"]) for row in rows]


class NoCacheStaticFiles(StaticFiles):
    """Serve static files with caching disabled to keep HLS live."""

    async def get_response(
        self, path: str, scope: Scope
    ) -> Response:  # pragma: no cover - FastAPI runtime
        response = await super().get_response(path, scope)
        if response.status_code < 400:
            headers = response.headers
            headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            headers["Pragma"] = "no-cache"
            headers["Expires"] = "0"
        return response


logger = logging.getLogger(__name__)


class TrackingStaticFiles(NoCacheStaticFiles):
    """Static files with optional per-request tracking hook."""

    def __init__(
        self,
        *args,
        on_request: Callable[[Scope, str], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._on_request = on_request

    async def get_response(
        self, path: str, scope: Scope
    ) -> Response:  # pragma: no cover - FastAPI runtime
        response = await super().get_response(path, scope)
        if response.status_code < 400 and self._on_request:
            try:
                self._on_request(scope, path)
            except Exception as exc:
                logger.debug(f"Listener tracking failed: {exc}")
        return response


class TimelineAPIServer:
    """API server for timeline and schedule information"""

    def __init__(self, radio_station):
        _ensure_mime_types()
        self.radio_station = radio_station
        self.app = FastAPI(title="Radio Timeline API", version="1.0.0")
        self.server = None

        # Configure CORS
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],  # In production, specify exact origins
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        self._mount_video_stream()
        self._mount_audio_stream()
        self._mount_artwork_stream()
        self._setup_routes()

    def _mount_video_stream(self) -> None:
        """Expose the video stream output directory via FastAPI static files."""
        video_stream = getattr(self.radio_station, "video_stream", None)
        if not video_stream:
            return

        output_dir = getattr(video_stream, "output_dir", None)
        if not output_dir:
            return

        try:
            # Ensure directory exists so StaticFiles does not error on startup
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)

            self.app.mount(
                "/video",
                NoCacheStaticFiles(directory=str(output_path), html=False),
                name="video",
            )
            logger.info("Video stream static mount available at /video")
        except Exception as exc:
            logger.error(f"Failed to mount video stream static directory: {exc}")

    def _mount_audio_stream(self) -> None:
        """Expose the audio stream output directory via FastAPI static files."""
        audio_stream = getattr(self.radio_station, "continuous_audio", None)
        if not audio_stream:
            return

        output_dir = getattr(audio_stream, "hls_output_dir", None)
        if not output_dir:
            return

        try:
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)

            self.app.mount(
                "/audio",
                TrackingStaticFiles(
                    directory=str(output_path),
                    html=False,
                    on_request=self._track_audio_request,
                ),
                name="audio",
            )
            logger.info("Audio stream static mount available at /audio")
        except Exception as exc:
            logger.error(f"Failed to mount audio stream static directory: {exc}")

    def _mount_artwork_stream(self) -> None:
        """Expose current artwork for audio clients."""
        try:
            artwork_dir = Path("/app/temp-audio/artwork")
            artwork_dir.mkdir(parents=True, exist_ok=True)
            self.app.mount(
                "/artwork",
                NoCacheStaticFiles(directory=str(artwork_dir), html=False),
                name="artwork",
            )
            logger.info("Artwork static mount available at /artwork")
        except Exception as exc:
            logger.error(f"Failed to mount artwork static directory: {exc}")

    def _track_audio_request(self, scope: Scope, path: str) -> None:
        if not self._should_track_audio_request(scope, path):
            return

        client_ip = self._get_client_ip(scope)
        tracker = getattr(self.radio_station, "track_direct_audio_listener", None)
        if tracker:
            tracker(client_ip)

    def _should_track_audio_request(self, scope: Scope, path: str) -> bool:
        if not path or not (path == "live.m3u8" or path.endswith(".ts")):
            return False

        proxy_source = self._get_header(scope, "x-proxy-source")
        return not (proxy_source and proxy_source.lower() == "web-interface")

    def _get_client_ip(self, scope: Scope) -> str | None:
        forwarded_for = self._get_header(scope, "x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip() or None

        client = scope.get("client")
        if client and len(client) >= 1:
            return client[0]
        return None

    def _get_header(self, scope: Scope, header_name: str) -> str | None:
        headers = scope.get("headers") or []
        target = header_name.lower().encode()
        for name, value in headers:
            if name.lower() == target:
                return value.decode("utf-8", "ignore")
        return None

    def _get_public_base_url(self, request: Request) -> str:
        forwarded_host = request.headers.get("x-forwarded-host")
        forwarded_proto = request.headers.get("x-forwarded-proto")
        if forwarded_host:
            proto = forwarded_proto or request.url.scheme
            return f"{proto}://{forwarded_host}/"
        return str(request.base_url)

    def _build_icy_metadata(self, base_url: str) -> bytes:
        metadata = {}
        if getattr(self.radio_station, "continuous_audio", None):
            metadata = self.radio_station.continuous_audio.get_current_metadata()

        title = str(metadata.get("title") or "").strip()
        artist = str(metadata.get("artist") or "").strip()
        stream_title = " - ".join([part for part in [artist, title] if part])
        if not stream_title:
            stream_title = ""

        artwork_url = str(
            metadata.get("artwork_url") or artwork_url_for_track(metadata.get("file_path"))
        ).strip()
        if artwork_url:
            if artwork_url.startswith("/"):
                artwork_url = f"{base_url.rstrip('/')}{artwork_url}"
            elif "://" not in artwork_url:
                artwork_url = f"{base_url.rstrip('/')}/{artwork_url}"
            cache_key = metadata.get("metadata_revision") or int(time.time())
            separator = "&" if "?" in artwork_url else "?"
            artwork_url = f"{artwork_url}{separator}m={cache_key}"
        stream_url = artwork_url

        if stream_title:
            stream_title = stream_title.replace("'", "")
        stream_url = stream_url.replace("'", "")

        metadata_str = ""
        if stream_title or stream_url:
            metadata_str = f"StreamTitle='{stream_title}';StreamUrl='{stream_url}';"

        meta_bytes = metadata_str.encode("utf-8", "replace")
        if len(meta_bytes) > 4080:
            meta_bytes = meta_bytes[:4080]

        blocks = int(math.ceil(len(meta_bytes) / 16)) if meta_bytes else 0
        if blocks > 255:
            blocks = 255
            meta_bytes = meta_bytes[: blocks * 16]

        padding = (blocks * 16) - len(meta_bytes)
        return bytes([blocks]) + meta_bytes + (b"\x00" * padding)

    async def _stream_icy_audio(
        self,
        request: Request,
        metaint: int,
        client_ip: str | None,
        base_url: str,
    ):
        audio_stream = getattr(self.radio_station, "continuous_audio", None)
        if not audio_stream:
            return

        pcm_url = audio_stream.get_pcm_audio_url()
        if not pcm_url:
            return

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
            pcm_url,
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-f",
            "mp3",
            "pipe:1",
        ]

        process = None
        stderr_task = None
        tracker = getattr(self.radio_station, "track_direct_audio_listener", None)
        last_track_at = 0.0
        bytes_since = 0

        async def _drain_stderr(stream: asyncio.StreamReader):
            try:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", "replace").strip()
                    if text:
                        logger.warning("icy ffmpeg: %s", text)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug(f"ICY ffmpeg stderr error: {exc}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            if process.stderr:
                stderr_task = asyncio.create_task(_drain_stderr(process.stderr))

            while True:
                if await request.is_disconnected():
                    break

                chunk = await process.stdout.read(4096)
                if not chunk:
                    break

                if tracker and client_ip:
                    now = time.time()
                    if now - last_track_at > 15:
                        tracker(client_ip)
                        last_track_at = now

                if metaint <= 0:
                    yield chunk
                    continue

                offset = 0
                while offset < len(chunk):
                    remaining = metaint - bytes_since
                    take = min(remaining, len(chunk) - offset)
                    yield chunk[offset : offset + take]
                    bytes_since += take
                    offset += take

                    if bytes_since >= metaint:
                        yield self._build_icy_metadata(base_url)
                        bytes_since = 0

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug(f"ICY stream error: {exc}")
        finally:
            if process:
                process.kill()
                with suppress(Exception):
                    await process.wait()
            if stderr_task:
                stderr_task.cancel()
                with suppress(Exception):
                    await stderr_task

    def _now(self) -> datetime:
        """Get timezone-aware current time"""
        if (
            hasattr(self.radio_station, "config_manager")
            and self.radio_station.config_manager.timezone
        ):
            return datetime.now(self.radio_station.config_manager.timezone)
        return datetime.now()

    def _setup_routes(self):
        """Setup API routes"""

        @self.app.get("/stream.mp3")
        async def get_icy_stream(request: Request):
            """MP3 stream with ICY metadata (no Icecast required)."""
            audio_stream = getattr(self.radio_station, "continuous_audio", None)
            if not audio_stream:
                raise HTTPException(status_code=503, detail="Audio stream unavailable")

            station_name = "Radio Station"
            station_location = ""
            if getattr(self.radio_station, "config_manager", None):
                station_config = self.radio_station.config_manager.station_config
                if station_config:
                    station_name = station_config.station_name or station_name
                    station_location = station_config.location or ""

            wants_metadata = (
                request.headers.get("Icy-MetaData", "")
                or request.headers.get("icy-metadata", "")
            ) == "1"
            metaint = 16000 if wants_metadata else 0

            headers = {
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Content-Type": "audio/mpeg",
                "icy-name": station_name,
                "icy-description": station_location or station_name,
                "icy-br": "128",
                "icy-pub": "1",
            }
            if wants_metadata:
                headers["icy-metaint"] = str(metaint)

            client_ip = self._get_client_ip(request.scope)
            tracker = getattr(self.radio_station, "track_direct_audio_listener", None)
            if tracker:
                tracker(client_ip)

            base_url = self._get_public_base_url(request)
            stream = self._stream_icy_audio(
                request=request,
                metaint=metaint,
                client_ip=client_ip,
                base_url=base_url,
            )
            return StreamingResponse(stream, headers=headers, media_type="audio/mpeg")

        @self.app.get("/api/timeline/current")
        async def get_current_timeline():
            """Get the current timeline summary"""
            try:
                if not self.radio_station.timeline_scheduler:
                    raise HTTPException(
                        status_code=503, detail="Timeline scheduler not initialized"
                    )

                current_timeline = (
                    self.radio_station.timeline_scheduler.current_timeline
                )
                if not current_timeline:
                    raise HTTPException(status_code=404, detail="No active timeline")

                summary = self.radio_station.timeline_scheduler.get_timeline_summary(
                    current_timeline, self.radio_station.get_on_air_timeline_id()
                )
                return JSONResponse(content=summary)

            except Exception as e:
                logger.error(f"Error getting current timeline: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        @self.app.get("/api/timeline/upcoming")
        async def get_upcoming_items(count: int = 10):
            """Get upcoming timeline items"""
            try:
                if not self.radio_station.timeline_scheduler:
                    raise HTTPException(
                        status_code=503, detail="Timeline scheduler not initialized"
                    )

                current_timeline = (
                    self.radio_station.timeline_scheduler.current_timeline
                )
                if not current_timeline:
                    raise HTTPException(status_code=404, detail="No active timeline")

                on_air = self.radio_station.get_on_air_metadata()
                on_air_id = on_air.get("timeline_id")
                upcoming = self.radio_station.timeline_scheduler.get_playback_order(
                    current_timeline, count, on_air_id
                )

                # One row per audible element, with the row whose audio is
                # streaming right now marked so the client can anchor on it.
                rows: list[dict[str, Any]] = []
                on_air_index = 0
                for item in upcoming:
                    item_rows = _expand_item_rows(item)
                    if item.timeline_id == on_air_id:
                        on_air_index = len(rows) + _on_air_row_index(item_rows, on_air)
                    rows.extend(item_rows)

                now = self._now()
                # Only the row on air can be timed against the producer; a list
                # that has not reached the stream yet falls back to durations.
                if on_air_id and rows:
                    rows[on_air_index]["is_on_air"] = True
                    elapsed = _on_air_elapsed(on_air, now)
                    remaining = self.radio_station.get_on_air_remaining()
                else:
                    elapsed = 0.0
                    remaining = rows[0]["duration"] if rows else 0.0

                ends = _project_row_times(rows, on_air_index, now, elapsed, remaining)

                # Keep just enough history for a client sitting behind the live
                # edge to still find the row it is hearing.
                cutoff = now - timedelta(seconds=ROW_LOOKBEHIND_SECONDS)
                rows = [
                    row for row, end in zip(rows, ends, strict=True) if end > cutoff
                ][: count + 1]

                return {
                    "upcoming_items": rows,
                    "count": len(rows),
                    "current_time": now.isoformat(),
                }

            except Exception as e:
                logger.error(f"Error getting upcoming items: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        @self.app.get("/api/timeline/current-item")
        async def get_current_item():
            """Get the currently playing timeline item"""
            try:
                if not self.radio_station.timeline_scheduler:
                    raise HTTPException(
                        status_code=503, detail="Timeline scheduler not initialized"
                    )

                current_timeline = (
                    self.radio_station.timeline_scheduler.current_timeline
                )
                if not current_timeline:
                    raise HTTPException(status_code=404, detail="No active timeline")

                current_item = self.radio_station.timeline_scheduler.get_current_item(
                    current_timeline, self.radio_station.get_on_air_timeline_id()
                )
                if not current_item:
                    return JSONResponse(content={"current_item": None})

                # Serialize current item
                content_summary = {}
                if hasattr(current_item.content, "talk_type"):
                    content_summary = {
                        "type": "dj_talk",
                        "talk_type": current_item.content.talk_type,
                        "content": current_item.content.content,
                        "has_audio": bool(
                            getattr(current_item.content, "audio_file", None)
                        ),
                    }
                elif hasattr(current_item.content, "songs"):
                    songs_info = []
                    for song in current_item.content.songs:
                        songs_info.append(
                            {
                                "title": song.title,
                                "artist": song.artist,
                                # "album": song.album,
                                "year": song.year,
                                "duration": song.duration,
                            }
                        )
                    content_summary = {
                        "type": "song_block",
                        "block_type": current_item.content.block_type,
                        "songs_count": len(current_item.content.songs),
                        "songs": songs_info,
                        "selection_approach": current_item.content.selection_approach,
                        "intro_style": getattr(
                            current_item.content, "intro_style", None
                        ),
                        "has_intro_audio": getattr(
                            current_item.content, "dj_audio_ready", False
                        ),
                        "intro_audio_file": getattr(
                            current_item.content, "intro_audio", None
                        ),
                        "has_outro_audio": getattr(
                            current_item.content, "outro_audio_ready", False
                        ),
                        "outro_audio_file": getattr(
                            current_item.content, "outro_audio", None
                        ),
                    }

                return JSONResponse(
                    content={
                        "current_item": {
                            "timeline_id": current_item.timeline_id,
                            "item_type": current_item.item_type,
                            "scheduled_start": current_item.scheduled_start.isoformat(),
                            "estimated_duration": current_item.estimated_duration,
                            "status": current_item.status,
                            "content": content_summary,
                        },
                        "current_time": datetime.now().isoformat(),
                    }
                )

            except Exception as e:
                logger.error(f"Error getting current item: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        @self.app.post("/api/timeline/create")
        async def create_new_timeline(dj_id: str, duration_hours: int = 4):
            """Create a new timeline for a DJ show"""
            try:
                if not self.radio_station.timeline_scheduler:
                    raise HTTPException(
                        status_code=503, detail="Timeline scheduler not initialized"
                    )

                start_time = self._now()
                end_time = start_time.replace(
                    hour=(start_time.hour + duration_hours) % 24,
                    minute=0,
                    second=0,
                    microsecond=0,
                )

                timeline = self.radio_station.timeline_scheduler.create_show_timeline(
                    dj_id, start_time, end_time
                )

                # Start the timeline manager
                asyncio.create_task(
                    self.radio_station.timeline_scheduler.run_timeline_manager(timeline)
                )

                summary = self.radio_station.timeline_scheduler.get_timeline_summary(
                    timeline
                )
                return JSONResponse(
                    content={
                        "message": "Timeline created successfully",
                        "timeline": summary,
                    }
                )

            except Exception as e:
                logger.error(f"Error creating timeline: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        @self.app.get("/api/debug/scheduler-status")
        async def get_scheduler_status():
            """Get detailed scheduler status for debugging"""
            try:
                current_track = (
                    self.radio_station.continuous_audio.get_current_metadata()
                )
                playback_info = (
                    self.radio_station.continuous_audio.get_current_playback_info()
                )
                queue_size = self.radio_station.continuous_audio.get_queue_size()

                timeline_info = {}
                if (
                    self.radio_station.timeline_scheduler
                    and self.radio_station.timeline_scheduler.current_timeline
                ):
                    timeline = self.radio_station.timeline_scheduler.current_timeline
                    timeline_info = {
                        "timeline_id": timeline.timeline_id,
                        "dj_id": timeline.dj_id,
                        "total_items": len(timeline.items),
                        "is_running": self.radio_station.timeline_scheduler.is_running,
                        "active_preparations": len(
                            self.radio_station.timeline_scheduler.preparation_tasks
                        ),
                    }

                return JSONResponse(
                    content={
                        "timestamp": self._now().isoformat(),
                        "playback": {
                            "current_track": current_track,
                            "is_playing": playback_info.get("is_playing", False),
                            "remaining_time": playback_info.get("remaining_time", 0),
                            "queue_size": queue_size,
                        },
                        "timeline": timeline_info,
                        "scheduler": {
                            "has_program_scheduler": bool(
                                self.radio_station.program_scheduler
                            ),
                            "has_timeline_scheduler": bool(
                                self.radio_station.timeline_scheduler
                            ),
                            "is_running": self.radio_station.is_running,
                        },
                    }
                )

            except Exception as e:
                logger.error(f"Error getting scheduler status: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        @self.app.get("/api/weather")
        async def get_weather():
            """Get current weather information"""
            try:
                if hasattr(self.radio_station, "weather_service"):
                    weather_data = (
                        await self.radio_station.weather_service.get_current_weather()
                    )
                    if weather_data:
                        # Return clean formatted weather data for web interface
                        return JSONResponse(
                            content={
                                "condition": weather_data.get("condition", "Unknown"),
                                "temperature": weather_data.get("temperature", "--"),
                                "location": weather_data.get("location", "Unknown"),
                                "humidity": weather_data.get("humidity"),
                                "wind_speed": weather_data.get("wind_speed"),
                            }
                        )

                # Fallback weather data
                return JSONResponse(
                    content={
                        "condition": "Weather Unavailable",
                        "temperature": "--",
                        "location": "London",
                    }
                )
            except Exception as e:
                logger.error(f"Error getting weather: {e}")
                return JSONResponse(
                    content={
                        "condition": "Weather Service Error",
                        "temperature": "--",
                        "location": "London",
                    }
                )

        @self.app.get("/api/current-track")
        async def get_current_track():
            """Get current playing track information"""
            try:
                # Get metadata from continuous audio system
                current_metadata = (
                    self.radio_station.continuous_audio.get_current_metadata()
                )
                playback_info = (
                    self.radio_station.continuous_audio.get_current_playback_info()
                )
                listeners = getattr(self.radio_station, "cached_listener_count", 0)

                # Build response
                track_info = {
                    "title": current_metadata.get("title", "Unknown Title"),
                    "artist": current_metadata.get("artist", "Unknown Artist"),
                    "album": current_metadata.get("album", ""),
                    "listeners": listeners,
                    "is_playing": playback_info.get("is_playing", False),
                    "remaining_time": playback_info.get("remaining_time", 0),
                    "artwork_url": current_metadata.get(
                        "artwork_url",
                        artwork_url_for_track(current_metadata.get("file_path")),
                    ),
                }

                return JSONResponse(content=track_info)

            except Exception as e:
                logger.error(f"Error getting current track: {e}")
                return JSONResponse(
                    content={
                        "title": "Unknown Title",
                        "artist": "Unknown Artist",
                        "album": "",
                        "listeners": 0,
                        "is_playing": False,
                        "remaining_time": 0,
                    }
                )

        @self.app.get("/api/station")
        async def get_station():
            """Get station configuration"""
            try:
                station_config = self.radio_station.config_manager.station_config
                if station_config:
                    video_stream = getattr(self.radio_station, "video_stream", None)
                    video_info = None
                    if video_stream:
                        try:
                            video_info = {
                                "playlist": video_stream.get_public_path(),
                            }
                        except Exception as exc:
                            logger.warning(
                                f"Unable to build video stream info for station config: {exc}"
                            )

                    payload = {
                        "station_name": station_config.station_name,
                        "location": station_config.location,
                        "timezone": station_config.timezone,
                        "weather_location": station_config.weather_location,
                        "ad_break_interval_minutes": station_config.ad_break_interval_minutes,
                        "crossfade_duration_seconds": station_config.crossfade_duration_seconds,
                        "stream_bitrate": station_config.stream_bitrate,
                    }
                    if video_info:
                        payload["video_stream"] = video_info
                    audio_stream = getattr(
                        self.radio_station, "continuous_audio", None
                    )
                    audio_info = None
                    if audio_stream:
                        try:
                            audio_info = {
                                "playlist": audio_stream.get_public_path(),
                            }
                        except Exception as exc:
                            logger.warning(
                                f"Unable to build audio stream info for station config: {exc}"
                            )
                    if audio_info:
                        payload["audio_stream"] = audio_info

                    return JSONResponse(content=payload)
                else:
                    return JSONResponse(
                        status_code=500,
                        content={"error": "Station config not loaded"},
                    )
            except Exception as e:
                logger.error(f"Error getting station config: {e}")
                return JSONResponse(
                    status_code=500,
                    content={"error": "Failed to retrieve station config"},
                )

        @self.app.get("/api/schedule")
        async def get_schedule():
            """Get full schedule"""
            try:
                schedule_entries = self.radio_station.config_manager.schedule
                if schedule_entries:
                    # Convert schedule entries to dict
                    schedule_data = {
                        "schedule": [
                            {
                                "start_time": entry.start_time.strftime("%H:%M"),
                                "end_time": entry.end_time.strftime("%H:%M"),
                                "dj_name": entry.dj_name,
                                "music_folders": entry.music_folders,
                            }
                            for entry in schedule_entries
                        ]
                    }
                    return JSONResponse(content=schedule_data)
                else:
                    return JSONResponse(
                        status_code=500,
                        content={"error": "Schedule not loaded"},
                    )
            except Exception as e:
                logger.error(f"Error getting schedule: {e}")
                return JSONResponse(
                    status_code=500,
                    content={"error": "Failed to retrieve schedule"},
                )

        @self.app.get("/api/config")
        async def get_config():
            """Get combined station and schedule configuration"""
            try:
                station_config = self.radio_station.config_manager.station_config
                schedule_entries = self.radio_station.config_manager.schedule

                video_stream = getattr(self.radio_station, "video_stream", None)
                video_info = None
                if video_stream:
                    try:
                        video_info = {
                            "playlist": video_stream.get_public_path(),
                        }
                    except Exception as exc:
                        logger.warning(
                            f"Unable to build video stream info for config endpoint: {exc}"
                        )

                station_data = {
                    "station_name": station_config.station_name,
                    "location": station_config.location,
                    "timezone": station_config.timezone,
                    "weather_location": station_config.weather_location,
                    "ad_break_interval_minutes": station_config.ad_break_interval_minutes,
                    "crossfade_duration_seconds": station_config.crossfade_duration_seconds,
                    "stream_bitrate": station_config.stream_bitrate,
                }
                if video_info:
                    station_data["video_stream"] = video_info
                audio_stream = getattr(self.radio_station, "continuous_audio", None)
                audio_info = None
                if audio_stream:
                    try:
                        audio_info = {
                            "playlist": audio_stream.get_public_path(),
                        }
                    except Exception as exc:
                        logger.warning(
                            f"Unable to build audio stream info for config endpoint: {exc}"
                        )
                if audio_info:
                    station_data["audio_stream"] = audio_info

                schedule_data = {
                    "schedule": [
                        {
                            "start_time": entry.start_time.strftime("%H:%M"),
                            "end_time": entry.end_time.strftime("%H:%M"),
                            "dj_name": entry.dj_name,
                            "music_folders": entry.music_folders,
                        }
                        for entry in schedule_entries
                    ]
                }

                return JSONResponse(
                    content={"station": station_data, "schedule": schedule_data}
                )
            except Exception as e:
                logger.error(f"Error getting configuration: {e}")
                return JSONResponse(
                    status_code=500,
                    content={"error": "Failed to retrieve configuration"},
                )

        @self.app.get("/health")
        async def health_check():
            """Health check endpoint"""
            return JSONResponse(
                content={
                    "status": "healthy",
                    "timestamp": datetime.now().isoformat(),
                    "service": "Radio Timeline API",
                }
            )

    async def start(self, host: str = "0.0.0.0", port: int = 8080):
        """Start the API server"""
        import uvicorn

        config = uvicorn.Config(
            app=self.app, host=host, port=port, log_level="warning", access_log=True
        )

        self.server = uvicorn.Server(config)
        logger.info(f"Starting Timeline API server on {host}:{port}")

        # Run server in background task
        await self.server.serve()

    async def stop(self):
        """Stop the API server"""
        if self.server:
            self.server.should_exit = True
            logger.info("Timeline API server stopped")
