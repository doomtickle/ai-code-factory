# LOIS voice

A local speech service for the LOIS dashboard. It runs Kokoro-82M in one Docker container, on the
GPU when there is one and on the CPU otherwise, as on an Apple silicon Mac. It listens on `127.0.0.1:8873` only. No text and no audio leave this machine.
It needs no network once built and makes no calls of its own, though the container sits on Docker's default bridge and is not firewalled from it.

## Use

    factory voice build     # first time only: several GB, takes a while
    factory voice start     # or: factory voice start --build
    factory voice status    # running or not, and the device
    factory voice test      # speaks a test passage, silently: does it work, and keep up here?
    factory voice stop      # stops and removes the container, keeps the image

The model takes a few seconds to load after `start`. `status` says when it answers.

## The protocol

`GET /health` returns `{ok, device, voices}`.

`POST /speak` takes JSON: `{text, mix: [[voice, weight], ...], speed, lang}`. `lang` is `a`
(American) or `b` (British). It streams one chunked frame per sentence:

- uint32 LE: sample count
- uint32 LE: lip byte count
- the lip bytes: JSON `{"p": phonemes, "d": durations}`, empty if unavailable
- float32 PCM at 24 kHz

Limits: body up to 8 KiB, text cut at 1200 characters, speed 0.5 to 2, up to 4 voices from the
known list, one generation at a time.

## Who may call it

- `Host` must be exactly `127.0.0.1:8873` or `localhost:8873`.
- A browser's `Origin` must be `http://127.0.0.1:<port>` or `http://localhost:<port>`. That origin
  gets the CORS header. Any other origin gets a 403. No `Origin` at all is allowed, for curl.
- There are no credentials and no cookies. It serves no files. It never logs the text.

Any program or loopback page on this machine can make it speak. It has no key. It can only speak.

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

## What it can't do

- English only. No other Kokoro languages are installed.
- No voice cloning, no speech recognition, no saved audio.
- One voice at a time. A second request waits, and gets a 503 after 30 seconds.
- A cut in the text is silent: past 1200 characters it just stops.
- The x86_64 image is large (about 12 GB on disk) because PyTorch's CUDA build is. A CPU-only
  x86_64 machine still pulls all of it.
- On Apple silicon it runs on the CPU: Docker on a Mac can't reach the GPU. `factory voice build` picks
  `Dockerfile.arm64` by itself. `factory voice test` says whether it keeps up on that machine.
