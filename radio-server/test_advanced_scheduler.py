#!/usr/bin/env python3
"""
Test script for the Advanced Program Scheduler
"""

import asyncio
import logging
from datetime import datetime, timedelta

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

from radio_server.advanced_program_scheduler import AdvancedProgramScheduler
from radio_server.config_manager import ConfigManager
from radio_server.dj_ai import DJAI
from radio_server.music_library import MusicLibrary
from radio_server.weather_service import WeatherService


async def test_advanced_scheduler():
    """Test the advanced program scheduler."""
    print("🎵 Testing Advanced Program Scheduler")

    # Initialize components
    config_manager = ConfigManager()
    config_manager.load_all_configs()

    music_library = MusicLibrary()
    await music_library.initialize(["test"])

    dj_ai = DJAI()
    weather_service = WeatherService()

    # Create advanced scheduler
    scheduler = AdvancedProgramScheduler(
        config_manager, music_library, dj_ai, weather_service
    )

    # Start a mock DJ show
    now = datetime.now()
    show_end = now + timedelta(hours=2)

    scheduler.start_dj_show("morning_mike", now, show_end)

    print(f"🎧 Started show for Morning Mike: {now} to {show_end}")

    # Generate and display 5 segments
    for i in range(5):
        print(f"\n--- Segment {i + 1} ---")

        segment = scheduler.get_next_segment()
        if segment:
            print(f"Type: {segment.segment_type}")
            print(f"DJ: {segment.dj_id}")
            print(f"Duration: {segment.estimated_duration:.1f}s")

            if segment.segment_type == "song_block":
                song_block = segment.content
                print(f"Block Type: {song_block.block_type}")
                print(f"Selection Approach: {song_block.selection_approach}")
                print(f"Songs ({len(song_block.songs)}):")
                for song in song_block.songs:
                    print(f"  - {song.artist} - {song.title} ({song.duration:.1f}s)")

                if song_block.intro_style:
                    print(f"Intro Style: {song_block.intro_style}")
                if song_block.outro_style:
                    print(f"Outro Style: {song_block.outro_style}")
                print(f"Crossover: {song_block.crossover_type}")

            elif segment.segment_type in ["dj_talk", "show_start", "show_end"]:
                talk_segment = segment.content
                print(f"Talk Type: {talk_segment.talk_type}")
                print(f"Prompt Style: {talk_segment.prompt_style}")

                # Test audio generation
                if segment.segment_type == "show_start":
                    print("🎤 Generating show start audio...")
                    success = await scheduler.prepare_audio_for_segment(segment)
                    if success:
                        print(f"✅ Audio generated: {talk_segment.content[:100]}...")
                    else:
                        print("❌ Failed to generate audio")
        else:
            print("❌ No segment generated")

        # Small delay between segments
        await asyncio.sleep(1)

    print("\n🎉 Advanced Scheduler Test Complete!")


if __name__ == "__main__":
    asyncio.run(test_advanced_scheduler())
