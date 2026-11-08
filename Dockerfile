FROM debian:bookworm-slim AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    PATH="/opt/venv/bin:${PATH}"

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /app

ADD --checksum=sha256:f699e2f88dca05212f2a452b58475f2993cb6993dfbafb1d0205a3291eb8b4b8 \
    https://ftp.debian.org/debian/pool/main/d/debian-archive-keyring/debian-archive-keyring_2023.3+deb12u2_all.deb \
    /tmp/debian-archive-keyring.deb

RUN dpkg -i /tmp/debian-archive-keyring.deb \
    && rm /tmp/debian-archive-keyring.deb \
    && sed -i \
       -e 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' \
       -e 's|http://security.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' \
       /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && addgroup --system --gid 10001 bot \
    && adduser --system --uid 10001 --ingroup bot --home /app bot

COPY pyproject.toml deploy/install_build_dependencies.py ./
RUN python install_build_dependencies.py && rm install_build_dependencies.py

ADD --checksum=sha256:88817c574c1838a39f88fda0bbd043b4481fd385fb10e92c323e230844d636ce \
    https://cdn.npmmirror.com/binaries/chrome-for-testing/148.0.7778.96/linux64/chrome-headless-shell-linux64.zip \
    /tmp/playwright-headless-shell.zip

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       fonts-liberation \
       fonts-noto-cjk \
       fonts-noto-color-emoji \
       libasound2 \
       libatk-bridge2.0-0 \
       libatk1.0-0 \
       libatspi2.0-0 \
       libcairo2 \
       libcups2 \
       libdbus-1-3 \
       libdrm2 \
       libgbm1 \
       libglib2.0-0 \
       libnspr4 \
       libnss3 \
       libpango-1.0-0 \
       libx11-6 \
       libx11-xcb1 \
       libxcb1 \
       libxcomposite1 \
       libxdamage1 \
       libxext6 \
       libxfixes3 \
       libxkbcommon0 \
       libxrandr2 \
       libxshmfence1 \
       unzip \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/pw-browsers/chromium_headless_shell-1223 \
    && unzip -q /tmp/playwright-headless-shell.zip \
       -d /opt/pw-browsers/chromium_headless_shell-1223 \
    && rm /tmp/playwright-headless-shell.zip \
    && test -x /opt/pw-browsers/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell \
    && chmod -R a+rX /opt/pw-browsers

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

COPY README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --no-build-isolation --no-deps .

# Runtime code and dependencies are read-only.  Only the mount points themselves
# need to be owned by the unprivileged process; recursively chowning /app makes
# image builds needlessly expensive on small servers.
RUN mkdir -p /data /config && chown bot:bot /app /data /config

# The PWA package contains a third-party signer.  Keep it out of the bot/API
# image so it can never execute in a process that receives the full bot .env.
FROM runtime-base AS highlight-controller-runtime
ARG PWA_WHEELHOUSE_URL=
ARG PWA_WHEELHOUSE_TRUSTED_HOST=
COPY deploy/install_build_dependencies.py ./
RUN if [ -n "$PWA_WHEELHOUSE_URL" ]; then \
        PIP_NO_INDEX=1 PIP_FIND_LINKS="$PWA_WHEELHOUSE_URL" \
          PIP_TRUSTED_HOST="$PWA_WHEELHOUSE_TRUSTED_HOST" \
          python install_build_dependencies.py --only-optional-group pwa-demo; \
    else \
        python install_build_dependencies.py --only-optional-group pwa-demo; \
    fi \
    && rm install_build_dependencies.py
USER bot

VOLUME ["/data"]

CMD ["python", "-m", "pwcs2_bot.highlight_controller"]

# Keep this stage last so a plain `docker build .` still produces the bot image.
FROM runtime-base AS bot-runtime
USER bot

EXPOSE 18790
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -m pwcs2_bot.healthcheck || exit 1

CMD ["nanobot", "gateway", "--config", "/config/config.json"]
