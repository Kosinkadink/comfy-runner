# Thin comfy-runner image for RunPod.
#
# Strategy: minimal image (~1 GB), everything heavy lives on the
# network volume (/workspace).  comfy-runner is cloned from GitHub
# at boot so the image rarely needs rebuilding.
#
# Build:
#   docker build -t ghcr.io/kosinkadink/comfy-runner:latest -f comfy_runner/hosted/Dockerfile .
#
# The entrypoint (startup.sh) handles:
#   1. Clone / update comfy-runner
#   2. Create venv + install requirements
#   3. Start comfy-runner server on 0.0.0.0:9189

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        wget \
        openssh-server \
        python3 \
        python3-venv \
        python3-pip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd

# Copy startup script
COPY comfy_runner/hosted/startup.sh /opt/startup.sh
RUN chmod +x /opt/startup.sh

# Ports: ComfyUI (8188), comfy-runner server (9189), SSH (22)
EXPOSE 8188 9189 22

CMD ["/opt/startup.sh"]
