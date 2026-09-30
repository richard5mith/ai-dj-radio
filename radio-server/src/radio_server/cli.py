"""CLI entry point for the radio server."""

import asyncio

from .main import main


def cli_main():
    """CLI entry point that runs the async main function."""
    asyncio.run(main())


if __name__ == "__main__":
    cli_main()
