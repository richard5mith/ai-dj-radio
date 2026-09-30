import asyncio
import logging

from .config_manager import ConfigManager
from .logger_config import setup_logging
from .radio_station import RadioStation


async def main():
    """Main entry point for the radio server."""
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("Starting radio server...")

    # Initialize configuration
    config_manager = ConfigManager()

    # Create radio station instance
    radio_station = RadioStation(config_manager)

    try:
        # Start the radio station
        await radio_station.start()

        # Keep the server running
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
    except Exception as e:
        logger.error(f"Error in main loop: {e}")
        raise
    finally:
        logger.info("Shutting down radio station...")
        await radio_station.stop()


if __name__ == "__main__":
    asyncio.run(main())
