#!/bin/bash

# Setup script for AI-powered streaming radio station.
# Seeds .env, config/, and dj-configs/ from the *.example templates so the
# checked-in defaults stay clean while you customise locally.

set -e

echo "🎵 Radio station setup 🎵"
echo "======================================"

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker first."
    echo "Visit: https://docs.docker.com/get-docker/"
    exit 1
fi

# Docker Compose v2 is bundled as `docker compose`. Older systems may still
# have `docker-compose`; check for either.
if ! docker compose version &> /dev/null && ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose is not installed. Please install Docker Compose first."
    echo "Visit: https://docs.docker.com/compose/install/"
    exit 1
fi

echo "✅ Docker and Docker Compose are installed"

# Seed .env from template
if [ ! -f .env ]; then
    echo "📝 Creating .env from .env.example..."
    cp .env.example .env
    echo "⚠️  Edit .env to add your API keys (OpenAI, weather, etc.)"
fi

# Seed config/*.json from *.example.json (skip files the user already created)
echo "📝 Seeding config/ from examples..."
for example in config/*.example.json; do
    [ -e "$example" ] || continue
    target="${example%.example.json}.json"
    if [ ! -f "$target" ]; then
        cp "$example" "$target"
        echo "   created $(basename "$target")"
    fi
done

# Seed dj-configs/*.json from *.example.json
echo "📝 Seeding dj-configs/ from examples..."
for example in dj-configs/*.example.json; do
    [ -e "$example" ] || continue
    target="${example%.example.json}.json"
    if [ ! -f "$target" ]; then
        cp "$example" "$target"
        echo "   created $(basename "$target")"
    fi
done

# Create asset directories
echo "📁 Creating asset directories (music/, jingles/, beds/, ads/, temp-audio/)..."
mkdir -p music jingles beds ads temp-audio

# Warn if no music files
music_count=$(find music -name "*.mp3" -o -name "*.m4a" 2>/dev/null | wc -l | tr -d ' ')
if [ "$music_count" -eq 0 ]; then
    echo "⚠️  No music files found under music/."
    echo "   Drop MP3/M4A files into subfolders matching the music_folders in"
    echo "   config/schedule.json (e.g. music/mixed/, music/80s/)."
fi

echo ""
echo "🎯 Next steps:"
echo "==============="
echo "1. Edit .env with your API keys"
echo "2. Edit config/station.json (station name, location, timezone)"
echo "3. Edit dj-configs/*.json to customise DJ personas"
echo "4. Add MP3/M4A files to music/ subdirectories"
echo "5. Run: docker compose up --build -d"
echo ""
echo "📡 The station will be available at:"
echo "   - Web player:      http://localhost:3000"
echo "   - Audio HLS:       http://localhost:8081/audio/live.m3u8"
echo ""
echo "📖 See README.md for detailed documentation"
