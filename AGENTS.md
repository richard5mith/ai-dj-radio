# APPLICATION NOTES

This is a streaming radio station using real music and AI generated speech.
The scheduler is just there to schedule upcoming items.
The radio station is responsible for playing items from that timeline. It does not schedule anything.
There are multiple DJs each of whom have their own show. These are scheduled throughout the day.
A DJ should end their show and hand it off to the DJ on the next scheduled show at the appropriate time.

The compose services are `radio-server` (Python backend) and `web-interface` (Node front-end). Use `docker compose exec radio-server <command>` rather than referring to specific container names — names depend on the local compose project prefix.

# CODING NOTES

### Python

- Write modern Python
- Type hints: Required for all functions and use the latest type hint conventions (e.g. `str | None` not `Optional[str]`, `list[str]` not `List[str]`)
- Fix linting errors
- Imports: Group standard library, third-party, and local imports
- Docstrings: Use triple quotes with description, args, and return values on all functions
- Do not duplicate code
- Do not add unnecessary dependencies
- Structure code for readability, testing and maintainability
- Follow security and performance best practices
- Write comments that explain the code to another developer

### Linting

Always run ruff after making Python changes and fix any issues it finds:

```bash
# Check for issues (from project root)
ruff check radio-server/src/

# Auto-fix what it can
ruff check --fix radio-server/src/
```

Ruff is configured in `radio-server/pyproject.toml`. All checks must pass before committing.

## Other

- Always check the existing code carefully before making changes. Methods may already exist to do what you want. Or may just need slightly modifying.
- Always remove legacy code.
- Don't leave commented-out code or unused functions.
- Don't leave TODOs or FIXMEs.
- Create helper functions for repeated code patterns.
- Make sure common code patterns always use the same helper functions.
- Ensure all async subprocess calls are safely wrapped to prevent radio stops.
- Take things step by step. Don't try and do too much at once.
- Don't do half a job. Complete each step fully before moving on.
- Run commands inside containers with `docker compose exec <service> <command>` (services: `radio-server`, `web-interface`).
- Check logs with `docker compose logs --tail 300 <service>`.
- The project uses uv in the container. So if you need to add dependencies, use uv commands to add them.
- The project uses docker compose. Use `docker compose up --build -d` to rebuild.
