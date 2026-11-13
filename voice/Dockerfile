# LOIS's local voice: Kokoro-82M behind voice/server.py. Build from this folder (`factory voice
# build`). Pinned per the repo rule: base image by digest, Python packages by hash
# (requirements.lock), weights and voices by Hugging Face commit and sha256.
#
# No apt packages: espeak-ng comes from the espeakng-loader wheel (library and data), so there is
# nothing unpinned to install. Kokoro needs no git. The image does contain FFmpeg: the av wheel (faster-whisper's
# audio dependency) bundles its own shared libraries, so it is not an apt package either.
FROM python:3.11.18-slim-trixie@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893

# Runtime is offline and read-only: nothing may reach the Hub, and every cache goes to the /tmp
# tmpfs. PYTHONDONTWRITEBYTECODE: site-packages is read-only anyway; pip compiled it at build.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/tmp/hf \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8873 \
    BIND=0.0.0.0 \
    KOKORO_DIR=/opt/kokoro

COPY requirements.lock /build/requirements.lock
# Wheels only, except docopt (num2words needs it; it ships no wheel), built with the setuptools
# and wheel the digest-pinned base image already holds.
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes \
        --only-binary :all: --no-binary docopt --no-build-isolation \
        -r /build/requirements.lock \
    && python -m spacy info en_core_web_sm >/dev/null \
    && rm -rf /build

# The folders first: ADD would give the parents it makes the files' 644, and no one could enter.
RUN mkdir -p /opt/kokoro/voices /opt/whisper/base.en
# hexgrad/Kokoro-82M at a fixed commit (Apache-2.0): the model and the 28 English voices.
ADD --checksum=sha256:5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/config.json /opt/kokoro/config.json
ADD --checksum=sha256:496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/kokoro-v1_0.pth /opt/kokoro/kokoro-v1_0.pth
ADD --checksum=sha256:6d877149dd8b348fbad12e5845b7e43d975390e9f3b68a811d1d86168bef5aa3 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_alloy.pt /opt/kokoro/voices/af_alloy.pt
ADD --checksum=sha256:c03bd1a4c3716c2d8eaa3d50022f62d5c31cfbd6e15933a00b17fefe13841cc4 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_aoede.pt /opt/kokoro/voices/af_aoede.pt
ADD --checksum=sha256:8cb64e02fcc8de0327a8e13817e49c76c945ecf0052ceac97d3081480e8e48d6 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_bella.pt /opt/kokoro/voices/af_bella.pt
ADD --checksum=sha256:0ab5709b8ffab19bfd849cd11d98f75b60af7733253ad0d67b12382a102cb4ff --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_heart.pt /opt/kokoro/voices/af_heart.pt
ADD --checksum=sha256:cdfdccb8cc975aa34ee6b89642963b0064237675de0e41a30ae64cc958dd4e87 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_jessica.pt /opt/kokoro/voices/af_jessica.pt
ADD --checksum=sha256:8bfbc512321c3db49dff984ac675fa5ac7eaed5a96cc31104d3a9080e179d69d --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_kore.pt /opt/kokoro/voices/af_kore.pt
ADD --checksum=sha256:c5561808bcf5250fe8c5f5de32caf2d94f27e57e95befdb098c5c85991d4c5da --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_nicole.pt /opt/kokoro/voices/af_nicole.pt
ADD --checksum=sha256:e0233676ddc21908c37a1f102f6b88a59e4e5c1bd764983616eb9eda629dbcd2 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_nova.pt /opt/kokoro/voices/af_nova.pt
ADD --checksum=sha256:e149459bd9c084416b74756b9bd3418256a8b839088abb07d463730c369dab8f --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_river.pt /opt/kokoro/voices/af_river.pt
ADD --checksum=sha256:49bd364ea3be9eb3e9685e8f9a15448c4883112a7c0ff7ab139fa4088b08cef9 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_sarah.pt /opt/kokoro/voices/af_sarah.pt
ADD --checksum=sha256:c799548aed06e0cb0d655a85a01b48e7f10484d71663f9a3045a5b9362e8512c --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_sky.pt /opt/kokoro/voices/af_sky.pt
ADD --checksum=sha256:ced7e284aba12472891be1da3ab34db84cc05cc02b5889535796dbf2d8b0cb34 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/am_adam.pt /opt/kokoro/voices/am_adam.pt
ADD --checksum=sha256:8bcfdc852bc985fb45c396c561e571ffb9183930071f962f1b50df5c97b161e8 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/am_echo.pt /opt/kokoro/voices/am_echo.pt
ADD --checksum=sha256:ada66f0eefff34ec921b1d7474d7ac8bec00cd863c170f1c534916e9b8212aae --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/am_eric.pt /opt/kokoro/voices/am_eric.pt
ADD --checksum=sha256:98e507eca1db08230ae3b6232d59c10aec9630022d19accac4f5d12fcec3c37a --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/am_fenrir.pt /opt/kokoro/voices/am_fenrir.pt
ADD --checksum=sha256:c82550757ddb31308b97f30040dda8c2d609a9e2de6135848d0a948368138518 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/am_liam.pt /opt/kokoro/voices/am_liam.pt
ADD --checksum=sha256:9a443b79a4b22489a5b0ab7c651a0bcd1a30bef675c28333f06971abbd47bd37 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/am_michael.pt /opt/kokoro/voices/am_michael.pt
ADD --checksum=sha256:e8452be16cd0f6da7b4579eaf7b1e4506e92524882053d86d72b96b9a7fed584 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/am_onyx.pt /opt/kokoro/voices/am_onyx.pt
ADD --checksum=sha256:dd1d8973f4ce4b7d8ae407c77a435f485dabc052081b80ea75c4f30b84f36223 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/am_puck.pt /opt/kokoro/voices/am_puck.pt
ADD --checksum=sha256:7f2f7582fa2b1f160e90aafe6d0b442a685e773608b6667e545d743b073e97a7 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/am_santa.pt /opt/kokoro/voices/am_santa.pt
ADD --checksum=sha256:d292651b6af6c0d81705c2580dcb4463fccc0ff7b8d618a471dbb4e45655b3f3 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/bf_alice.pt /opt/kokoro/voices/bf_alice.pt
ADD --checksum=sha256:d0a423deabf4a52b4f49318c51742c54e21bb89bbbe9a12141e7758ddb5da701 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/bf_emma.pt /opt/kokoro/voices/bf_emma.pt
ADD --checksum=sha256:cdd4c37003805104d1d08fb1e05855c8fb2c68de24ca6e71f264a30aaa59eefd --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/bf_isabella.pt /opt/kokoro/voices/bf_isabella.pt
ADD --checksum=sha256:6e09c2e481e2d53004d7e5ae7d3a325369e130a6f45c35a6002de75084be9285 --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/bf_lily.pt /opt/kokoro/voices/bf_lily.pt
ADD --checksum=sha256:fc3fce4e9c12ed4dbc8fa9680cfe51ee190a96444ce7c3ad647549a30823fc5d --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/bm_daniel.pt /opt/kokoro/voices/bm_daniel.pt
ADD --checksum=sha256:d44935f3135257a9064df99f007fc1342ff1aa767552b4a4fa4c3b2e6e59079c --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/bm_fable.pt /opt/kokoro/voices/bm_fable.pt
ADD --checksum=sha256:f1bc812213dc59774769e5c80004b13eeb79bd78130b11b2d7f934542dab811b --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/bm_george.pt /opt/kokoro/voices/bm_george.pt
ADD --checksum=sha256:b5204750dcba01029d2ac9cec17aec3b20a6d64073c579d694a23cb40effbd0e --chmod=644 https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/bm_lewis.pt /opt/kokoro/voices/bm_lewis.pt

# Systran/faster-whisper-base.en at a fixed commit (MIT): the speech-to-text model POST /listen loads.
ADD --checksum=sha256:f3bc3821e9fc76a27bae538e11ae5b677dcdd352b4600429ce7951d398569aeb --chmod=644 https://huggingface.co/Systran/faster-whisper-base.en/resolve/3d3d5dee26484f91867d81cb899cfcf72b96be6c/config.json /opt/whisper/base.en/config.json
ADD --checksum=sha256:2a166925539a16005f14ff328359f9b9adb9dc4fb631bb3b227526862e93e2ef --chmod=644 https://huggingface.co/Systran/faster-whisper-base.en/resolve/3d3d5dee26484f91867d81cb899cfcf72b96be6c/model.bin /opt/whisper/base.en/model.bin
ADD --checksum=sha256:929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df --chmod=644 https://huggingface.co/Systran/faster-whisper-base.en/resolve/3d3d5dee26484f91867d81cb899cfcf72b96be6c/tokenizer.json /opt/whisper/base.en/tokenizer.json
ADD --checksum=sha256:ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf --chmod=644 https://huggingface.co/Systran/faster-whisper-base.en/resolve/3d3d5dee26484f91867d81cb899cfcf72b96be6c/vocabulary.txt /opt/whisper/base.en/vocabulary.txt

COPY server.py /app/server.py

RUN useradd --system --uid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin kokoro
USER 10001:10001
WORKDIR /app
EXPOSE 8873
CMD ["python", "/app/server.py"]
