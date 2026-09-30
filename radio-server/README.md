# Radio Server

AI-powered streaming radio station server with multiple DJs, scheduled shows, and dynamic content generation.

## Setup

The server runs in Docker. See the root `docker-compose.yml` for configuration.

```bash
docker compose up --build -d
```

## Development

### Linting

The project uses [ruff](https://docs.astral.sh/ruff/) for linting. Configuration is in `pyproject.toml`.

```bash
# Check for issues (from project root)
ruff check radio-server/src/

# Auto-fix what it can
ruff check --fix radio-server/src/
```

All ruff checks must pass before committing.

### Adding Dependencies

The project uses `uv` for dependency management inside the container:

```bash
docker compose exec radio-server uv add <package>
```
