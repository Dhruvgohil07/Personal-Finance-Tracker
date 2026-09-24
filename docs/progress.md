# Progress

## Current phase
Phase 0 — Foundation (plan: docs/plans/phase-0.md)

## Completed
- [x] Step 1: Tooling and infrastructure — pyproject.toml, docker-compose, .env.example, .gitignore

## In progress
- (none)

## Next up
- Step 2a: Config (pydantic-settings)
- Step 2b: Logging with redaction
- Step 2c: Errors
- Step 2d: Money helpers

## Open decisions / questions
- RQ worker on Windows → decided: run as a Docker service (added in Phase 2)
- Schema scope → decided: add tables phase by phase

## My TODO(dhruv) tasks
- [ ] Money parsing + formatting edge-case tests (Step 2d)
- [ ] App version field in /health (Step 4)

## Known issues
- (none)
