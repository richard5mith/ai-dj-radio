"""
Config File Watcher

Monitors config and dj-configs directories for changes and automatically reloads configurations.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

logger = logging.getLogger(__name__)


class ConfigFileHandler(FileSystemEventHandler):
    """Handles file system events for config files."""

    def __init__(
        self,
        callback: Callable[[str, str], None],
        loop: asyncio.AbstractEventLoop,
        watch_audio: bool = False,
    ):
        """
        Initialize handler.

        Args:
            callback: Async function to call when a config file changes (receives file path)
            loop: The asyncio event loop to schedule callbacks on
            watch_audio: If True, watch for audio files instead of json files
        """
        super().__init__()
        self.callback = callback
        self.loop = loop
        self.watch_audio = watch_audio
        self._debounce_timers: dict[str, asyncio.TimerHandle] = {}
        self._audio_watch_started_at = time.time() if watch_audio else None

    def _handle_file_event(self, event: FileSystemEvent, event_type: str):
        """Common handler for file events."""
        if event.is_directory:
            return

        file_path = event.src_path

        # Watch different file types based on mode
        if self.watch_audio:
            # Watch for audio files
            if not file_path.endswith(
                (".mp3", ".m4a", ".mp4", ".aac", ".MP3", ".M4A", ".MP4", ".AAC")
            ):
                return
            if event_type == "deleted" and Path(file_path).exists():
                logger.debug(f"🟡 Ignoring delete for existing file: {file_path}")
                return
            if event_type in {"created", "modified"} and not Path(file_path).exists():
                logger.debug(f"🟡 Ignoring {event_type} for missing file: {file_path}")
                return
            is_initial_poll = False
            if (
                self._audio_watch_started_at
                and event_type in {"created", "modified"}
            ):
                try:
                    if (
                        Path(file_path).stat().st_mtime
                        < self._audio_watch_started_at - 1.0
                    ):
                        is_initial_poll = True
                except OSError:
                    is_initial_poll = False
            if event_type == "deleted":
                logger.debug(f"🔄 Audio file delete detected: {file_path}")
            else:
                if is_initial_poll:
                    logger.debug(f"🔄 Audio file {event_type}: {file_path}")
                else:
                    logger.info(f"🔄 Audio file {event_type}: {file_path}")
        else:
            # Only watch .json files
            if not file_path.endswith(".json"):
                return
            logger.info(f"🔄 Config file {event_type}: {file_path}")

        # Cancel existing timer for this file if any
        if file_path in self._debounce_timers:
            self._debounce_timers[file_path].cancel()

        # Schedule callback after debounce delay
        timer = self.loop.call_later(0.5, self._schedule_callback, file_path, event_type)
        self._debounce_timers[file_path] = timer

    def on_modified(self, event: FileSystemEvent):
        """Handle file modification events."""
        self._handle_file_event(event, "modified")

    def on_created(self, event: FileSystemEvent):
        """Handle file creation events."""
        self._handle_file_event(event, "created")

    def on_deleted(self, event: FileSystemEvent):
        """Handle file deletion events."""
        self._handle_file_event(event, "deleted")

    def _schedule_callback(self, file_path: str, event_type: str):
        """Schedule the callback on the event loop."""
        # Remove timer from tracking
        if file_path in self._debounce_timers:
            del self._debounce_timers[file_path]

        # Schedule the async callback as a task
        asyncio.run_coroutine_threadsafe(
            self.callback(file_path, event_type), self.loop
        )


class ConfigWatcher:
    """Watches configuration directories and triggers reloads on changes."""

    def __init__(
        self,
        config_path: str = "/app/config",
        dj_configs_path: str = "/app/dj-configs",
        music_path: str = "/app/music",
        jingles_path: str = "/app/jingles",
    ):
        """
        Initialize config watcher.

        Args:
            config_path: Path to config directory
            dj_configs_path: Path to dj-configs directory
            music_path: Path to music directory
            jingles_path: Path to jingles directory
        """
        self.config_path = Path(config_path)
        self.dj_configs_path = Path(dj_configs_path)
        self.music_path = Path(music_path)
        self.jingles_path = Path(jingles_path)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.observers: list = []
        self.handlers: list = []
        self._running = False
        self._pending_music_files: set[str] = set()
        self._music_reload_timer: asyncio.TimerHandle | None = None
        self._pending_music_deletes: set[str] = set()
        self._music_delete_timer: asyncio.TimerHandle | None = None
        self._music_watch_started_at: float | None = None
        self._pending_jingles_files: set[str] = set()
        self._jingles_reload_timer: asyncio.TimerHandle | None = None
        self._jingles_watch_started_at: float | None = None

        # Callbacks for different config types
        self.reload_callbacks: dict[str, Callable] = {}

    def register_reload_callback(self, config_type: str, callback: Callable):
        """
        Register a callback for a specific config type.

        Args:
            config_type: Type of config (e.g., 'station', 'sponsors', 'news_feeds', 'dj_configs')
            callback: Async function to call when that config changes
        """
        self.reload_callbacks[config_type] = callback
        logger.info(f"📋 Registered reload callback for: {config_type}")

    async def _handle_file_change(self, file_path: str, _event_type: str):
        """Handle a file change by calling the appropriate reload callback."""
        file_name = Path(file_path).name

        # Determine which config changed and call appropriate callback
        config_type = None

        if file_name == "station.json":
            config_type = "station"
        elif file_name == "schedule.json":
            config_type = "schedule"
        elif file_name == "sponsors.json":
            config_type = "sponsors"
        elif file_name == "news_feeds.json":
            config_type = "news_feeds"
        elif file_name == "scheduler_weights.json":
            config_type = "scheduler_weights"
        elif file_name == "dj_prompts.json":
            config_type = "dj_prompts"
        elif file_name == "jingles.json":
            config_type = "jingles"
        elif file_path.find("dj-configs") != -1:
            # Any file in dj-configs directory
            config_type = "dj_configs"

        if config_type and config_type in self.reload_callbacks:
            logger.info(f"🔄 Reloading {config_type} configuration...")
            try:
                callback = self.reload_callbacks[config_type]
                if asyncio.iscoroutinefunction(callback):
                    await callback()
                else:
                    callback()
                logger.info(f"✅ Successfully reloaded {config_type} configuration")
            except Exception as e:
                logger.error(f"❌ Error reloading {config_type} configuration: {e}")
        else:
            logger.debug(f"No reload callback registered for: {file_name}")

    async def _handle_music_change(self, file_path: str, event_type: str):
        """Handle music file changes (new songs added or removed)."""
        if event_type == "deleted":
            self._queue_music_deletion(file_path)
            return

        if not Path(file_path).exists():
            logger.debug(f"🟡 Ignoring {event_type} for missing file: {file_path}")
            return

        if self._should_ignore_music_event(file_path, event_type):
            return

        file_name = Path(file_path).name
        logger.info(f"🎵 Music file detected: {file_name}")

        # Add to pending files
        self._pending_music_files.add(file_path)

        # Cancel existing timer if any
        if self._music_reload_timer:
            self._music_reload_timer.cancel()

        # Schedule batched processing after 2 seconds
        loop = self._get_loop()
        if not loop:
            return
        self._music_reload_timer = loop.call_later(
            2.0, self._process_pending_music_files
        )

    async def _handle_jingles_change(self, file_path: str, event_type: str):
        """Handle jingle file changes and trigger jingle reload batching."""
        if event_type in {"created", "modified"} and not Path(file_path).exists():
            logger.debug(f"🟡 Ignoring {event_type} for missing jingle: {file_path}")
            return

        if self._should_ignore_initial_audio_event(
            file_path, event_type, self._jingles_watch_started_at
        ):
            return

        logger.info("🎵 Jingle file %s: %s", event_type, Path(file_path).name)
        self._pending_jingles_files.add(file_path)

        if self._jingles_reload_timer:
            self._jingles_reload_timer.cancel()

        loop = self._get_loop()
        if not loop:
            return
        self._jingles_reload_timer = loop.call_later(
            1.0, self._process_pending_jingles_files
        )

    def _queue_music_deletion(self, file_path: str):
        """Queue a music file deletion for confirmation."""
        self._pending_music_deletes.add(file_path)

        if self._music_delete_timer:
            self._music_delete_timer.cancel()

        loop = self._get_loop()
        if not loop:
            return
        self._music_delete_timer = loop.call_later(
            5.0, self._process_pending_music_deletes
        )

    def _process_pending_music_files(self):
        """Process all pending music files in a batch."""
        if not self._pending_music_files:
            return

        files_to_process = list(self._pending_music_files)
        self._pending_music_files.clear()
        self._music_reload_timer = None

        # Schedule async processing
        loop = self._get_loop()
        if not loop:
            return
        asyncio.run_coroutine_threadsafe(
            self._add_music_files_async(files_to_process), loop
        )

    def _process_pending_music_deletes(self):
        """Process pending music deletions after confirmation."""
        if not self._pending_music_deletes:
            return

        files_to_process = list(self._pending_music_deletes)
        self._pending_music_deletes.clear()
        self._music_delete_timer = None

        loop = self._get_loop()
        if not loop:
            return
        asyncio.run_coroutine_threadsafe(
            self._remove_music_files_async(files_to_process), loop
        )

    def _process_pending_jingles_files(self) -> None:
        """Process pending jingle changes by reloading jingle configuration."""
        if not self._pending_jingles_files:
            return

        files_to_process = list(self._pending_jingles_files)
        self._pending_jingles_files.clear()
        self._jingles_reload_timer = None

        loop = self._get_loop()
        if not loop:
            return

        asyncio.run_coroutine_threadsafe(
            self._reload_jingles_async(files_to_process), loop
        )

    async def _reload_jingles_async(self, file_paths: list[str]) -> None:
        """Reload jingles after one or more jingle file changes."""
        if "jingles" not in self.reload_callbacks:
            logger.debug("No jingles reload callback registered")
            return

        logger.info("🔄 Processing %s jingle file change(s)...", len(file_paths))
        callback = self.reload_callbacks["jingles"]

        try:
            if asyncio.iscoroutinefunction(callback):
                await callback()
            else:
                callback()
            logger.info("✅ Successfully reloaded jingles after file changes")
        except Exception as e:
            logger.error(f"❌ Error reloading jingles after file changes: {e}")

    async def _add_music_files_async(self, file_paths: list[str]):
        """Add music files to the library asynchronously."""
        if "music_library" not in self.reload_callbacks:
            return

        logger.info(f"🔄 Processing {len(file_paths)} new music file(s)...")

        try:
            music_library = self.reload_callbacks["music_library"]
            added_count = 0

            # Check if music_library has add_single_file method
            if hasattr(music_library, "__self__") and hasattr(
                music_library.__self__, "add_single_file"
            ):
                library = music_library.__self__
                candidates = []
                for index, file_path in enumerate(file_paths):
                    if not Path(file_path).exists():
                        continue
                    if hasattr(library, "has_file") and library.has_file(file_path):
                        continue
                    candidates.append(file_path)
                    if index % 200 == 0:
                        await asyncio.sleep(0)

                if not candidates:
                    logger.info("ℹ️ No new tracks detected after filtering")
                    return

                # Add files individually
                for index, file_path in enumerate(candidates):
                    try:
                        result = await library.add_single_file(file_path)
                        if result:
                            added_count += 1
                    except Exception as e:
                        logger.error(f"Error adding {file_path}: {e}")
                    if index % 50 == 0:
                        await asyncio.sleep(0)

                if added_count > 0:
                    logger.info(f"✅ Added {added_count} new track(s) to music library")
                else:
                    logger.info(
                        "ℹ️ No new tracks added (all were duplicates or invalid)"
                    )
            else:
                # Fallback to full reload if method not available
                logger.warning(
                    "add_single_file not available, falling back to full reload"
                )
                if asyncio.iscoroutinefunction(music_library):
                    await music_library()
                else:
                    music_library()

        except Exception as e:
            logger.error(f"❌ Error processing music files: {e}")

    async def _remove_music_files_async(self, file_paths: list[str]):
        """Remove music files from the library asynchronously after confirmation."""
        if "music_library" not in self.reload_callbacks:
            return

        missing_files = [path for path in file_paths if not Path(path).exists()]
        if not missing_files:
            logger.info("ℹ️ No confirmed deletions after recheck")
            return

        logger.info(f"🗑️ Processing {len(missing_files)} confirmed deletion(s)...")

        try:
            music_library = self.reload_callbacks["music_library"]
            if hasattr(music_library, "__self__") and hasattr(
                music_library.__self__, "remove_single_file"
            ):
                removed_count = 0
                for index, file_path in enumerate(missing_files):
                    try:
                        if music_library.__self__.remove_single_file(file_path):
                            removed_count += 1
                    except Exception as e:
                        logger.error(f"Error removing {file_path}: {e}")
                    if index % 50 == 0:
                        await asyncio.sleep(0)

                if removed_count > 0:
                    logger.info(
                        f"✅ Removed {removed_count} track(s) from music library"
                    )
                else:
                    logger.info("ℹ️ No tracks removed (already absent)")
            else:
                logger.warning(
                    "remove_single_file not available, falling back to full reload"
                )
                if asyncio.iscoroutinefunction(music_library):
                    await music_library()
                else:
                    music_library()

        except Exception as e:
            logger.error(f"❌ Error processing music deletions: {e}")

    def start(self):
        """Start watching config directories."""
        if self._running:
            logger.warning("Config watcher already running")
            return

        self._running = True

        # Get the current event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("No running event loop - config watcher cannot start")
            return
        self.loop = loop

        # Watch config directory
        if self.config_path.exists():
            handler = ConfigFileHandler(self._handle_file_change, loop)
            observer = Observer()
            observer.schedule(handler, str(self.config_path), recursive=False)
            observer.start()
            self.handlers.append(handler)
            self.observers.append(observer)
            logger.info(f"👀 Watching config directory: {self.config_path}")
        else:
            logger.warning(f"Config directory not found: {self.config_path}")

        # Watch dj-configs directory
        if self.dj_configs_path.exists():
            handler = ConfigFileHandler(self._handle_file_change, loop)
            observer = Observer()
            observer.schedule(handler, str(self.dj_configs_path), recursive=False)
            observer.start()
            self.handlers.append(handler)
            self.observers.append(observer)
            logger.info(f"👀 Watching dj-configs directory: {self.dj_configs_path}")
        else:
            logger.warning(f"DJ configs directory not found: {self.dj_configs_path}")

        # Watch music directory for new audio files
        # Use PollingObserver for music directory to work reliably with Docker bind mounts
        if self.music_path.exists():
            handler = ConfigFileHandler(
                self._handle_music_change, loop, watch_audio=True
            )
            observer = PollingObserver(timeout=60)  # Check every 60 seconds
            observer.schedule(handler, str(self.music_path), recursive=True)
            observer.start()
            self.handlers.append(handler)
            self.observers.append(observer)
            self._music_watch_started_at = time.time()
            logger.info(
                f"🎵 Watching music directory (polling mode): {self.music_path}"
            )
        else:
            logger.warning(f"Music directory not found: {self.music_path}")

        # Watch jingles directory for sting changes
        if self.jingles_path.exists():
            handler = ConfigFileHandler(
                self._handle_jingles_change, loop, watch_audio=True
            )
            observer = PollingObserver(timeout=10)  # Keep jingle updates responsive
            observer.schedule(handler, str(self.jingles_path), recursive=True)
            observer.start()
            self.handlers.append(handler)
            self.observers.append(observer)
            self._jingles_watch_started_at = time.time()
            logger.info(
                f"🎵 Watching jingles directory (polling mode): {self.jingles_path}"
            )
        else:
            logger.warning(f"Jingles directory not found: {self.jingles_path}")

        logger.info("🔍 Config watcher started")

    def stop(self):
        """Stop watching config directories."""
        self._running = False
        self._music_watch_started_at = None
        self._jingles_watch_started_at = None

        if self._music_reload_timer:
            self._music_reload_timer.cancel()
            self._music_reload_timer = None
        if self._music_delete_timer:
            self._music_delete_timer.cancel()
            self._music_delete_timer = None
        if self._jingles_reload_timer:
            self._jingles_reload_timer.cancel()
            self._jingles_reload_timer = None

        for observer in self.observers:
            observer.stop()
            observer.join()

        self.observers.clear()
        self.handlers.clear()
        self.loop = None

        logger.info("🔍 Config watcher stopped")

    def _get_music_library(self):
        """Return the music library instance if available."""
        music_library = self.reload_callbacks.get("music_library")
        if not music_library:
            return None
        if hasattr(music_library, "__self__"):
            return music_library.__self__
        return None

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the active event loop used for debounce timers and async callbacks."""
        if self.loop is None:
            logger.error("Config watcher event loop is not available")
            return None
        return self.loop

    def _music_library_has_file(self, file_path: str) -> bool:
        """Check whether the music library already tracks this file."""
        library = self._get_music_library()
        if not library or not hasattr(library, "has_file"):
            return False
        return library.has_file(file_path)

    def _should_ignore_music_event(self, file_path: str, event_type: str) -> bool:
        """Ignore events that are likely initial polling snapshots or duplicates."""
        if event_type not in {"created", "modified"}:
            return False

        if self._music_library_has_file(file_path):
            if self._music_watch_started_at:
                try:
                    if (
                        Path(file_path).stat().st_mtime
                        < self._music_watch_started_at - 1.0
                    ):
                        logger.debug(
                            "🟡 Ignoring initial poll event for known file: %s",
                            file_path,
                        )
                    else:
                        logger.debug("🟡 Ignoring duplicate music event: %s", file_path)
                except OSError:
                    logger.debug("🟡 Ignoring duplicate music event: %s", file_path)
            else:
                logger.debug("🟡 Ignoring duplicate music event: %s", file_path)
            return True

        if self._should_ignore_initial_audio_event(
            file_path, event_type, self._music_watch_started_at
        ):
            logger.debug("🟡 Ignoring initial poll event for unknown file: %s", file_path)
            return True

        return False

    def _should_ignore_initial_audio_event(
        self, file_path: str, event_type: str, watch_started_at: float | None
    ) -> bool:
        """Return True when an audio event appears to be from initial polling state."""
        if event_type not in {"created", "modified"} or not watch_started_at:
            return False

        try:
            return Path(file_path).stat().st_mtime < watch_started_at - 1.0
        except OSError:
            return False
