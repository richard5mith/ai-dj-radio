#!/usr/bin/env python3
"""Standalone runner for the radio server."""

import asyncio
import sys
from pathlib import Path

# Add src to path so we can import the package
sys.path.insert(0, str(Path(__file__).parent / "src"))

from radio_server.main import main

if __name__ == "__main__":
    asyncio.run(main())
