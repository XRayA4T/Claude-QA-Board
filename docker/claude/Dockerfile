# Claude Code + web terminal (ttyd + tmux) + board watcher, in one container.
# Built by docker/compose.yaml with the repo as the build context.
FROM node:22-bookworm

ARG USER_UID=1000
ARG USER_GID=1000
ARG WITH_CHROMIUM=0
ARG TTYD_VERSION=1.7.7
# ttyd.x86_64 from the release's SHA256SUMS
ARG TTYD_SHA256=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55

ENV LANG=C.UTF-8 SHELL=/bin/bash

# Tools Claude Code relies on, tmux for the shared session, python3 for the board watcher
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates tmux python3 ripgrep jq less procps unzip openssh-client tzdata \
    && rm -rf /var/lib/apt/lists/*

# Optional headless browser (Debian's Chromium works on amd64 and arm64)
RUN if [ "$WITH_CHROMIUM" = 1 ]; then \
        apt-get update && apt-get install -y --no-install-recommends chromium fonts-liberation fonts-noto-color-emoji \
        && rm -rf /var/lib/apt/lists/*; fi

# ttyd, pinned and checksum-verified (x86_64; on arm64 replace the URL and checksum with ttyd.aarch64)
RUN curl -fsSL -o /usr/local/bin/ttyd \
        "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
    && echo "${TTYD_SHA256}  /usr/local/bin/ttyd" | sha256sum -c - \
    && chmod +x /usr/local/bin/ttyd

# Claude Code
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# A user that owns files the same way you do on the host (replaces the image's 'node' user)
RUN userdel -r node 2>/dev/null || true; \
    getent group "$USER_GID" >/dev/null || groupadd -g "$USER_GID" claude; \
    useradd -m -u "$USER_UID" -g "$USER_GID" -s /bin/bash claude

COPY watcher/board-watch.py docker/claude/entrypoint.sh docker/claude/claude-loop.sh \
     docker/claude/resume-prompt.txt docker/claude/tmux.conf /opt/claude/
RUN chmod 755 /opt/claude/*.sh /opt/claude/board-watch.py

USER claude
WORKDIR /workspace
EXPOSE 7681
ENTRYPOINT ["/opt/claude/entrypoint.sh"]
