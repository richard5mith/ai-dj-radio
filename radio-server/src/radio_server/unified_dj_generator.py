"""
Unified DJ Talk Generation System

This module consolidates all DJ talk generation into a single, consistent system
that properly handles all types of DJ content with structured prompts and
weighted selection.
"""

import json
import logging
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pytz

from .config_manager import DJConfig

logger = logging.getLogger(__name__)

LISTENER_SEED_FILES = {
    "names": ("listener_names.json", "names"),
    "locations": ("listener_locations.json", "locations"),
    "verbs": ("listener_verbs.json", "verbs"),
    "objects": ("listener_objects.json", "objects"),
}

DEFAULT_LISTENER_SEEDS = {
    "names": [
        "Alex",
        "Jamie",
        "Sam",
        "Taylor",
        "Jordan",
        "Casey",
        "Morgan",
    ],
    "locations": [
        "London",
        "Manchester",
        "Bristol",
        "Glasgow",
        "Cardiff",
        "Belfast",
    ],
    "verbs": [
        "cooking",
        "walking",
        "working",
        "studying",
        "cleaning",
        "painting",
    ],
    "objects": [
        "dinner",
        "dog",
        "car",
        "garden",
        "desk",
        "kitchen",
    ],
}


class DJTalkRequest:
    """Represents a request for DJ talk generation."""

    def __init__(
        self,
        talk_category: str,  # intro_styles, outro_styles, dj_talk_types, show_transitions
        style: str = None,  # specific style, or None for weighted selection
        context: dict[str, Any] = None,
        songs: list[dict[str, Any]] = None,  # Song information for context
        previous_song: dict[str, Any] = None,  # For outros and transitions
        next_song: dict[str, Any] = None,  # For intros and transitions
        weather_data: dict[str, Any] = None,
        custom_context: dict[str, Any] = None,
    ):
        self.talk_category = talk_category
        self.style = style
        self.context = context or {}
        self.songs = songs or []
        self.previous_song = previous_song
        self.next_song = next_song
        self.weather_data = weather_data
        self.custom_context = custom_context or {}


class DJTalkResult:
    """Result of DJ talk generation."""

    def __init__(
        self,
        success: bool,
        text_content: str = None,
        audio_file: str = None,
        talk_category: str = None,
        style: str = None,
        duration: float = None,
        context_used: dict[str, Any] = None,
        error: str = None,
    ):
        self.success = success
        self.text_content = text_content
        self.audio_file = audio_file
        self.talk_category = talk_category
        self.style = style
        self.duration = duration
        self.context_used = context_used or {}
        self.error = error


class UnifiedDJGenerator:
    """
    Unified DJ talk generation system that handles all types of DJ content
    using structured prompts and consistent logic.
    """

    def __init__(
        self,
        dj_ai,
        config_manager,
        weather_service,
        scheduler=None,
        news_manager=None,
        sponsor_manager=None,
    ):
        self.dj_ai = dj_ai
        self.config_manager = config_manager
        self.weather_service = weather_service
        self.scheduler = scheduler
        self.news_manager = news_manager
        self.sponsor_manager = sponsor_manager
        self.prompts = self._load_dj_prompts()
        self._listener_seed_log_once: set[str] = set()

        # Style selection history for avoiding repetition
        self.recent_styles = {}  # {talk_category: [recent_styles]}
        self.max_style_history = 3

    def _load_dj_prompts(self) -> dict[str, Any]:
        """Load DJ prompts from configuration file."""
        prompts_path = Path("/app/config/dj_prompts.json")
        try:
            if prompts_path.exists():
                with prompts_path.open() as f:
                    prompts = json.load(f)
                    logger.info(f"✅ Loaded unified DJ prompts from {prompts_path}")
                    return prompts
        except Exception as e:
            logger.error(f"Failed to load DJ prompts from {prompts_path}: {e}")
            exit(1)

    def reload_dj_prompts(self):
        """Reload DJ prompts from configuration file."""
        logger.info("🔄 Reloading DJ prompts...")
        try:
            self.prompts = self._load_dj_prompts()
            logger.info("✅ DJ prompts reloaded successfully")
        except Exception as e:
            logger.error(f"❌ Failed to reload DJ prompts: {e}")

    def _resolve_listener_seed_path(self, file_name: str) -> Path:
        docker_path = Path("/app/config") / file_name
        if docker_path.exists():
            return docker_path
        base_dir = Path(__file__).parent.parent.parent.parent
        return base_dir / "config" / file_name

    def _log_listener_seed_warning_once(self, seed_key: str, reason: str, message: str):
        log_key = f"{seed_key}:{reason}"
        if log_key in self._listener_seed_log_once:
            return
        self._listener_seed_log_once.add(log_key)
        logger.warning(message)

    def _load_listener_seed_values(self, seed_key: str) -> list[str]:
        if seed_key not in LISTENER_SEED_FILES:
            return []
        file_name, root_key = LISTENER_SEED_FILES[seed_key]
        fallback = DEFAULT_LISTENER_SEEDS.get(seed_key, [])
        path = self._resolve_listener_seed_path(file_name)
        if not path.exists():
            self._log_listener_seed_warning_once(
                seed_key,
                "missing",
                f"Listener seed file not found at {path}, using defaults",
            )
            return fallback
        try:
            with path.open() as f:
                data = json.load(f)
        except Exception as e:
            self._log_listener_seed_warning_once(
                seed_key,
                "error",
                f"Failed to load listener seed file {path}: {e}",
            )
            return fallback

        values = data.get(root_key) if isinstance(data, dict) else data
        if not isinstance(values, list):
            self._log_listener_seed_warning_once(
                seed_key,
                "invalid",
                f"Listener seed file {path} missing list '{root_key}', using defaults",
            )
            return fallback

        cleaned = [str(item).strip() for item in values if str(item).strip()]
        if not cleaned:
            self._log_listener_seed_warning_once(
                seed_key,
                "empty",
                f"Listener seed file {path} has no usable values, using defaults",
            )
            return fallback
        return cleaned

    def _select_listener_interaction_seeds(self) -> dict[str, str]:
        return {
            "listener_name": random.choice(self._load_listener_seed_values("names")),
            "listener_location": random.choice(
                self._load_listener_seed_values("locations")
            ),
            "listener_verb": random.choice(self._load_listener_seed_values("verbs")),
            "listener_object": random.choice(
                self._load_listener_seed_values("objects")
            ),
        }

    async def generate_dj_talk(
        self, dj_config: DJConfig, request: DJTalkRequest
    ) -> DJTalkResult:
        """
        Generate DJ talk content based on the request.
        This is the main entry point for all DJ talk generation.
        """
        try:
            # Special handling for sponsor talk type - select sponsor first, then determine style
            actual_style = request.style
            selected_sponsor = None

            if request.talk_category == "dj_talk_types" and request.style == "sponsor":
                if self.sponsor_manager:
                    selected_sponsor = self.sponsor_manager.get_sponsor(dj_config.name)
                    if selected_sponsor:
                        # Use sponsor's style to determine prompt (read or vamp)
                        actual_style = f"sponsor_{selected_sponsor.style}"
                        logger.info(
                            f"💰 Selected sponsor: {selected_sponsor.name} "
                            f"(style={selected_sponsor.style})"
                        )
                    else:
                        logger.warning(
                            f"⚠️ No sponsors available for {dj_config.name}, skipping"
                        )
                        return DJTalkResult(
                            success=False, error="No sponsors available"
                        )
                else:
                    logger.warning(
                        "⚠️ Sponsor talk type selected but no sponsor_manager available!"
                    )
                    return DJTalkResult(
                        success=False, error="Sponsor manager not initialized"
                    )

            # Build comprehensive context
            context = await self._build_context(dj_config, request, selected_sponsor)

            # Select style (weighted or specified)
            style = self._select_style(dj_config, request.talk_category, actual_style)

            # Get prompt configuration
            prompt_config = self._get_prompt_config(request.talk_category, style)
            if not prompt_config:
                return DJTalkResult(
                    success=False,
                    error=f"No prompt config found for {request.talk_category}/{style}",
                )

            # Generate the prompt
            prompt = self._build_prompt(dj_config, prompt_config, context, request)

            logger.info(
                f"🎙️ Generating DJ talk: category={request.talk_category}, style={style}"
            )
            logger.info(f"🎙️ Prompt length: {len(prompt)} chars")

            # Generate text content
            text_content = await self.dj_ai._generate_text_only(prompt)
            if not text_content:
                return DJTalkResult(
                    success=False, error="Failed to generate text content"
                )

            # Generate audio
            audio_file = await self.dj_ai._generate_speech(
                text_content,
                dj_config.voice_id,
                getattr(dj_config, "speech_speed", 1.0),
                getattr(dj_config, "tts_instructions", None),
                getattr(dj_config, "voice_provider", "openai"),
            )

            if not audio_file:
                return DJTalkResult(
                    success=False, error="Failed to generate audio file"
                )

            # Calculate duration if possible
            from .audio_utils import get_audio_duration

            duration = await get_audio_duration(audio_file)

            # Update style history
            self._update_style_history(request.talk_category, style)

            logger.info(
                f"✅ Generated unified DJ talk for {dj_config.name}: {request.talk_category}/{style}"
            )

            return DJTalkResult(
                success=True,
                text_content=text_content,
                audio_file=audio_file,
                talk_category=request.talk_category,
                style=style,
                duration=duration,
                context_used=context,
            )

        except Exception as e:
            logger.error(f"❌ Failed to generate unified DJ talk: {e}")
            return DJTalkResult(success=False, error=str(e))

    async def _build_context(
        self, dj_config: DJConfig, request: DJTalkRequest, selected_sponsor=None
    ) -> dict[str, Any]:
        """Build comprehensive context for DJ talk generation."""
        # Get station info
        station_config = self.config_manager.station_config
        station_name = station_config.station_name if station_config else "Radio Station"

        # Build base context
        context = {
            "dj_name": dj_config.name.replace("_", " ").title(),
            "personality": dj_config.personality_prompt,
            "station_name": station_name,
        }

        current_entry = self.config_manager.get_current_schedule_entry()
        if current_entry:
            context["current_show_end"] = current_entry.end_time.strftime("%H:%M")

        next_entry = self.config_manager.get_next_schedule_entry()
        if next_entry:
            context["next_dj_name"] = next_entry.dj_name.replace("_", " ").title()
            context["next_show_start"] = next_entry.start_time.strftime("%H:%M")
            context["next_show_end"] = next_entry.end_time.strftime("%H:%M")

        # Add time and location context
        timezone_str = os.getenv("TIMEZONE", "UTC")
        try:
            timezone = pytz.timezone(timezone_str)
            now = datetime.now(timezone)
        except pytz.UnknownTimeZoneError:
            logger.warning(f"Unknown timezone '{timezone_str}', using UTC")
            now = datetime.now(pytz.UTC)

        context.update(
            {
                "current_time": now.strftime("%H:%M"),
                "time_of_day": now.strftime("%A, %B %d at %I:%M %p"),
                "timezone": timezone_str,
            }
        )

        # Add seasonal context
        month = now.month
        if month in [12, 1, 2]:
            season = "winter"
        elif month in [3, 4, 5]:
            season = "spring"
        elif month in [6, 7, 8]:
            season = "summer"
        else:
            season = "autumn"
        context["season"] = season

        # Add weather context
        if request.weather_data:
            weather = request.weather_data
        else:
            weather = await self.weather_service.get_current_weather()

        if weather:
            context.update(
                {
                    "weather": f"{weather.get('condition', 'unknown')}, {weather.get('temperature', 'N/A')}°C",
                    "location": weather.get("location", self.weather_service.location),
                }
            )
        else:
            context.update(
                {
                    "weather": "weather information unavailable",
                    "location": self.weather_service.location,
                }
            )

        if (
            request.talk_category == "dj_talk_types"
            and request.style == "listener_interaction"
        ):
            listener_seeds = self._select_listener_interaction_seeds()
            context.update(listener_seeds)
            context["listener_activity"] = (
                f"{listener_seeds['listener_verb']} {listener_seeds['listener_object']}"
            ).strip()

        # Add song context
        if request.next_song:
            context["next_song"] = request.next_song
        if request.previous_song:
            context["previous_song"] = request.previous_song
        if request.songs:
            context["songs"] = request.songs
            if request.songs:
                # Add primary song info to top-level context
                song = request.songs[0]
                context.update(
                    {
                        "song_title": song.get("title", ""),
                        "song_artist": song.get("artist", ""),
                        "song_album": song.get("album", ""),
                        "song_year": song.get("year", ""),
                    }
                )

        # Add news headlines if news manager is available and style is 'news'
        if (
            self.news_manager
            and request.talk_category == "dj_talk_types"
            and request.style == "news"
        ):
            news_headlines = self.news_manager.get_headlines_text()
            context["news_headlines"] = news_headlines
            logger.info(
                f"📰 Adding news headlines to context ({len(news_headlines)} chars)"
            )
            logger.info(
                f"📰 Headlines: {news_headlines[:200]}..."
            )  # Log first 200 chars
        elif request.talk_category == "dj_talk_types" and request.style == "news":
            logger.warning("⚠️ News style selected but no news_manager available!")
            context["news_headlines"] = "No recent news headlines available."

        # Add sponsor content if sponsor was already selected
        if selected_sponsor:
            sponsor_context = self.sponsor_manager.get_sponsor_context(selected_sponsor)
            context.update(sponsor_context)
            logger.info(
                f"💰 Adding sponsor context: {selected_sponsor.name} "
                f"(style={selected_sponsor.style})"
            )

        # Merge any custom context
        context.update(request.custom_context)
        context.update(request.context)

        if request.talk_category == "dj_talk_types" and request.style == "trivia":
            trivia_topics = getattr(dj_config, "trivia_topics", None) or []
            if trivia_topics:
                context["trivia_topics"] = trivia_topics

        return context

    def _select_style(
        self, dj_config: DJConfig, talk_category: str, requested_style: str = None
    ) -> str:
        """Select a style using weighted selection or return the requested style."""
        if requested_style:
            return requested_style

        # Get available styles
        available_styles = self.prompts.get(talk_category, {})
        if not available_styles:
            logger.warning(f"No styles available for category: {talk_category}")
            return "brief"  # fallback

        # Get weights for this DJ and category
        weights = self._get_weights_for_dj(dj_config.name, talk_category)

        # Filter out recently used styles to avoid repetition
        recent = self.recent_styles.get(talk_category, [])
        available_weights = {}
        for style, weight in weights.items():
            if style not in recent and style in available_styles:
                available_weights[style] = weight

        # If all styles have been used recently, reset and use all
        if not available_weights:
            logger.debug(f"All {talk_category} styles used recently, resetting")
            available_weights = {
                style: weight
                for style, weight in weights.items()
                if style in available_styles
            }

        # Fallback if no weights
        if not available_weights:
            return list(available_styles.keys())[0]

        # Weighted selection
        return self._weighted_choice(available_weights)

    def _get_weights_for_dj(self, dj_name: str, category: str) -> dict[str, float]:
        """Get weights for DJ and category, with fallbacks."""
        if not self.scheduler:
            # Equal weights if no scheduler
            styles = self.prompts.get(category, {})
            return dict.fromkeys(styles.keys(), 1.0)

        # Try DJ-specific weights first
        weights = self.scheduler._get_weights_for_dj(dj_name, category)

        if not weights:
            # Fallback to global weights
            weights = self.scheduler.weights.get("global_weights", {}).get(category, {})

        if not weights:
            # Final fallback - equal weights for all available styles
            styles = self.prompts.get(category, {})
            weights = dict.fromkeys(styles.keys(), 1.0)

        return weights

    def _weighted_choice(self, weights: dict[str, float]) -> str:
        """Make a weighted random choice."""
        if not weights:
            return "brief"  # fallback

        items = list(weights.keys())
        weights_list = list(weights.values())
        return random.choices(items, weights=weights_list, k=1)[0]

    def _get_prompt_config(self, talk_category: str, style: str) -> dict[str, Any]:
        """Get prompt configuration for category and style."""
        return self.prompts.get(talk_category, {}).get(style, {})

    def _build_prompt(
        self,
        dj_config: DJConfig,
        prompt_config: dict[str, Any],
        context: dict[str, Any],
        request: DJTalkRequest,
    ) -> str:
        """Build the complete prompt for text generation."""
        # Start with DJ personality
        prompt = dj_config.personality_prompt + "\n\n"
        prompt += "It is " + context.get("time_of_day", "") + "\n\n"

        # Add the style-specific prompt with context formatting
        style_prompt = prompt_config.get("prompt", "")
        try:
            formatted_prompt = style_prompt.format(**context)
        except KeyError as e:
            logger.warning(f"Context key {e} not available for prompt formatting")
            formatted_prompt = style_prompt

        prompt += formatted_prompt

        # Handle extended talk for filling time
        if request.context and request.context.get("extended_talk"):
            target_duration = request.context.get("target_duration", 60)
            remaining_show_time = request.context.get("remaining_show_time", 0)

            # Calculate words needed for target duration (assuming ~2.5 words per second for natural speech)
            target_words = int(target_duration * 2.5)

            prompt += f"\n\nIMPORTANT: You need to fill approximately {target_duration:.0f} seconds of airtime "
            prompt += f"(about {target_words} words). The show ends in {remaining_show_time / 60:.1f} minutes, "
            prompt += "so expand your talk naturally. You can discuss multiple topics, share stories, "
            prompt += "interact with listeners, mention upcoming music, or talk about the station. "
            prompt += "Be conversational and engaging while filling the time naturally."

            # Override max_words for extended talk
            max_words = target_words
        else:
            # Add word limit guidance (except for sponsor reads which explicitly say not to mention limits)
            max_words = prompt_config.get("max_words", 30)
            if "Do not mention word limits" not in formatted_prompt:
                prompt += f" Keep it to {max_words} words or less."

        # Add song-specific information based on talk category
        if request.talk_category == "intro_styles":
            # Check if we have multiple songs or selection approach context
            selection_approach = context.get("selection_approach")
            songs = request.songs or []

            if len(songs) > 1:
                # Multiple songs - provide context about the block
                prompt += f"\n\nYou're about to play {len(songs)} songs:"
                for i, song in enumerate(songs, 1):
                    prompt += (
                        f"\n{i}. '{song.get('title', '')}' by {song.get('artist', '')}"
                    )

                # Add selection approach context
                if selection_approach == "same_artist":
                    artist = songs[0].get("artist", "")
                    prompt += f"\n\nThese are all songs by {artist}. You can mention this in your intro."
                elif selection_approach == "same_album":
                    artist = songs[0].get("artist", "")
                    album = songs[0].get("album", "")
                    prompt += f"\n\nThese songs are all from {artist}'s album '{album}'. You can reference this."
                elif selection_approach == "same_genre":
                    genre = songs[0].get("genre", "")
                    prompt += f"\n\nThese are all {genre} songs. You can mention the genre theme."
                elif selection_approach == "same_year":
                    year = songs[0].get("year", "")
                    prompt += f"\n\nThese songs are all from {year}. You can mention the year."
                elif selection_approach == "same_decade":
                    year = songs[0].get("year", "")
                    decade = (year // 10) * 10 if year else None
                    if decade:
                        prompt += f"\n\nThese songs are all from the {decade}s. You can reference the decade."
            elif request.next_song:
                # Single song with next_song set
                song = request.next_song
                prompt += f"\n\nYou're about to play '{song.get('title', '')}' by {song.get('artist', '')}"
                if song.get("album"):
                    prompt += f" from the album '{song['album']}'"
                if song.get("year"):
                    prompt += f" ({song['year']})"
                prompt += "."
            elif songs:
                # Single song from songs list
                song = songs[0]
                prompt += f"\n\nYou're about to play '{song.get('title', '')}' by {song.get('artist', '')}"
                if song.get("album"):
                    prompt += f" from the album '{song['album']}'"
                if song.get("year"):
                    prompt += f" ({song['year']})"
                prompt += "."

        elif request.talk_category == "outro_styles":
            # Check if we have multiple songs or selection approach context
            selection_approach = context.get("selection_approach")
            songs = request.songs or []

            if len(songs) > 1:
                # Multiple songs - provide context about what just played
                prompt += f"\n\nYou just played {len(songs)} songs:"
                for i, song in enumerate(songs, 1):
                    prompt += (
                        f"\n{i}. '{song.get('title', '')}' by {song.get('artist', '')}"
                    )

                # Add selection approach context
                if selection_approach == "same_artist":
                    artist = songs[0].get("artist", "")
                    prompt += f"\n\nThese were all songs by {artist}. You can reference this in your outro."
                elif selection_approach == "same_album":
                    artist = songs[0].get("artist", "")
                    album = songs[0].get("album", "")
                    prompt += f"\n\nThese songs were all from {artist}'s album '{album}'. You can mention this."
                elif selection_approach == "same_genre":
                    genre = songs[0].get("genre", "")
                    prompt += f"\n\nThese were all {genre} songs. You can reference the genre theme."
                elif selection_approach == "same_year":
                    year = songs[0].get("year", "")
                    prompt += f"\n\nThese songs were all from {year}. You can mention the year."
                elif selection_approach == "same_decade":
                    year = songs[0].get("year", "")
                    decade = (year // 10) * 10 if year else None
                    if decade:
                        prompt += f"\n\nThese songs were all from the {decade}s. You can reference the decade."
            elif request.previous_song:
                # Single song with previous_song set
                song = request.previous_song
                prompt += f"\n\nYou just finished playing '{song.get('title', '')}' by {song.get('artist', '')}"
                if song.get("album"):
                    prompt += f" from the album '{song['album']}'"
                if song.get("year"):
                    prompt += f" ({song['year']})"
                prompt += "."
            if request.style == "next_preview":
                next_in_block = context.get("next_song_in_block")
                next_after_block = context.get("next_song_after_block")
                preview_target = next_in_block or request.next_song
                if preview_target:
                    prompt += f"\n\nUp next is '{preview_target.get('title', '')}' by {preview_target.get('artist', '')}"
                    if preview_target.get("album"):
                        prompt += f" from the album '{preview_target['album']}'"
                    if preview_target.get("year"):
                        prompt += f" ({preview_target['year']})"
                    prompt += ". Use this to shape your tease."

                if next_in_block and next_after_block:
                    prompt += (
                        "\n\nAfter this block, the next song is "
                        f"'{next_after_block.get('title', '')}' by {next_after_block.get('artist', '')}'. "
                        "If it helps, you can say 'coming up next is X, but first Y.'"
                    )
            elif songs:
                # Single song from songs list
                song = songs[0]
                prompt += f"\n\nYou just finished playing '{song.get('title', '')}' by {song.get('artist', '')}"
                if song.get("album"):
                    prompt += f" from the album '{song['album']}'"
                if song.get("year"):
                    prompt += f" ({song['year']})"
                prompt += "."

        elif request.songs:
            # General song context (fallback for other talk categories)
            song = request.songs[0]
            prompt += (
                f"\n\nThe song is '{song.get('title', '')}' by {song.get('artist', '')}"
            )
            if song.get("album"):
                prompt += f" from the album '{song['album']}'"
            if song.get("year"):
                prompt += f" ({song['year']})"
            prompt += "."

        # Final instructions
        prompt += "\n\nGenerate ONLY the spoken content, no instructions or meta-text. Don't start with a greeting unless specifically asked."

        return prompt

    def _update_style_history(self, talk_category: str, style: str):
        """Update the history of recently used styles."""
        if talk_category not in self.recent_styles:
            self.recent_styles[talk_category] = []

        history = self.recent_styles[talk_category]
        if style in history:
            history.remove(style)

        history.append(style)

        # Trim to max size
        if len(history) > self.max_style_history:
            self.recent_styles[talk_category] = history[-self.max_style_history :]

    # Convenience methods for common use cases

    async def generate_song_intro(
        self,
        dj_config: DJConfig,
        song: dict[str, Any],
        style: str = None,
        weather_data: dict[str, Any] = None,
    ) -> DJTalkResult:
        """Generate a song introduction."""
        request = DJTalkRequest(
            talk_category="intro_styles",
            style=style,
            next_song=song,
            songs=[song],
            weather_data=weather_data,
        )
        return await self.generate_dj_talk(dj_config, request)

    async def generate_song_outro(
        self,
        dj_config: DJConfig,
        song: dict[str, Any],
        style: str = None,
        weather_data: dict[str, Any] = None,
    ) -> DJTalkResult:
        """Generate a song outro."""
        request = DJTalkRequest(
            talk_category="outro_styles",
            style=style,
            previous_song=song,
            songs=[song],
            weather_data=weather_data,
        )
        return await self.generate_dj_talk(dj_config, request)

    async def generate_transition(
        self,
        dj_config: DJConfig,
        previous_song: dict[str, Any],
        next_song: dict[str, Any],
        weather_data: dict[str, Any] = None,
    ) -> DJTalkResult:
        """Generate a transition between two songs."""
        # For transitions, we could use either intro or outro styles
        # Let's use outro styles with both song contexts
        request = DJTalkRequest(
            talk_category="outro_styles",
            previous_song=previous_song,
            next_song=next_song,
            songs=[previous_song, next_song],
            weather_data=weather_data,
            custom_context={
                "transition_mode": True,
                "previous_title": previous_song.get("title", ""),
                "previous_artist": previous_song.get("artist", ""),
                "next_title": next_song.get("title", ""),
                "next_artist": next_song.get("artist", ""),
            },
        )
        return await self.generate_dj_talk(dj_config, request)

    async def generate_emergency_announcement(
        self, dj_config: DJConfig
    ) -> DJTalkResult:
        """Generate an emergency announcement when playlist is empty."""
        try:
            station_config = self.config_manager.station_config
            station_name = (
                station_config.station_name if station_config else "Radio Station"
            )

            prompt = f"""You are {dj_config.name} on {station_name}.
We're experiencing some technical difficulties with our music system.
Please stand by while we get things sorted out.
Thanks for your patience, and we'll be right back with more great music!"""

            # Generate audio directly
            audio_file = await self.dj_ai._generate_speech(
                prompt,
                dj_config.voice_id,
                1.0,
                getattr(dj_config, "tts_instructions", None),
                getattr(dj_config, "voice_provider", "openai"),
            )

            if audio_file:
                logger.info(f"Generated emergency announcement for {dj_config.name}")
                return DJTalkResult(
                    success=True,
                    text_content=prompt,
                    audio_file=audio_file,
                    talk_category="emergency",
                    style="emergency",
                )
            else:
                return DJTalkResult(
                    success=False, error="Failed to generate emergency audio"
                )

        except Exception as e:
            logger.error(f"Error generating emergency announcement: {e}")
            return DJTalkResult(success=False, error=str(e))
