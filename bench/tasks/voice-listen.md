---
name: voice-listen
base: dbcec09358f41e7d86a561b45256b20c97092095
reference: f138c3bbacd0ce0483ae0d754a718478b725fe29
tier: full
area: dashboard
---
Work only from commit dbcec09358f41e7d86a561b45256b20c97092095 (`base`). Do not look at any later history, other branches or refs, or the reflog; solve this from the code as it stands at `base`.

Voice: local speech-to-text on the voice server, `POST /listen`.

Goal: owner decision of 12 Nov 2026 reverses "speech-to-text is out": the owner wants to talk to the Coordinator instead of typing, and the audio must never leave the machine. The voice container (`voice/`, `127.0.0.1:8873`) today only speaks (`/speak`, Kokoro). Add `POST /listen`: raw audio in, English text out, using a local Whisper model through faster-whisper (`base.en`). This task is the server side only; the dashboard page that records the microphone is a separate task.

Files: `voice/server.py`, `voice/test_server.py`, `factory/test_voice.py`, `voice/README.md`, `CLAUDE.md` (the voice paragraph), `broker/docs/decisions.md` (a dated decision entry, newest first).

Known facts:
- Request: body is raw 16 kHz, mono, signed 16-bit little-endian PCM, with `Content-Type: audio/L16; rate=16000; channels=1` (`channels` may be omitted). Any other type gets 415. At most 60 s of audio: 1,920,000 bytes; a larger or empty `Content-Length` gets 413, an odd length 400. No container format and no decoder runs on the request: the samples go to the model as numbers.
- Response: JSON `{"text": "...", "ms": <milliseconds taken>}`.
- Same Host/Origin/CORS rules as `/speak`, and still no key and no cookie: it acts on nothing; what is done with a transcript is up to the page.
- One transcription at a time, with its own lock so it never waits behind a `/speak` stream (a `/speak` holds its lock as long as its client takes to read). A request that waits more than 30 s gets 503. At most two requests may wait for the slot; any more get an instant 503 before their body is read, so a pile of callers cannot each hold up to 1.92 MB.
- The server logs neither the audio nor the text and keeps neither; a failed transcription is a 500 with no detail that frees the slot.
- Listening is optional inside the image: if the model is absent or fails to load, the server still speaks, `/listen` answers 503, and `GET /health` says `"listen": false`. `/health` gains `listen` (bool) and `listen_model` (the model name or null).
- The tests have no model and no network: use a fake in place of faster-whisper. The voice server is standard library plus its pinned requirements only; tests must run under `python3 -m unittest discover -s voice` and `factory/test_voice.py` without torch.
- The image carries no faster-whisper or model yet. Do not resolve or edit the lock files or the Dockerfiles (they need hashes from a host download); state in `voice/README.md` and your report that the pins are still to come.

Constraints: no new dependency in the host code. Follow the existing style and limits of `/speak`. Record the owner decision and the choices to review in the decision entry, and update the voice paragraph of `CLAUDE.md`, which still says speech only.

Done when:
- `POST /listen` with valid PCM returns the model's text and a millisecond count; wrong content type, wrong rate or channels, empty, odd-length and over-60-s bodies get the status codes above, with no model call for a short or empty body.
- Two simultaneous requests transcribe one after the other; one that waits past the limit gets 503; a third waiter beyond the two allowed gets an instant 503.
- `/listen` and `/speak` do not wait for each other.
- A failed transcription is a 500 that logs no text and leaves the slot free; nothing about the audio or text is logged or kept.
- Without a model, `/listen` is 503 and `/health` reports `listen: false` while `/speak` still works.
- `voice/README.md` documents the endpoint, its limits and what it never keeps; `CLAUDE.md` and `broker/docs/decisions.md` record the decision.
Tests required: each Done-when item above, in `voice/test_server.py`, with a fake Whisper; plus the hostile case (very large declared length, wrong types) refused before any read. Run `voice/test_server.py` and `factory/test_voice.py`.
Out of scope: the page that records audio, lock files and Dockerfile pins, other languages, streaming recognition, saving transcripts, GPU or aarch64 tuning.
Risk: normal
