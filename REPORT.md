# Implementation report (initial draft)

## Goal
Create an API that accepts narration text or an HTTPS audio URL, generates a scene-based video, and returns video plus machine-readable metadata.

## Current implementation
- FastAPI endpoints for job creation, job polling, output downloads, health and readiness.
- Basic request validation and in-memory idempotency key handling.
- Script splitting at sentence/newline boundaries.
- Offline TTS for script inputs using eSpeak NG.
- Optional Pexels stock video lookup when configured; generated graphic fallback when unavailable.
- MP4 outputs for requested 16:9, 9:16, and 1:1 ratios, plus SRT, timeline and manifest files.

## Verification status
Not yet verified in a clean environment. Run Docker build, API tests, 10 varied end-to-end inputs, inspect each output, measure relevance, and replace this section with actual commands/results and links to evidence. Do not mark a requirement as passing based on code inspection alone.

## Known limitations
See README.md. In particular, title-card fallback is not B-roll; audio-only speech transcription is missing; caption burn-in, durable job queue/storage, full integration-standard compliance, and comprehensive media-quality checks are not yet implemented.
