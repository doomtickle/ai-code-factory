# LOIS voice

A local speech service for the LOIS dashboard. It runs Kokoro-82M (text to speech) and Whisper through
faster-whisper (speech to text) in one Docker container, on the GPU when there is one and on the CPU
otherwise, as on an Apple silicon Mac. It listens on `127.0.0.1:8873` only. No text and no audio leave this machine.
It needs no network once built and makes no calls of its own, though the container sits on Docker's default bridge and is not firewalled from it.

## Use

    factory voice build     # first time only: several GB, takes a while
    factory voice start     # or: factory voice start --build
    factory voice status    # running or not, and the device
    factory voice test      # speaks a test passage, silently: does it work, and keep up here?
    factory voice stop      # stops and removes the container, keeps the image

The model takes a few seconds to load after `start`. `status` says when it answers.

## The protocol

`GET /health` returns `{ok, device, voices, listen, listen_model}`. `listen` is `true` when the
Whisper model loaded, and `listen_model` names it (`base.en`), or is `null`.

`POST /speak` takes JSON: `{text, mix: [[voice, weight], ...], speed, lang}`. `lang` is `a`
(American) or `b` (British). It streams one chunked frame per sentence:

- uint32 LE: sample count
- uint32 LE: lip byte count
- the lip bytes: JSON `{"p": phonemes, "d": durations}`, empty if unavailable
- float32 PCM at 24 kHz

Limits: body up to 8 KiB, text cut at 1200 characters, speed 0.5 to 2, up to 4 voices from the
known list, one generation at a time.

`POST /listen` takes raw audio: mono, 16 kHz, signed 16-bit little-endian PCM, with
`Content-Type: audio/L16; rate=16000; channels=1` (`channels` may be left out). It returns JSON
`{"text": "...", "ms": 1234}`: the English transcript and the milliseconds it took.

- At most 60 seconds: 1,920,000 bytes. A larger `Content-Length` gets a 413, an empty one a 413, an
  odd one a 400, any other `Content-Type` a 415.
- One transcription at a time, with its own slot, so it never waits behind `/speak`. A second
  request waits, and gets a 503 after 30 seconds.
- A 503 also when the image has no Whisper model (`/health` says `"listen": false`); a 500 if the
  model fails, with no detail.
- No container format and no decoding: the samples go to the model as numbers.

## Who may call it

- `Host` must be exactly `127.0.0.1:8873` or `localhost:8873`.
- A browser's `Origin` must be `http://127.0.0.1:<port>` or `http://localhost:<port>`. That origin
  gets the CORS header. Any other origin gets a 403. No `Origin` at all is allowed, for curl.
- There are no credentials and no cookies. It serves no files. It never logs the text or the audio,
  and keeps neither: both live only for the length of the request.

Any program or loopback page on this machine can make it speak, or send it audio and get the text
back. It has no key. It can only speak and transcribe; it acts on nothing it hears.

## How the container runs

Non-root, read-only root filesystem, all capabilities dropped, `no-new-privileges`, no restart.

`/tmp` is a tmpfs with exec allowed. phonemizer copies `libespeak-ng.so` there and loads the copy.
On a noexec `/tmp` that fails, and words Kokoro does not know go silent. The server refuses to
start if espeak-ng did not load.

## What is pinned

- The base image, by digest.
- All Python packages: exact versions and sha256 hashes in `requirements.lock`, the full
  transitive set. CPython 3.11 on Linux x86_64 only. `requirements-arm64.lock` is the same versions
  for Linux aarch64, without the CUDA packages.
- The spaCy model `en_core_web_sm`, as a hashed wheel in the same file. misaki would otherwise
  download it at runtime.
- The Kokoro weights and the 28 English voices, by Hugging Face commit and sha256, in the
  `Dockerfile`.

- faster-whisper 1.2.1 with CTranslate2, PyAV, onnxruntime, flatbuffers and protobuf, in the same
  lock files. The Whisper model `Systran/faster-whisper-base.en`, by Hugging Face commit
  (`3d3d5dee26484f91867d81cb899cfcf72b96be6c`) and sha256, in both Dockerfiles. The server loads it
  from `/opt/whisper/base.en` (`WHISPER_DIR`, `WHISPER_MODEL`), offline only.

The aarch64 set was resolved across platforms, not installed on aarch64. CTranslate2 on the GPU
with the lock's CUDA 12.8 and cuDNN 9.7 is also unverified; on the CPU it works regardless.

There are no apt packages, so nothing is left unpinned. docopt ships no wheel, so pip builds that
one hash-checked sdist, using the setuptools and wheel in the pinned base image. To change a
version, update `requirements.lock` with its new hashes.

## Licences

- Kokoro-82M weights and the kokoro package: Apache-2.0 (see the model card).
- espeak-ng: GPL-3. It comes inside the image through the `espeakng-loader` wheel, as does the
  GPL-3 `phonemizer-fork`. Neither is vendored in this repo, and the image is built locally, not
  published.
- PyTorch and the NVIDIA libraries in its wheels carry their own licences. The image is local for
  that reason too.
- Whisper weights (OpenAI) and their CTranslate2 conversion (`Systran/faster-whisper-base.en`): MIT.
- faster-whisper: MIT. Its wheel carries the Silero VAD onnx model; that model's licence has not
  been checked.
- CTranslate2: MIT. Its wheel bundles GCC's libgomp (GPL-3 with the runtime exception).
- PyAV: BSD-3, but its wheel bundles FFmpeg 8.1.2 (an LGPLv3 build) whose libavcodec links the
  bundled libx264 and libx265 (GPL-2 or later). Treated like espeak-ng: GPL code inside the
  image, not vendored in this repo, and the image is not published. The wheel also bundles
  opencore-amr, LAME, GnuTLS, nettle, GMP and BSD-licensed codecs.
- onnxruntime (CPU package): MIT. flatbuffers: Apache-2.0. protobuf: BSD-3.

## What it can't do

- English only. No other Kokoro languages are installed.
- English only for listening too: `base.en` is an English-only model.
- No voice cloning, no saved audio, no saved transcripts, no streaming recognition: a whole clip of
  up to 60 seconds goes in, its text comes out.
- It does nothing with a transcript but return it.
- One voice at a time. A second request waits, and gets a 503 after 30 seconds.
- A cut in the text is silent: past 1200 characters it just stops.
- The x86_64 image is large (about 12 GB on disk) because PyTorch's CUDA build is. A CPU-only
  x86_64 machine still pulls all of it.
- On Apple silicon it runs on the CPU: Docker on a Mac can't reach the GPU. `factory voice build` picks
  `Dockerfile.arm64` by itself. `factory voice test` says whether it keeps up on that machine.
