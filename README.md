# AI DJ Radio

Your own 24/7 internet radio station: your music collection, presented by AI DJs.

Each DJ has their own show, personality and voice. They introduce songs, read the
weather, chat about the news, give shout-outs to listeners and hand over to the
next DJ when their show ends. You bring the music; the station does the rest.

## Features

- **Scheduled shows** - a daily schedule of DJs, each playing from the music folders you choose
- **AI-written, AI-voiced DJs** - scripts by OpenAI, speech by OpenAI, ElevenLabs or Gemini
- **Real song knowledge** - DJs read your MP3/M4A tags when introducing tracks
- **Weather, news and sponsors** - live weather, RSS headlines and optional sponsor reads
- **Radio production** - crossfades, jingles, stings and music beds under DJ talk
- **Audio and video streams** - HLS audio plus a video stream with artwork and a visualiser
- **Web player** - schedule, now playing and listener count at `http://localhost:3000`
- **Live config reload** - edit any config file and the station picks it up without a restart

## Quick start

You need [Docker](https://docs.docker.com/get-docker/), an
[OpenAI API key](https://platform.openai.com/api-keys) and some music.

```bash
git clone https://github.com/<you>/ai-dj-radio.git
cd ai-dj-radio
./setup.sh
```

`setup.sh` creates your local config from the templates:

| Created                                          | From                         |
| ------------------------------------------------ | ---------------------------- |
| `.env`                                           | `.env.example`               |
| `config/*.json`                                  | `config/*.example.json`      |
| `dj-configs/*.json`                              | `dj-configs/*.example.json`  |
| `music/`, `jingles/`, `beds/`, `ads/`, `temp-audio/` | empty folders            |

Your copies are git-ignored, so pulling updates never overwrites them. Running
`setup.sh` again only creates files that are missing.

Then:

1. **Add your API keys** to `.env`. Only `OPENAI_API_KEY` is required; see
   [API keys](#api-keys).
2. **Name your station** in `config/station.json`: `station_name`, `location`,
   `timezone` and `weather_location`.
3. **Add music.** The example schedule plays from `music/mixed/`, `music/70s/`,
   `music/80s/`, `music/90s/` and `music/00s/`. Put at least a few tracks in each
   (or edit `config/schedule.json` to use your own folder names). See
   [music/README.md](music/README.md).
4. **Start the station:**

   ```bash
   docker compose up --build -d
   ```

5. **Listen** at http://localhost:3000.

The first DJ link takes a little while to generate. Watch progress with
`docker compose logs -f radio-server`.

## API keys

Set these in `.env`, then run `docker compose up -d` to apply changes.

| Key                  | Needed for                                                     | Get one                                                        |
| -------------------- | -------------------------------------------------------------- | -------------------------------------------------------------- |
| `OPENAI_API_KEY`     | **Required.** Writes every DJ script; default voice provider   | https://platform.openai.com/api-keys                           |
| `WEATHER_API_KEY`    | Weather reports (skipped without it)                           | https://openweathermap.org/api (One Call 3.0; free tier is fine) |
| `ELEVENLABS_API_KEY` | Only DJs with `"voice_provider": "elevenlabs"`                 | https://elevenlabs.io                                          |
| `GEMINI_API_KEY`     | Only DJs with `"voice_provider": "gemini"`                     | https://aistudio.google.com/apikey                             |

## Listening

| URL                                        | What                        |
| ------------------------------------------ | --------------------------- |
| http://localhost:3000                      | Web player                  |
| http://localhost:3000/audio/live.m3u8      | Audio HLS stream            |
| http://localhost:3000/video/live.m3u8      | Video HLS stream            |
| http://localhost:8081/api/timeline/upcoming | What's coming up (JSON)    |

The HLS streams play in the web player, Safari, VLC and most media players.

To share streams outside your network behind a password, see
[Password-protected streams](#password-protected-streams).

## Configuration

All config is JSON and reloads automatically when you save it.

### `config/`

| File                     | Controls                                                                  |
| ------------------------ | ------------------------------------------------------------------------- |
| `station.json`           | Station name, location, timezone, crossfade, bitrate, pronunciations      |
| `schedule.json`          | Which DJ is on when, and which music folders each show plays              |
| `dj_prompts.json`        | The prompts behind each kind of DJ talk (weather, trivia, time, ...)      |
| `jingles.json`           | Jingles played before/after particular kinds of DJ talk                   |
| `news_feeds.json`        | RSS feeds the DJs pick headlines from                                     |
| `sponsors.json`          | Sponsor reads (disabled in the examples)                                  |
| `scheduler_weights.json` | How often each kind of talk comes up                                      |
| `song_facts.json`        | Your own facts about tracks and artists for DJs to mention                |
| `listener_*.json`        | Names, places and things used for listener shout-outs                     |
| `video_theme.json`       | Look of the video stream; see [config/README_video_theme.md](config/README_video_theme.md) |

### Station (`config/station.json`)

```json
{
  "station_name": "My Radio",
  "location": "London",
  "timezone": "Europe/London",
  "weather_location": "London,UK",
  "ad_break_interval_minutes": 30,
  "crossfade_duration_seconds": 3.0,
  "stream_bitrate": 128,
  "pronunciations": { "Sade": "Shar-day" }
}
```

`pronunciations` replaces words in DJ scripts before they are spoken, to fix
names the voice gets wrong.

### Schedule (`config/schedule.json`)

```json
{
  "schedule": [
    {
      "start_time": "06:00",
      "end_time": "10:00",
      "dj_name": "morning_mike",
      "music_folders": ["mixed"]
    }
  ]
}
```

- `dj_name` matches a file in `dj-configs/` (`morning_mike` -> `dj-configs/morning_mike.json`).
- `music_folders` are folder names under `music/`.
- Shows can run past midnight (`"start_time": "22:00", "end_time": "06:00"`).
- Cover the whole day; at the end of each show the DJ hands over to the next one.

### DJs (`dj-configs/*.json`)

The examples include five DJs (Morning Mike, Daytime Diana, Afternoon Alex,
Evening Emma, Night Nick) plus a spare, Disco Stu. To add your own, copy one,
rename it, and add it to `config/schedule.json`.

```json
{
  "name": "morning_mike",
  "voice_provider": "openai",
  "voice_id": "ballad",
  "personality_prompt": "You are Morning Mike, an energetic and upbeat morning radio DJ...",
  "tts_instructions": "Speak with high energy and enthusiasm...",
  "announcement_frequency": 0.3,
  "trivia_topics": ["morning routines", "breakfast foods"],
  "talk_schedule": { "time": { "every_minutes": 10 } }
}
```

| Field                    | Meaning                                                                   |
| ------------------------ | ------------------------------------------------------------------------- |
| `personality_prompt`     | Who the DJ is. This shapes everything they say.                           |
| `voice_provider`         | `openai` (default), `elevenlabs` or `gemini`                              |
| `voice_id`               | Voice for that provider (see below)                                       |
| `tts_instructions`       | How to speak: tone, pace, accent (OpenAI and Gemini)                      |
| `announcement_frequency` | 0-1, how often the DJ talks between songs                                 |
| `trivia_topics`          | Subjects for the DJ's trivia links                                        |
| `talk_schedule`          | Talk that happens on a timer, e.g. a time check every 10 minutes          |
| `talk_beds`              | Background music under each kind of talk; see [Music beds](#music-beds)   |

#### Voices

- **OpenAI**: `alloy`, `ash`, `ballad`, `coral`, `echo`, `fable`, `nova`, `onyx`,
  `sage`, `shimmer`, `verse`.
- **ElevenLabs**: `voice_id` is the voice ID from your ElevenLabs voice library.
- **Gemini**: `voice_id` is a Gemini voice name such as `Kore` or `Puck`.
  `speech_speed` (e.g. `1.0`) asks for a faster or slower delivery. See Google's
  [TTS guide](https://ai.google.dev/gemini-api/docs/generate-content/speech-generation).

```json
{
  "voice_provider": "gemini",
  "voice_id": "Kore",
  "tts_instructions": "Speak warmly with a relaxed British radio presenter delivery.",
  "speech_speed": 1.0
}
```

All providers go through the same loudness normalisation, so DJs sit at the same
level as the music.

### Jingles and stings (optional)

Drop audio files into `jingles/global/` (any DJ can use them) or
`jingles/<dj_name>/` (just that DJ) and they are played as stings between
songs. See [jingles/README.md](jingles/README.md) for jingles tied to
particular kinds of talk (e.g. a weather intro).

### Music beds (optional)

Beds are instrumental tracks played quietly under DJ talk. Put them in `beds/`
and reference them by filename. `bed` can be one file or a list to pick from at
random.

Default for a kind of talk, in `config/dj_prompts.json`:

```json
"dj_talk_types": {
  "weather": {
    "prompt": "Give a brief weather update for {location}...",
    "max_words": 50,
    "bed": ["weather_bed_1.mp3", "weather_bed_2.mp3"],
    "bed_volume": 0.18
  }
}
```

Per-DJ override, in `dj-configs/*.json`:

```json
"talk_beds": {
  "weather": "daytime_diana_1.mp3",
  "trivia": ["daytime_diana_1.mp3", "daytime_diana_2.mp3"],
  "time": { "bed": ["daytime_diana_1.mp3"], "bed_volume": 0.15 }
}
```

Missing bed files are skipped, so the example DJ configs work before you add any.

### Password-protected streams

An optional [Caddy](https://caddyserver.com) proxy serves the streams on port
8090 behind HTTP basic auth, which is handy for putting the station on the
internet. In `.env`:

```bash
COMPOSE_PROFILES=streams
CADDY_STREAM_USER=listener
# Generate with: docker run --rm caddy:2-alpine caddy hash-password --plaintext 'your-password'
CADDY_STREAM_PASS_HASH=$2a$14$...
```

Then `docker compose up -d`. If the web player is served from somewhere else,
set `RADIO_STREAM_URL` in `.env` to the public playlist URL.

## Project layout

```text
ai-dj-radio/
├── setup.sh               # First-run setup: seeds config from the examples
├── docker-compose.yml
├── .env.example           # API keys and tuning knobs
├── config/                # Station config (*.example.json are the templates)
├── dj-configs/            # One file per DJ
├── music/                 # Your music, one folder per music_folder
├── jingles/               # Stings and jingles
├── beds/                  # Music beds for under DJ talk
├── radio-server/          # Python: scheduler, DJ generation, audio/video streaming
└── web-interface/         # Node: web player and stream proxy
```

The radio server has two halves. The **scheduler** builds the upcoming timeline
(songs, DJ links, jingles, handovers) ahead of time. The **station** plays that
timeline out as a continuous stream and never schedules anything itself.

## Troubleshooting

Start with the logs: `docker compose logs --tail 300 radio-server`.

| Problem                     | Check                                                                                          |
| --------------------------- | ---------------------------------------------------------------------------------------------- |
| No sound                    | Is there music in every folder the current show uses? Does `curl http://localhost:3000/audio/live.m3u8` return a playlist? |
| `Station config not found`  | Run `./setup.sh` to create `config/*.json` from the examples.                                  |
| DJs never talk              | Is `OPENAI_API_KEY` set? Is the DJ's `announcement_frequency` above 0?                         |
| One DJ is silent            | Their `voice_provider` needs its API key in `.env`.                                            |
| No weather                  | Is `WEATHER_API_KEY` an OpenWeatherMap One Call 3.0 key? Is `weather_location` valid?          |
| Web player won't load       | `docker compose ps`; is something else using port 3000?                                        |

## Development

`docker-compose.yml` mounts the source into the containers, so code changes
only need a restart:

```bash
docker compose restart radio-server        # after Python changes
docker compose restart web-interface       # after server.js changes
docker compose up --build -d               # after dependency changes
```

Front-end files in `web-interface/public/` are served live; just refresh.

Python code is linted with [ruff](https://docs.astral.sh/ruff/) (config in
`radio-server/pyproject.toml`):

```bash
ruff check radio-server/src/
```

Add Python dependencies inside the container with
`docker compose exec radio-server uv add <package>`.

## Contributing

Issues and pull requests are welcome. Please run ruff before opening a PR, and
include steps to reproduce for bugs.

## License

MIT. See [LICENSE](LICENSE).
