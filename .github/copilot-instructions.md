APPLICATION NOTES

This is a streaming radio station using real music and AI generated speech.

The scheduler is just there to schedule upcoming items.

The radio station is responsible for playing items from that timeline. It does not schedule anything.

There are multiple DJs each of whom have their own show. These are scheduled throughout the day.

A DJ should end their show and hand it off to the DJ on the next scheduled show at the appropriate time.

CODING NOTES

Always check the existing code carefully before making changes. Methods may already exist to do what you want. Or may just need slightly modifying.

Always remove legacy code.

Don't leave commented-out code or unused functions.

Don't leave TODOs or FIXMEs.

Create helper functions for repeated code patterns.

Make sure common code patterns always use the same helper functions.

Ensure all async subprocess calls are safely wrapped to prevent radio stops.

Take things step by step. Don't try and do too much at once.

Don't do half a job. Complete each step fully before moving on.

Run commands in the docker containers using `docker compose exec <service> <command>` to ensure the correct environment. Restart containers if you change the code.

Check logs for errors if something isn't working as expected. `docker logs --tail 200 <container_name>`

The project uses uv in the container. So if you need to add dependencies, use uv commands to add them. You'll also need to rebuild the container after adding dependencies.

The project uses docker compose. So use `docker compose up --build -d` to rebuild and restart containers after making changes.
