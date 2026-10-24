# IncuBrix PS3 — Script and Voiceover to B-roll Video Engine (MVP starter)

## Important status
This repository is a **starter implementation, not a claim that all IncuBrix must-haves pass**. It provides a FastAPI job API, script-to-scene splitting, offline narration via eSpeak NG, captions/timeline/manifest output, multiple output ratios, and an optional Pexels stock-video adapter. If `PEXELS_API_KEY` is not configured or a stock result fails, the service deliberately falls back to generated title-card graphics. That fallback is not real B-roll and is a known gap against the brief. Audio URL input is supported; the upload endpoint is experimental and must be tested before claiming it works. Audio-only input does not yet transcribe speech.

## Run locally with Docker
1. Install Docker Desktop.
2. Copy `.env.example` to `.env`.
3. Optionally set `PEXELS_API_KEY` in `.env` (never commit the key).
4. Run `docker compose up --build`.
5. Open `http://localhost:8000/docs`.
6. Check `http://localhost:8000/health` and `/ready`.

## Create a job
```bash
curl -X POST http://localhost:8000/api/v1/broll/jobs \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-001' \
  -d '{"script":"Saving money starts with a plan. Track spending every week. Build an emergency fund.","aspect_ratios":["16:9","9:16"],"voice":"en-IN-female","captions":{"burn_in":true,"srt":true}}'
```
Copy the returned `job_id`, then poll `GET /api/v1/jobs/{job_id}`. Download the returned file URLs.

## API routes
- `POST /api/v1/broll/jobs` — script, scenes, or HTTPS audio URL
- `POST /api/v1/broll/jobs/upload` — experimental audio upload
- `GET /api/v1/jobs/{job_id}` — status/progress/result links
- `GET /api/v1/jobs/{job_id}/files/{filename}` — download outputs
- `GET /health`, `GET /ready`
- `GET /docs`, `GET /api/v1/openapi.json`

## Known gaps to close before submission
- Persist job state/idempotency across restarts; current state is in-memory.
- Test and harden audio upload, silent/music-only audio, duration limits, redirects, and concurrent jobs.
- Add robust audio transcription for audio-only inputs and align scene boundaries to actual speech timing.
- Pexels matching is keyword-based, not semantic; review every clip and licence metadata.
- Implement burn-in captions (currently SRT is produced but not burned in), per-scene visual safety checks, and output validation for black/frozen frames and audio drift.
- Add MinIO/S3-compatible storage, durable queue, complete structured error handling, and batch runner.
- Test 10 varied inputs and rate each scene honestly; do not claim Core level until the brief's criteria are verified.
- Update `openapi.yaml` to match any code changes and collect evidence for each submission claim.

## Tests
Run `pytest -q` inside the container or local virtual environment. Tests are a starting point, not complete coverage.
