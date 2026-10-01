"""Exercise Gemini speech routing, failures, and playable audio output."""

import asyncio
import base64
import io
import math
import struct
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from radio_server.dj_ai import DJAI


def wav_audio() -> bytes:
    """Return a short mono WAV fixture; takes no arguments."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(
            b"".join(
                struct.pack("<h", int(4000 * math.sin(2 * math.pi * 440 * i / 24000)))
                for i in range(24000)
            )
        )
    return buffer.getvalue()


@pytest.fixture
def dj(tmp_path: Path) -> DJAI:
    """Build an isolated DJ using tmp_path; return the speech generator."""
    with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
        instance = DJAI()
    instance.temp_dir = tmp_path
    return instance


def mock_session(result: dict, status: int = 200) -> MagicMock:
    """Return an HTTP session mock with the given JSON result and status."""
    response = AsyncMock()
    response.status = status
    response.headers = {}
    response.json.return_value = result
    session = MagicMock()
    session.post.return_value.__aenter__.return_value = response
    session.__aenter__.return_value = session
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,body,expected",
    [
        ({"Retry-After": "45"}, {}, 45),
        (
            {},
            {
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "60.5s",
                        }
                    ]
                }
            },
            60.5,
        ),
        ({"Retry-After": "invalid"}, {}, 15),
        ({"Retry-After": "nan"}, {"error": {"details": None}}, 15),
        ({"Retry-After": "-1"}, [], 15),
        ({"Retry-After": "Wed, 01 Jan 2020 00:00:00 GMT"}, {}, 15),
    ],
)
async def test_retry_hints(
    dj: DJAI, headers: dict, body: dict | list, expected: float
) -> None:
    """Verify response headers/body produce a safe minimum retry delay."""
    response = AsyncMock()
    response.headers = headers
    response.json.return_value = body
    with patch("radio_server.dj_ai.random.uniform", return_value=0):
        assert await dj._gemini_retry_delay(response) == expected


@pytest.mark.asyncio
async def test_gemini_retries_then_succeeds(dj: DJAI) -> None:
    """Verify a 429 is retried after its cooldown and success clears backoff."""
    session = mock_session({}, 429)
    response = session.post.return_value.__aenter__.return_value
    response.headers = {"Retry-After": "45"}
    clock = [1000.0]

    async def advance(delay: float) -> None:
        """Advance the fake clock by delay and make the next request succeed."""
        clock[0] += delay
        response.status = 200
        response.json.return_value = {"candidates": []}

    with (
        patch("radio_server.dj_ai.aiohttp.ClientSession", return_value=session),
        patch("radio_server.dj_ai.time.monotonic", side_effect=lambda: clock[0]),
        patch("radio_server.dj_ai.asyncio.sleep", side_effect=advance) as sleep,
        patch("radio_server.dj_ai.random.uniform", return_value=0),
    ):
        assert await dj._request_gemini_speech("key", {}) == {"candidates": []}
    sleep.assert_awaited_once_with(45)
    assert session.post.call_count == 2
    assert dj._gemini_rate_limit_count == 0
    assert dj._gemini_retry_at == 0


@pytest.mark.asyncio
async def test_gemini_retry_exhaustion_preserves_cooldown(dj: DJAI) -> None:
    """Verify bounded exponential retries and cooldown shared with later calls."""
    session = mock_session({}, 429)
    clock = [1000.0]

    async def advance(delay: float) -> None:
        """Advance the fake monotonic clock by delay; return nothing."""
        clock[0] += delay

    with (
        patch("radio_server.dj_ai.aiohttp.ClientSession", return_value=session),
        patch("radio_server.dj_ai.time.monotonic", side_effect=lambda: clock[0]),
        patch("radio_server.dj_ai.asyncio.sleep", side_effect=advance) as sleep,
        patch("radio_server.dj_ai.random.uniform", return_value=0),
    ):
        assert await dj._request_gemini_speech("key", {}) is None
        assert session.post.call_count == 3
        assert [call.args[0] for call in sleep.await_args_list] == [15, 30]
        assert dj._gemini_retry_at == clock[0] + 60
        assert await dj._request_gemini_speech("key", {}) is None
        assert session.post.call_count == 4
        assert sleep.await_args_list[-1].args == (60,)


@pytest.mark.asyncio
async def test_long_cooldown_skips_later_requests(dj: DJAI) -> None:
    """Verify long quota delays do not hold generation open or hammer Gemini."""
    session = mock_session({}, 429)
    session.post.return_value.__aenter__.return_value.headers = {"Retry-After": "3600"}
    with (
        patch("radio_server.dj_ai.aiohttp.ClientSession", return_value=session),
        patch("radio_server.dj_ai.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        assert await dj._request_gemini_speech("key", {}) is None
        assert await dj._request_gemini_speech("key", {}) is None
    assert session.post.call_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_gemini_cancellation_releases_lock(dj: DJAI) -> None:
    """Verify cancellation propagates and releases the shared request lock."""
    session = mock_session({}, 429)
    with (
        patch("radio_server.dj_ai.aiohttp.ClientSession", return_value=session),
        patch("radio_server.dj_ai.asyncio.sleep", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await dj._request_gemini_speech("key", {})
    assert not dj._gemini_lock.locked()
    assert dj._gemini_retry_at > 0


@pytest.mark.asyncio
async def test_gemini_serializes_concurrent_requests(dj: DJAI) -> None:
    """Verify concurrent speech preparation never sends overlapping requests."""
    session = mock_session({})
    entered = asyncio.Event()
    release = asyncio.Event()

    async def result() -> dict:
        """Wait for release before returning JSON; signal when HTTP is active."""
        entered.set()
        await release.wait()
        return {}

    session.post.return_value.__aenter__.return_value.json.side_effect = result
    with patch("radio_server.dj_ai.aiohttp.ClientSession", return_value=session):
        async with asyncio.timeout(5), asyncio.TaskGroup() as group:
            group.create_task(dj._request_gemini_speech("key", {}))
            await entered.wait()
            group.create_task(dj._request_gemini_speech("key", {}))
            await asyncio.sleep(0)
            assert session.post.call_count == 1
            release.set()
    assert session.post.call_count == 2


@pytest.mark.asyncio
async def test_non_json_rate_limit_uses_backoff(dj: DJAI) -> None:
    """Verify a non-JSON 429 still receives exponential backoff."""
    response = AsyncMock()
    response.headers = {}
    response.json.side_effect = ValueError("not JSON")
    dj._gemini_rate_limit_count = 2
    with patch("radio_server.dj_ai.random.uniform", return_value=0):
        assert await dj._gemini_retry_delay(response) == 60


@pytest.mark.asyncio
async def test_gemini_request_and_normalization(dj: DJAI) -> None:
    """Verify the DJ request and real FFmpeg conversion; return nothing."""
    session = mock_session(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "data": base64.b64encode(wav_audio()).decode()
                                }
                            }
                        ]
                    }
                }
            ]
        }
    )
    dj.config_manager = MagicMock()
    dj.config_manager.station_config.pronunciations = {
        "Strathaven": "Strayven",
    }
    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        patch("radio_server.dj_ai.aiohttp.ClientSession", return_value=session),
    ):
        result = await dj._generate_speech("Strathaven", "Kore", 1.2, "Warm", "gemini")
    assert result is not None
    assert Path(result).suffix == ".mp3"
    assert Path(result).stat().st_size > 0
    assert not list(dj.temp_dir.glob("raw_*"))
    request = session.post.call_args.kwargs
    assert request["headers"] == {"x-goog-api-key": "test-key"}
    part = request["json"]["contents"][0]["parts"][0]
    assert part["text"] == "Strayven"
    assert (
        part["speech_metadata"]["style"] == "Warm Speak at 1.2 times your normal pace."
    )
    assert request["json"]["generationConfig"]["speechConfig"]["voiceConfig"] == {
        "voice": "Kore"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,status",
    [
        ({}, 403),
        ({"candidates": []}, 200),
        ({"candidates": [{"content": {"parts": [{"text": "blocked"}]}}]}, 200),
        (
            {"candidates": [{"content": {"parts": [{"inlineData": {"data": "%%%"}}]}}]},
            200,
        ),
        (
            {
                "candidates": [
                    {"content": {"parts": [{"inlineData": {"data": "YWJj"}}]}}
                ]
            },
            200,
        ),
    ],
)
async def test_gemini_bad_response(dj: DJAI, result: dict, status: int) -> None:
    """Verify failed or malformed responses return None without files."""
    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        patch(
            "radio_server.dj_ai.aiohttp.ClientSession",
            return_value=mock_session(result, status),
        ),
    ):
        assert await dj._generate_speech_gemini("Hello", "Kore") is None
    assert not list(dj.temp_dir.iterdir())


@pytest.mark.asyncio
async def test_gemini_missing_key(dj: DJAI) -> None:
    """Verify missing credentials skip HTTP requests; return nothing."""
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("radio_server.dj_ai.aiohttp.ClientSession") as session,
    ):
        assert await dj._generate_speech_gemini("Hello", "Kore") is None
        session.assert_not_called()


@pytest.mark.asyncio
async def test_gemini_timeout(dj: DJAI) -> None:
    """Verify HTTP timeouts safely return None; return nothing."""
    session = mock_session({})
    session.post.return_value.__aenter__.side_effect = TimeoutError
    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
        patch("radio_server.dj_ai.aiohttp.ClientSession", return_value=session),
    ):
        assert await dj._generate_speech_gemini("Hello", "Kore") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".wav", ".mp3"])
async def test_normalization_failure_preserves_audio(dj: DJAI, suffix: str) -> None:
    """Verify raw_file survives failed normalization for both provider formats."""
    raw_file = dj.temp_dir / f"raw{suffix}"
    raw_file.write_bytes(b"original audio")
    with patch.object(dj, "_normalize_audio", new=AsyncMock(return_value=None)):
        result = await dj._finalize_speech(raw_file, dj.temp_dir / "output.mp3")
    assert Path(result).read_bytes() == b"original audio"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "elevenlabs"])
async def test_existing_provider_routing(dj: DJAI, provider: str) -> None:
    """Verify each existing provider still receives speech; return nothing."""
    with patch.object(
        dj, f"_generate_speech_{provider}", new=AsyncMock(return_value="speech.mp3")
    ) as generate:
        assert (
            await dj._generate_speech("Hello", "voice", provider=provider)
            == "speech.mp3"
        )
        generate.assert_awaited_once()
