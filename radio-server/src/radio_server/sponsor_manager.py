"""
Sponsor Manager

Manages sponsor selection and content for DJ sponsor segments.
Supports global sponsors and DJ-specific sponsor overrides with weighted selection.
"""

import json
import logging
import random
from pathlib import Path

logger = logging.getLogger(__name__)


class Sponsor:
    """Represents a sponsor with their content and metadata."""

    def __init__(
        self,
        name: str,
        style: str,
        weight: float = 1.0,
        script: str = None,
        talking_points: str = None,
        website: str = None,
        enabled: bool = True,
    ):
        self.name = name
        self.style = style  # "read" or "vamp"
        self.weight = weight
        self.script = script  # For "read" style
        self.talking_points = talking_points  # For "vamp" style
        self.website = website  # For "vamp" style
        self.enabled = enabled

    def __repr__(self):
        return f"Sponsor(name='{self.name}', style='{self.style}', weight={self.weight})"


class SponsorManager:
    """Manages sponsors and provides weighted selection for DJ segments."""

    def __init__(self, config_path: str = None):
        """
        Initialize sponsor manager.

        Args:
            config_path: Path to sponsors.json config file
        """
        if config_path is None:
            # Check if we're in Docker (file is at /app/config/sponsors.json)
            docker_path = Path("/app/config/sponsors.json")
            if docker_path.exists():
                config_path = docker_path
            else:
                # Default to relative path from project root for local development
                base_dir = Path(__file__).parent.parent.parent.parent
                config_path = base_dir / "config" / "sponsors.json"

        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.global_sponsors = self._parse_sponsors(
            self.config.get("global_sponsors", [])
        )
        self.dj_sponsors = {}  # DJ ID -> List[Sponsor]

        # Parse DJ-specific sponsors
        dj_overrides = self.config.get("dj_overrides", {})
        for dj_id, sponsors_data in dj_overrides.items():
            self.dj_sponsors[dj_id] = self._parse_sponsors(sponsors_data)

        logger.info(
            f"💰 Sponsor Manager initialized: {len(self.global_sponsors)} global sponsors, "
            f"{len(self.dj_sponsors)} DJs with custom sponsors"
        )

    def _load_config(self) -> dict:
        """Load sponsor configuration from file."""
        try:
            logger.info(f"💰 Loading sponsor config from: {self.config_path}")
            with self.config_path.open() as f:
                config = json.load(f)
                logger.info(f"💰 Loaded config with {len(config.get('global_sponsors', []))} global sponsors")
                return config
        except FileNotFoundError:
            logger.warning(
                f"Sponsor config not found at {self.config_path}, using empty config"
            )
            return {"global_sponsors": [], "dj_overrides": {}}
        except Exception as e:
            logger.error(f"Error loading sponsor config: {e}")
            return {"global_sponsors": [], "dj_overrides": {}}

    def _parse_sponsors(self, sponsors_data: list[dict]) -> list[Sponsor]:
        """Parse sponsor data into Sponsor objects."""
        logger.info(f"💰 Parsing {len(sponsors_data)} sponsor entries")
        sponsors = []
        for data in sponsors_data:
            try:
                sponsor = Sponsor(
                    name=data.get("name", "Unknown Sponsor"),
                    style=data.get("style", "read"),
                    weight=data.get("weight", 1.0),
                    script=data.get("script"),
                    talking_points=data.get("talking_points"),
                    website=data.get("website"),
                    enabled=data.get("enabled", True),
                )
                if sponsor.enabled:
                    sponsors.append(sponsor)
                    logger.info(f"💰 Added enabled sponsor: {sponsor.name} (style={sponsor.style})")
                else:
                    logger.info(f"💰 Skipped disabled sponsor: {sponsor.name}")
            except Exception as e:
                logger.error(f"Error parsing sponsor {data.get('name', 'unknown')}: {e}")

        logger.info(f"💰 Parsed {len(sponsors)} enabled sponsors")
        return sponsors

    def get_sponsor(self, dj_id: str, requested_style: str = None) -> Sponsor | None:
        """
        Get a sponsor for a DJ segment using weighted selection.

        Args:
            dj_id: DJ identifier
            requested_style: "sponsor_read" or "sponsor_vamp" (or None for any)

        Returns:
            Selected Sponsor object or None if no sponsors available
        """
        # If DJ has specific sponsors, use only those (ignore global)
        if dj_id in self.dj_sponsors and self.dj_sponsors[dj_id]:
            available_sponsors = self.dj_sponsors[dj_id]
            logger.info(f"💰 Using {len(available_sponsors)} DJ-specific sponsors for {dj_id}")
        else:
            available_sponsors = self.global_sponsors
            logger.info(f"💰 Using {len(available_sponsors)} global sponsors for {dj_id}")

        if not available_sponsors:
            logger.warning(f"💰 No sponsors available for {dj_id}")
            return None

        # Filter by style if requested
        if requested_style:
            # Convert talk type to sponsor style
            sponsor_style = "read" if requested_style == "sponsor_read" else "vamp"
            filtered = [s for s in available_sponsors if s.style == sponsor_style]

            if not filtered:
                logger.warning(
                    f"💰 No sponsors with style '{sponsor_style}' available for {dj_id}, "
                    f"using any style"
                )
                filtered = available_sponsors

            available_sponsors = filtered

        # Weighted random selection
        sponsors = available_sponsors
        weights = [s.weight for s in sponsors]

        try:
            selected = random.choices(sponsors, weights=weights, k=1)[0]
            logger.info(f"💰 Selected sponsor: {selected.name} (style={selected.style}, weight={selected.weight})")
            return selected
        except Exception as e:
            logger.error(f"Error selecting sponsor: {e}")
            return None

    def get_sponsor_context(self, sponsor: Sponsor) -> dict[str, str]:
        """
        Get context dictionary for sponsor to be used in prompt generation.

        Args:
            sponsor: Sponsor object

        Returns:
            Dictionary with sponsor context for prompt formatting
        """
        context = {
            "sponsor_name": sponsor.name,
        }

        if sponsor.style == "read" and sponsor.script:
            context["sponsor_script"] = sponsor.script
        elif sponsor.style == "vamp":
            context["sponsor_talking_points"] = sponsor.talking_points or "No talking points provided"
            context["sponsor_website"] = sponsor.website or ""

        return context

    def has_sponsors(self, dj_id: str = None) -> bool:
        """
        Check if any sponsors are available.

        Args:
            dj_id: Optional DJ ID to check for specific sponsors

        Returns:
            True if sponsors available
        """
        if dj_id and dj_id in self.dj_sponsors:
            return len(self.dj_sponsors[dj_id]) > 0

        return len(self.global_sponsors) > 0

    def get_status(self, dj_id: str = None) -> dict:
        """
        Get status information about sponsors.

        Args:
            dj_id: Optional DJ ID for DJ-specific status

        Returns:
            Status dictionary
        """
        if dj_id and dj_id in self.dj_sponsors:
            sponsors = self.dj_sponsors[dj_id]
            source = f"DJ-specific ({dj_id})"
        else:
            sponsors = self.global_sponsors
            source = "Global"

        read_count = sum(1 for s in sponsors if s.style == "read")
        vamp_count = sum(1 for s in sponsors if s.style == "vamp")

        return {
            "source": source,
            "total_sponsors": len(sponsors),
            "read_sponsors": read_count,
            "vamp_sponsors": vamp_count,
            "sponsors": [s.name for s in sponsors],
        }

    def reload(self):
        """Reload sponsor configuration from file."""
        logger.info("🔄 Reloading sponsor configuration...")
        try:
            self.config = self._load_config()
            self.global_sponsors = self._parse_sponsors(
                self.config.get("global_sponsors", [])
            )
            self.dj_sponsors = {}

            # Parse DJ-specific sponsors
            dj_overrides = self.config.get("dj_overrides", {})
            for dj_id, sponsors_data in dj_overrides.items():
                self.dj_sponsors[dj_id] = self._parse_sponsors(sponsors_data)

            logger.info(
                f"✅ Sponsor config reloaded: {len(self.global_sponsors)} global sponsors, "
                f"{len(self.dj_sponsors)} DJs with custom sponsors"
            )
        except Exception as e:
            logger.error(f"❌ Error reloading sponsor config: {e}")
