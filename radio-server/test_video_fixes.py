"""Checks for the video stream bug fixes.

Covers:
- VIDEO_ENCODE_THREADS=0 meaning "auto" (not coerced to a single thread)
- artwork feeder generation guard (orphaned feeders must exit)
- live-overlay guard (stale-track publishes must not revert current.jpg)
- artwork pacing (I/O delays must not accumulate into early metadata changes)

Run from radio-server/: .venv/bin/python test_video_fixes.py
"""

import select
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from radio_server.audio_utils import artwork_filename_for
from radio_server.video_stream_producer import (
    VideoStreamProducer,
    _parse_encode_threads,
)


def _bare_producer() -> VideoStreamProducer:
    """Instance without __init__ (it hardcodes /app paths unavailable on host)."""
    producer = VideoStreamProducer.__new__(VideoStreamProducer)
    producer._running = True
    producer._artwork_feeder_running = True
    producer._artwork_feeder_generation = 1
    return producer


def test_encode_threads_zero_means_auto() -> None:
    assert _parse_encode_threads("0") is None, "0 must mean x264 auto threads"
    assert _parse_encode_threads(None) is None
    assert _parse_encode_threads("") is None
    assert _parse_encode_threads("abc") is None
    assert _parse_encode_threads("2") == 2
    assert _parse_encode_threads("1") == 1


def test_feeder_generation_guard() -> None:
    producer = _bare_producer()

    assert producer._is_feeder_current(1), "current generation must be live"

    # A newer feeder started (or this one stopped): generation bumped.
    producer._artwork_feeder_generation = 2
    assert not producer._is_feeder_current(1), (
        "orphaned feeder must exit once superseded"
    )
    assert producer._is_feeder_current(2)

    # Stop path: flag cleared even without a generation bump.
    producer._artwork_feeder_running = False
    assert not producer._is_feeder_current(2)


def test_stale_publish_cannot_revert_live_artwork() -> None:
    producer = _bare_producer()
    with tempfile.TemporaryDirectory() as tmp:
        producer.artwork_dir = Path(tmp)
        producer.current_artwork_file = Path(tmp) / "current.jpg"
        producer.current_artwork_file.write_bytes(b"live-bytes")

        live_path = "/app/music/live_track.m4a"
        stale_path = "/app/music/stale_track.m4a"
        producer._live_artwork_file_path = live_path

        # Stale-track publish: per-track file written, current.jpg untouched.
        stale_frame = b"\xff\xd8\xff\xd9stale"
        producer._publish_artwork_bytes(stale_frame, stale_path)
        assert producer.current_artwork_file.read_bytes() == b"live-bytes", (
            "stale publish must not revert the live overlay"
        )
        assert (
            producer.artwork_dir / artwork_filename_for(stale_path)
        ).read_bytes() == stale_frame, "per-track file must still resolve"

        # Live-track publish updates the overlay.
        live_frame = b"\xff\xd8\xff\xd9live"
        producer._publish_artwork_bytes(live_frame, live_path)
        assert producer.current_artwork_file.read_bytes() == live_frame

        # Placeholder (file_path=None) publishes when None is live.
        producer._live_artwork_file_path = None
        producer._publish_artwork_bytes(b"\xff\xd8\xff\xd9ph", None)
        assert producer.current_artwork_file.read_bytes() == b"\xff\xd8\xff\xd9ph"


def test_artwork_normalized_to_fixed_canvas() -> None:
    producer = _bare_producer()
    for size in [(200, 181), (500, 400), (100, 100), (300, 300), (200, 200)]:
        normalized = producer._normalize_artwork_image(
            Image.new("RGB", size, (10, 20, 30))
        )
        assert normalized.size == (200, 200), (
            f"{size} must publish as 200x200 (fixed pipe geometry)"
        )
        assert normalized.mode == "RGB"

    # RGBA flattens to RGB on the same fixed canvas.
    rgba = Image.new("RGBA", (160, 120), (10, 20, 30, 255))
    normalized = producer._normalize_artwork_image(rgba)
    assert normalized.size == (200, 200) and normalized.mode == "RGB"


def test_artwork_pacing_does_not_accumulate_io_or_sleep_delays() -> None:
    """Keep artwork timestamps on the audio clock despite write delays.

    Returns:
        None. Raises AssertionError if the feeder accumulates timing drift.
    """
    producer = _bare_producer()
    producer._artwork_frame_interval = 1.0
    clock = 0.0
    emitted_at: list[float] = []

    def write_frame(_data: bytes) -> None:
        """Record a frame and simulate pipe work, including a temporary stall.

        Args:
            _data: Encoded frame sent to the pipe.

        Returns:
            None.
        """
        nonlocal clock
        emitted_at.append(clock)
        clock += 3.0 if len(emitted_at) == 20 else 0.02
        if len(emitted_at) == 100:
            producer._artwork_feeder_running = False

    def sleep(seconds: float) -> None:
        """Advance the fake clock with a small operating system wakeup delay.

        Args:
            seconds: Requested sleep duration.

        Returns:
            None.
        """
        nonlocal clock
        clock += seconds + 0.003

    with tempfile.TemporaryDirectory() as tmp:
        producer.current_artwork_file = Path(tmp) / "current.jpg"
        Image.new("RGB", (200, 200)).save(producer.current_artwork_file)
        producer.artwork_pipe_path = Path(tmp) / "artwork.pipe"
        pipe = MagicMock()
        pipe.__enter__.return_value = pipe
        pipe.write.side_effect = write_frame
        module = "radio_server.video_stream_producer"
        with (
            patch(f"{module}.open_fifo_for_writing", return_value=42),
            patch(f"{module}.os.fdopen", return_value=pipe),
            patch(f"{module}.time.monotonic", side_effect=lambda: clock),
            patch(f"{module}.time.sleep", side_effect=sleep),
        ):
            producer._stream_artwork_frames(1)

    # At 1 fps, frame 100 belongs at 99 seconds, even after a transient stall.
    assert abs(emitted_at[-1] - 99.0) < 0.01, emitted_at[-1]


def test_pcm_input_starts_without_buffering_seconds_of_audio() -> None:
    """Decode live PCM immediately while the input remains open.

    Returns:
        None. Raises AssertionError if FFmpeg waits for a probing buffer.
    """
    producer = _bare_producer()
    producer.audio_source_type = "pcm"
    producer.audio_source = "pipe:0"
    producer.audio_sample_rate = 44100
    producer.audio_channels = 2
    command = [
        "ffmpeg", "-v", "error", *producer._build_audio_input_args(),
        "-frames:a", "1", "-f", "s16le", "pipe:1",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        # Supply 200 ms of PCM, keeping the live input open. Default
        # probing waits for seconds more data even though its format is known.
        process.stdin.write(bytes(35280))
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 2.0)
        assert ready, "PCM decoder buffered audio instead of emitting it immediately"
        assert process.stdout.read(1), "PCM decoder exited without producing audio"
    finally:
        process.kill()
        process.communicate(timeout=3)


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("All video fix checks passed")


if __name__ == "__main__":
    main()
