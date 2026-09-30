"""Audio utilities — centralised async audio processing helpers.

Provides safe, non-blocking audio operations for the radio station, plus the
shared plumbing for the FIFOs that feed the ffmpeg pipelines.
"""

import asyncio
import errno
import hashlib
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

PLACEHOLDER_ARTWORK_FILENAME = "placeholder.jpg"
ARTWORK_URL_PREFIX = "/artwork"


def artwork_filename_for(file_path: str | None) -> str:
    """Stable per-track artwork filename, content-addressed by source path.

    The video producer writes a normalized JPEG at this name for every track
    that has a file_path (real artwork, or placeholder content when none is
    found). Tracks without a file_path (DJ talk, jingles) share a single
    placeholder image. Because the filename is a pure function of the source
    path, the metadata pipeline can publish the URL before the file is written
    and a lagged listener loads the art for the track they actually hear,
    not the one at the live edge.

    Args:
        file_path: Absolute path of the source audio file, or None.

    Returns:
        Basename of the artwork file under /artwork/ for that track.
    """
    if not file_path:
        return PLACEHOLDER_ARTWORK_FILENAME
    digest = hashlib.md5(file_path.encode("utf-8", "ignore")).hexdigest()[:12]
    return f"artwork_{digest}.jpg"


def artwork_url_for_track(file_path: str | None) -> str:
    """Public /artwork/ URL for a track's artwork (see artwork_filename_for)."""
    return f"{ARTWORK_URL_PREFIX}/{artwork_filename_for(file_path)}"


if __name__ == "__main__":  # pragma: no cover - self-check
    # The URL and filename must agree, the hash must be stable, and a missing
    # file_path must fall back to the shared placeholder. The video producer
    # and the metadata pipeline both rely on this contract.
    assert artwork_filename_for(None) == PLACEHOLDER_ARTWORK_FILENAME
    assert artwork_url_for_track(None) == "/artwork/placeholder.jpg"
    assert artwork_filename_for("/music/foo.mp3") == "artwork_77ce5bc87152.jpg"
    assert artwork_url_for_track("/music/foo.mp3") == "/artwork/artwork_77ce5bc87152.jpg"
    # Same path must always map to the same filename.
    assert artwork_filename_for("/music/foo.mp3") == artwork_filename_for(
        "/music/foo.mp3"
    )
    print("artwork helper self-check OK")


def get_audio_duration_sync(file_path: str) -> float:
    """Blocking ffprobe-based duration probe for startup paths.

    Use during init (loading jingles) where there's no event loop yet. Inside
    coroutines, prefer the async variant to avoid blocking the loop.
    """
    try:
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"Audio file does not exist: {file_path}")
            return 0.0
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
        return 0.0
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        logger.warning(f"ffprobe sync failed for {file_path}: {e}")
        return 0.0


async def get_audio_duration(file_path: str) -> float:
    """
    Get audio duration using ffprobe in a safe, async manner.

    Args:
        file_path: Path to the audio file

    Returns:
        Duration in seconds as float, or 0.0 if unable to determine
    """
    try:
        # Normalize path
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"Audio file does not exist: {file_path}")
            return 0.0

        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

        if process.returncode == 0 and stdout:
            duration = float(stdout.decode().strip())
            logger.debug(f"Audio duration for {file_path}: {duration:.2f}s")
            return duration
        else:
            logger.warning(
                f"ffprobe failed for {file_path} (return code: {process.returncode})"
            )
            if stderr:
                logger.debug(f"ffprobe stderr: {stderr.decode()}")
            return 0.0

    except TimeoutError:
        logger.warning(f"ffprobe timeout for {file_path}")
        return 0.0
    except ValueError as e:
        logger.warning(f"Invalid duration format for {file_path}: {e}")
        return 0.0
    except Exception as e:
        logger.error(f"Error getting duration for {file_path}: {e}")
        return 0.0


def open_fifo_for_writing(fifo_path: Path | str) -> int | None:
    """Open a FIFO for writing without waiting for a reader to turn up.

    Opening non-blocking is the only way to find out that nothing is reading
    yet instead of hanging on the open. Leaving the descriptor non-blocking,
    though, turns every sizeable write into a partial one: the kernel takes
    what fits in the pipe buffer, reports how much it took, and the rest is
    silently dropped — which truncates anything larger than the buffer. So the
    descriptor is put back into blocking mode once it is open.

    Args:
        fifo_path: Path to the FIFO to open.

    Returns:
        An open file descriptor, or None when no reader is connected yet.
    """
    try:
        fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            # The pipe exists but nothing is reading it yet.
            return None
        raise

    os.set_blocking(fd, True)
    return fd
