import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from uuid import uuid4

import aiohttp
import openai

from .config_manager import DJConfig
from .unified_dj_generator import UnifiedDJGenerator

logger = logging.getLogger(__name__)


class DJAI:
    """AI-powered DJ with configurable speech providers."""

    def __init__(self, scheduler=None, config_manager=None, weather_service=None):
        self.client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.temp_dir = Path("/app/temp-audio")
        self.temp_dir.mkdir(exist_ok=True)
        self.scheduler = scheduler
        self.config_manager = config_manager
        self.weather_service = weather_service

        # Initialize unified generator (will be set up after dependencies are available)
        self.unified_generator = None

    def setup_unified_generator(
        self, config_manager, weather_service, news_manager=None, sponsor_manager=None
    ):
        """Initialize the unified generator once dependencies are available."""
        self.config_manager = config_manager
        self.weather_service = weather_service
        self.unified_generator = UnifiedDJGenerator(
            dj_ai=self,
            config_manager=config_manager,
            weather_service=weather_service,
            scheduler=self.scheduler,
            news_manager=news_manager,
            sponsor_manager=sponsor_manager,
        )

    # NEW UNIFIED METHODS

    async def _generate_text_only(self, prompt: str) -> str | None:
        """Generate text content using OpenAI Chat API."""
        try:
            logger.info(f"🤖 TEXT PROMPT: {prompt}")

            response = await self.client.chat.completions.create(
                model="gpt-5.6-luna",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a professional radio DJ. Generate natural, conversational radio content.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )

            if response.choices:
                generated_text = response.choices[0].message.content.strip()
                logger.info(f"✅ TEXT RESPONSE: {generated_text}")
                return generated_text

            logger.warning("❌ OPENAI TEXT GENERATION: No choices returned")
            return None

        except Exception as e:
            logger.error(f"❌ OPENAI TEXT GENERATION ERROR: {e}")
            return None

    async def generate_song_facts(
        self, artist: str, title: str, album: str = None
    ) -> list[str]:
        """Return up to three short pop-up-video style facts for the track."""

        song_context = f"'{title}' by {artist}"
        if album:
            song_context += f" from the album '{album}'"

        prompt = (
            "You create fun Pop-Up Video style facts for a live radio stream. "
            "Respond ONLY with a JSON array of 3 concise strings (max 160 characters each). "
            "Each fact must be accurate, upbeat, and work as an on-screen caption. "
            f"Provide facts about {song_context}."
        )

        try:
            response_text = await self._generate_text_only(prompt)
            if not response_text:
                return []

            try:
                data = json.loads(response_text)
                if isinstance(data, list):
                    facts = [
                        str(item).strip()
                        for item in data
                        if isinstance(item, (str, int, float))
                    ]
                    return [fact for fact in facts if fact]
            except json.JSONDecodeError:
                logger.warning(
                    "⚠️ Song facts response not valid JSON, attempting fallback parse"
                )
                facts = [
                    line.strip().lstrip("•- ").strip()
                    for line in response_text.splitlines()
                    if line.strip()
                ]
                return facts[:3]

        except Exception as exc:
            logger.error(f"❌ Error generating song facts: {exc}")

        return []

    async def generate_emergency_announcement(
        self, dj_config: DJConfig, station_name: str
    ) -> str | None:
        """Generate an emergency announcement when the playlist is empty."""
        try:
            prompt = f"""You are {dj_config.name} on {station_name}.
            We're experiencing some technical difficulties with our music system.
            Please stand by while we get things sorted out.
            Thanks for your patience, and we'll be right back with more great music!"""

            # Use default speed for emergency announcements
            speech_file = await self._generate_speech(
                prompt,
                dj_config.voice_id,
                1.0,
                getattr(dj_config, "tts_instructions", None),
                getattr(dj_config, "voice_provider", "openai"),
            )

            if speech_file:
                logger.info(f"Generated emergency announcement for {dj_config.name}")
                return speech_file

        except Exception as e:
            logger.error(f"Error generating emergency announcement: {e}")

        return None

    def _apply_pronunciations(self, text: str) -> str:
        """Apply configured pronunciation overrides before TTS synthesis."""
        station = (
            getattr(self.config_manager, "station_config", None)
            if self.config_manager
            else None
        )
        pronunciations = getattr(station, "pronunciations", None) if station else None
        if not pronunciations:
            return text
        for source, replacement in pronunciations.items():
            text = text.replace(source, replacement)
        return text

    async def _generate_speech(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        tts_instructions: str = None,
        provider: str = "openai",
    ) -> str | None:
        """Generate speech using the configured TTS provider (OpenAI by default)."""
        provider_key = (provider or "openai").lower()

        text = self._apply_pronunciations(text)

        if provider_key == "elevenlabs":
            return await self._generate_speech_elevenlabs(text, voice_id, speed)
        if provider_key == "gemini":
            return await self._generate_speech_gemini(
                text, voice_id, speed, tts_instructions
            )

        # Default: OpenAI TTS
        return await self._generate_speech_openai(
            text, voice_id, speed, tts_instructions
        )

    async def _generate_speech_openai(
        self,
        text: str,
        voice_id: str,
        _speed: float = 1.0,
        tts_instructions: str = None,
    ) -> str | None:
        """Generate speech using OpenAI TTS (speed parameter unused as OpenAI doesn't support it)."""
        try:
            logger.info(f"🎤 SPEECH TEXT (OpenAI): {text}")

            instructions = (
                tts_instructions
                or "You are a professional radio DJ. Speak clearly and enthusiastically."
            )

            response = await self.client.audio.speech.create(
                model="gpt-4o-mini-tts",
                voice=voice_id,
                input=text,
                response_format="mp3",
                instructions=instructions,
            )

            import random

            timestamp = f"{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
            speech_file = self.temp_dir / f"dj_speech_{timestamp}.mp3"

            raw_speech_file = self.temp_dir / f"raw_dj_speech_{timestamp}.mp3"
            with raw_speech_file.open("wb") as f:
                for chunk in response.iter_bytes():
                    f.write(chunk)

            return await self._finalize_speech(raw_speech_file, speech_file)

        except Exception as e:
            logger.error(f"❌ OPENAI SPEECH GENERATION ERROR: {e}")
            return None

    async def _generate_speech_elevenlabs(
        self, text: str, voice_id: str, speed: float = 1.0
    ) -> str | None:
        """Generate speech using ElevenLabs TTS."""
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            logger.error(
                "❌ ELEVENLABS_API_KEY not set; cannot generate speech via ElevenLabs"
            )
            return None

        try:
            logger.info(f"🎤 SPEECH TEXT (ElevenLabs): {text}")

            # ElevenLabs does not support arbitrary speed; keep for future tuning
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
            headers = {
                "xi-api-key": api_key,
                "accept": "audio/mpeg",
                "content-type": "application/json",
            }

            payload = {
                "text": text,
                "model_id": "eleven_flash_v2_5",
                "voice_settings": {
                    "stability": 0.55,
                    "similarity_boost": 0.8,
                    "style": 0.0,
                    "use_speaker_boost": True,
                    "speed": speed,
                },
                "output_format": "mp3_44100_128",
                "optimize_streaming_latency": 2,
            }

            import random

            timestamp = f"{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
            speech_file = self.temp_dir / f"dj_speech_{timestamp}.mp3"
            raw_speech_file = self.temp_dir / f"raw_dj_speech_{timestamp}.mp3"

            async with (
                aiohttp.ClientSession() as session,
                session.post(url, headers=headers, json=payload) as resp,
            ):
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        f"❌ ElevenLabs TTS failed (status {resp.status}): {body[:300]}"
                    )
                    return None

                with raw_speech_file.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(4096):
                        if chunk:
                            f.write(chunk)

            return await self._finalize_speech(raw_speech_file, speech_file)

        except Exception as e:
            logger.error(f"❌ ELEVENLABS SPEECH GENERATION ERROR: {e}")
            return None

    async def _generate_speech_gemini(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        tts_instructions: str | None = None,
    ) -> str | None:
        """Generate DJ audio with Gemini 3.8 Flash TTS.

        Args:
            text: Transcript to speak verbatim.
            voice_id: Gemini voice name or voice library ID.
            speed: Requested relative pace, expressed as a style instruction.
            tts_instructions: Delivery directions kept separate from the transcript.

        Returns:
            A playable audio path, or None if synthesis fails.
        """
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logger.error("GEMINI_API_KEY not set; cannot generate Gemini speech")
            return None

        style = tts_instructions or (
            "You are a professional radio DJ. Speak clearly and enthusiastically."
        )
        if speed != 1.0:
            style += f" Speak at {speed:g} times your normal pace."
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": text, "speech_metadata": {"style": style}}],
                }
            ],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "responseFormat": {"audio": {"mimeType": "AUDIO_WAV"}},
                "speechConfig": {"voiceConfig": {"voice": voice_id}},
            },
        }
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as session,
                session.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    "gemini-3.8-flash-tts:generateContent",
                    headers={"x-goog-api-key": api_key},
                    json=payload,
                ) as response,
            ):
                if response.status != 200:
                    logger.error("Gemini TTS failed (status %s)", response.status)
                    return None
                result = await response.json()

            # Use one candidate only; alternatives must never be spoken together.
            candidates = result.get("candidates") or []
            parts = (
                candidates[0].get("content", {}).get("parts", []) if candidates else []
            )
            audio_parts = [part["inlineData"] for part in parts if "inlineData" in part]
            if len(audio_parts) != 1:
                logger.error("Gemini TTS returned no single audio result")
                return None
            audio = base64.b64decode(audio_parts[0]["data"], validate=True)
            # Unary WAV responses already contain their own header.
            if not (
                audio.startswith(b"RIFF") and audio[8:12] == b"WAVE" and len(audio) > 44
            ):
                logger.error("Gemini TTS returned invalid or empty WAV audio")
                return None
            identifier = uuid4().hex
            raw_file = self.temp_dir / f"raw_dj_speech_{identifier}.wav"
            output_file = self.temp_dir / f"dj_speech_{identifier}.mp3"
            raw_file.write_bytes(audio)
            return await self._finalize_speech(raw_file, output_file)
        except Exception as exc:
            logger.error("Gemini speech generation failed: %s", exc)
            return None

    async def _finalize_speech(self, raw_file: Path, output_file: Path) -> str:
        """Normalize speech and retain the original if conversion fails.

        Args:
            raw_file: Playable provider audio.
            output_file: Destination for normalized MP3 audio.

        Returns:
            The normalized path on success, otherwise the existing raw path.
        """
        normalized_file = await self._normalize_audio(str(raw_file), str(output_file))
        if not normalized_file:
            logger.warning("Audio normalization failed, using raw file")
            return str(raw_file)
        try:
            raw_file.unlink()
        except OSError as exc:
            logger.debug("Could not clean up raw speech file: %s", exc)
        return normalized_file

    async def _normalize_audio(self, input_file: str, output_file: str) -> str | None:
        """Normalize audio using FFmpeg loudnorm filter for consistent volume."""
        try:
            # FFmpeg command for audio normalization
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",  # Overwrite output file
                "-i",
                input_file,
                # Loudnorm filter for broadcast-standard normalization
                # Target: -12 LUFS (louder than music at -16 LUFS for DJ prominence)
                "-filter:a",
                "loudnorm=I=-12:LRA=7:tp=-1.5",
                # Output options for broadcast quality
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

            # Execute FFmpeg with timeout
            try:
                process = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd="/app",
                )

                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=30
                )

                if process.returncode == 0:
                    from pathlib import Path

                    if Path(output_file).exists():
                        logger.debug(f"🔊 Normalized DJ audio: {output_file}")
                        return output_file
                    else:
                        logger.warning(
                            "❌ FFmpeg succeeded but normalized file not found"
                        )
                        return None
                else:
                    logger.warning(
                        f"❌ Audio normalization failed (return code {process.returncode})"
                    )
                    logger.debug(
                        f"FFmpeg stderr: {stderr.decode() if stderr else 'None'}"
                    )
                    return None

            except TimeoutError:
                logger.warning("❌ Audio normalization timed out")
                return None

        except Exception as e:
            logger.warning(f"❌ Error normalizing audio: {e}")
            return None

    def cleanup_temp_files(self):
        """Clean up temporary speech files."""
        temp_dir = Path("/app/temp-audio")
        if temp_dir.exists():
            for file in temp_dir.glob("dj_speech_*.mp3"):
                try:
                    file.unlink()
                    logger.debug(f"Cleaned up temp file: {file}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp file {file}: {e}")
