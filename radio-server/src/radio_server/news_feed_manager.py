"""
News Feed Manager

Downloads and manages RSS news feeds for DJ news segments.
Fetches feeds on a schedule and provides random headlines for DJ talk.
"""

import asyncio
import contextlib
import json
import logging
import random
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import aiohttp

logger = logging.getLogger(__name__)


class NewsHeadline:
    """Represents a single news headline."""

    def __init__(self, title: str, source: str, link: str = "", pub_date: str = ""):
        self.title = title
        self.source = source
        self.link = link
        self.pub_date = pub_date

    def __repr__(self):
        return f"NewsHeadline(title='{self.title[:50]}...', source='{self.source}')"


class NewsFeedManager:
    """Manages RSS news feeds and provides headlines for DJ segments."""

    def __init__(self, config_path: str = None):
        """
        Initialize news feed manager.

        Args:
            config_path: Path to news_feeds.json config file
        """
        if config_path is None:
            # Check if we're in Docker (file is at /app/config/news_feeds.json)
            docker_path = Path("/app/config/news_feeds.json")
            if docker_path.exists():
                config_path = docker_path
            else:
                # Default to relative path from project root for local development
                base_dir = Path(__file__).parent.parent.parent.parent
                config_path = base_dir / "config" / "news_feeds.json"

        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.headlines: list[NewsHeadline] = []
        self.last_update: datetime | None = None
        self._update_task: asyncio.Task | None = None
        self._running = False

    def _load_config(self) -> dict:
        """Load news feed configuration."""
        try:
            with self.config_path.open() as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning(
                f"News feed config not found at {self.config_path}, using defaults"
            )
            return {
                "rss_feeds": [],
                "update_interval_minutes": 60,
                "max_headlines_per_feed": 10,
                "headlines_to_provide": 5,
            }
        except Exception as e:
            logger.error(f"Error loading news feed config: {e}")
            return {
                "rss_feeds": [],
                "update_interval_minutes": 60,
                "max_headlines_per_feed": 10,
                "headlines_to_provide": 5,
            }

    async def start(self):
        """Start the news feed manager background task."""
        if self._running:
            logger.warning("News feed manager already running")
            return

        self._running = True
        logger.info("🗞️ Starting news feed manager")
        logger.info(f"🗞️ Config: {len(self.config.get('rss_feeds', []))} feeds configured, update interval: {self.config.get('update_interval_minutes', 60)}min")

        # Do initial fetch
        await self.update_feeds()

        # Start background update task
        self._update_task = asyncio.create_task(self._update_loop())

    async def stop(self):
        """Stop the news feed manager."""
        self._running = False
        if self._update_task:
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
        logger.info("🗞️ News feed manager stopped")

    async def _update_loop(self):
        """Background task that updates feeds periodically."""
        update_interval = self.config.get("update_interval_minutes", 60) * 60

        while self._running:
            try:
                await asyncio.sleep(update_interval)
                if self._running:
                    await self.update_feeds()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in news feed update loop: {e}")
                await asyncio.sleep(60)  # Wait a bit before retrying

    async def update_feeds(self):
        """Fetch all enabled RSS feeds and update headlines."""
        feeds = self.config.get("rss_feeds", [])
        enabled_feeds = [f for f in feeds if f.get("enabled", True)]

        if not enabled_feeds:
            logger.info("No enabled news feeds configured")
            return

        logger.info(f"📰 Updating {len(enabled_feeds)} news feed(s)...")
        logger.info(f"📰 Feeds: {[f['name'] for f in enabled_feeds]}")

        new_headlines = []
        max_per_feed = self.config.get("max_headlines_per_feed", 10)

        async with aiohttp.ClientSession() as session:
            for feed in enabled_feeds:
                try:
                    headlines = await self._fetch_feed(
                        session, feed["url"], feed["name"], max_per_feed
                    )
                    new_headlines.extend(headlines)
                    logger.info(
                        f"✅ Fetched {len(headlines)} headlines from {feed['name']}"
                    )
                except Exception as e:
                    logger.error(f"❌ Error fetching {feed['name']}: {e}")

        if new_headlines:
            self.headlines = new_headlines
            self.last_update = datetime.now()
            logger.info(
                f"📰 Updated news: {len(self.headlines)} total headlines available"
            )
        else:
            logger.warning("No headlines fetched from any feed")

    async def _fetch_feed(
        self, session: aiohttp.ClientSession, url: str, source_name: str, max_items: int
    ) -> list[NewsHeadline]:
        """
        Fetch and parse a single RSS feed.

        Args:
            session: aiohttp client session
            url: RSS feed URL
            source_name: Name of the news source
            max_items: Maximum number of items to fetch

        Returns:
            List of NewsHeadline objects
        """
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    logger.error(
                        f"Failed to fetch {source_name}: HTTP {response.status}"
                    )
                    return []

                content = await response.text()
                return self._parse_rss(content, source_name, max_items)

        except TimeoutError:
            logger.error(f"Timeout fetching {source_name}")
            return []
        except Exception as e:
            logger.error(f"Error fetching {source_name}: {e}")
            return []

    def _parse_rss(self, xml_content: str, source_name: str, max_items: int) -> list[NewsHeadline]:
        """
        Parse RSS XML content into NewsHeadline objects.

        Args:
            xml_content: RSS feed XML content
            source_name: Name of the news source
            max_items: Maximum number of items to parse

        Returns:
            List of NewsHeadline objects
        """
        headlines = []

        try:
            root = ET.fromstring(xml_content)

            # Handle both RSS 2.0 and Atom feeds
            # RSS 2.0: <rss><channel><item>
            items = root.findall(".//item")
            if not items:
                # Atom format: <feed><entry>
                items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

            for item in items[:max_items]:
                try:
                    # RSS 2.0 format
                    title_elem = item.find("title")
                    link_elem = item.find("link")
                    pub_date_elem = item.find("pubDate")

                    # Atom format fallback
                    if title_elem is None:
                        title_elem = item.find("{http://www.w3.org/2005/Atom}title")
                    if link_elem is None:
                        link_elem = item.find("{http://www.w3.org/2005/Atom}link")
                        if link_elem is not None:
                            # Atom link is an attribute
                            link_text = link_elem.get("href", "")
                        else:
                            link_text = ""
                    else:
                        link_text = link_elem.text if link_elem.text else ""

                    if pub_date_elem is None:
                        pub_date_elem = item.find("{http://www.w3.org/2005/Atom}updated")

                    title = title_elem.text if title_elem is not None else ""
                    pub_date = pub_date_elem.text if pub_date_elem is not None else ""

                    if title:
                        headline = NewsHeadline(
                            title=title.strip(),
                            source=source_name,
                            link=link_text.strip(),
                            pub_date=pub_date.strip(),
                        )
                        headlines.append(headline)

                except Exception as e:
                    logger.debug(f"Error parsing RSS item: {e}")
                    continue

        except ET.ParseError as e:
            logger.error(f"XML parse error for {source_name}: {e}")
        except Exception as e:
            logger.error(f"Error parsing RSS feed {source_name}: {e}")

        return headlines

    def get_random_headlines(self, count: int = None) -> list[NewsHeadline]:
        """
        Get random headlines for DJ news segment.

        Args:
            count: Number of headlines to return (defaults to config value)

        Returns:
            List of random NewsHeadline objects
        """
        if not self.headlines:
            logger.warning("📰 No headlines available for DJ news segment")
            return []

        if count is None:
            count = self.config.get("headlines_to_provide", 5)

        # Get random sample, but don't exceed available headlines
        sample_size = min(count, len(self.headlines))
        selected = random.sample(self.headlines, sample_size)

        logger.info(f"📰 Selected {len(selected)} random headlines from {len(self.headlines)} available")

        return selected

    def get_headlines_text(self, count: int = None) -> str:
        """
        Get random headlines formatted as text for DJ prompt.

        Args:
            count: Number of headlines to return

        Returns:
            Formatted string of headlines
        """
        headlines = self.get_random_headlines(count)

        if not headlines:
            return "No recent news headlines available."

        formatted = []
        for i, headline in enumerate(headlines, 1):
            formatted.append(f"{i}. {headline.title} (Source: {headline.source})")

        return "\n".join(formatted)

    def has_headlines(self) -> bool:
        """Check if any headlines are available."""
        return len(self.headlines) > 0

    def get_status(self) -> dict:
        """Get status information about the news feed manager."""
        return {
            "running": self._running,
            "headlines_count": len(self.headlines),
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "feeds_configured": len(self.config.get("rss_feeds", [])),
            "feeds_enabled": len(
                [f for f in self.config.get("rss_feeds", []) if f.get("enabled", True)]
            ),
        }

    async def reload(self):
        """Reload news feed configuration and refresh feeds."""
        logger.info("🔄 Reloading news feed configuration...")
        try:
            self.config = self._load_config()
            logger.info(f"✅ News feed config reloaded: {len(self.config.get('rss_feeds', []))} feeds")

            # Refresh feeds with new config
            if self._running:
                await self.update_feeds()
        except Exception as e:
            logger.error(f"❌ Error reloading news feed config: {e}")
