import asyncio
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


class WeatherService:
    """Service for fetching current weather information."""

    def __init__(self):
        self.api_key = os.getenv("WEATHER_API_KEY")
        self.location = os.getenv(
            "WEATHER_LOCATION", "London,GB"
        )  # Use env var or default
        self.weather_url = "http://api.openweathermap.org/data/3.0/onecall"
        self.geocoding_url = "http://api.openweathermap.org/geo/1.0/direct"
        self.cache = {}
        self.geocode_cache = {}  # Cache for geocoding results
        self.cache_duration = 600  # Cache for 10 minutes
        self.geocode_cache_duration = 86400  # Cache geocoding for 24 hours

    def set_location(self, location: str):
        """Set the location for weather queries."""
        self.location = location
        # Clear geocoding cache when location changes
        self.geocode_cache.clear()
        logger.info(f"Weather location set to: {location}")

    async def _geocode_location(self, location: str) -> dict[str, float] | None:
        """Convert location string to latitude/longitude coordinates."""
        # Check cache first
        cache_key = f"geocode_{location}"
        if cache_key in self.geocode_cache:
            cached_data, timestamp = self.geocode_cache[cache_key]
            if (time.time() - timestamp) < self.geocode_cache_duration:
                logger.debug(f"Using cached geocoding data for {location}")
                return cached_data

        try:
            params = {
                "q": location,
                "limit": 1,  # Only need the first result
                "appid": self.api_key,
            }

            logger.debug(f"Geocoding location: {location}")
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(self.geocoding_url, params=params, timeout=10),
            )
            response.raise_for_status()

            data = response.json()

            if not data:
                logger.warning(f"No geocoding results found for location: {location}")
                return None

            result = data[0]  # Take the first result
            coords = {
                "lat": result["lat"],
                "lon": result["lon"],
                "name": result.get("name", location),
                "country": result.get("country", ""),
                "state": result.get("state", ""),
            }

            # Cache the result
            self.geocode_cache[cache_key] = (coords, time.time())

            logger.debug(f"Geocoded {location} to {coords['lat']}, {coords['lon']}")
            return coords

        except requests.exceptions.RequestException as e:
            logger.error(f"Error geocoding location {location}: {e}")
            return None
        except (KeyError, IndexError) as e:
            logger.error(f"Error parsing geocoding data for {location}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error geocoding {location}: {e}")
            return None

    async def get_current_weather(self) -> dict[str, Any] | None:
        """Get current weather information."""
        if not self.api_key:
            logger.warning("No weather API key configured")
            return None

        try:
            # Check cache first
            cache_key = f"weather_{self.location}"
            if cache_key in self.cache:
                cached_data, timestamp = self.cache[cache_key]
                if (time.time() - timestamp) < self.cache_duration:
                    logger.debug("Using cached weather data")
                    return cached_data

            # First, geocode the location to get coordinates
            coords = await self._geocode_location(self.location)
            if not coords:
                logger.error(f"Could not geocode location: {self.location}")
                return None

            # Make weather API request using coordinates
            params = {
                "lat": coords["lat"],
                "lon": coords["lon"],
                "appid": self.api_key,
                "units": "metric",  # Use Celsius
            }

            logger.debug(
                f"Fetching weather for {coords['name']} ({coords['lat']}, {coords['lon']})"
            )
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: requests.get(self.weather_url, params=params, timeout=10)
            )
            response.raise_for_status()

            data = response.json()

            # Extract relevant information
            location_name = coords["name"]
            if coords["state"]:
                location_name += f", {coords['state']}"
            if coords["country"]:
                location_name += f", {coords['country']}"

            logger.debug(data)

            weather_info = {
                "location": location_name,
                "temperature": round(data["current"]["feels_like"]),
                "condition": data["current"]["weather"][0]["description"].title(),
                "humidity": data["current"]["humidity"],
                "wind_speed": data["current"]["wind_speed"],
                "coordinates": {"lat": coords["lat"], "lon": coords["lon"]},
                "raw_data": data,
            }

            # Cache the result
            self.cache[cache_key] = (weather_info, time.time())

            logger.debug(
                f"Weather: {weather_info['condition']}, {weather_info['temperature']}°C in {weather_info['location']}"
            )

            return weather_info

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching weather data: {e}")
            return None
        except KeyError as e:
            logger.error(f"Error parsing weather data: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching weather: {e}")
            return None

    def get_weather_description(self, weather_info: dict[str, Any]) -> str:
        """Get a natural language description of the weather."""
        if not weather_info:
            return ""

        temp = weather_info.get("temperature")
        condition = weather_info.get("condition")
        location = weather_info.get("location")

        if not all([temp, condition, location]):
            return ""

        # Create a natural description
        descriptions = [
            f"It's {condition.lower()} and {temp} degrees in {location}",
            f"Current conditions in {location}: {condition.lower()} with a temperature of {temp} degrees",
            f"The weather in {location} is {condition.lower()}, {temp} degrees",
            f"It's {temp} degrees and {condition.lower()} here in {location}",
        ]

        import random

        return random.choice(descriptions)

    def clear_cache(self):
        """Clear the weather and geocoding caches."""
        self.cache = {}
        self.geocode_cache = {}
        logger.debug("Weather and geocoding caches cleared")
