FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs ca-certificates libnss3 libnspr4 libglib2.0-0 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libx11-6 libcups2t64 fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright==1.62.0 Pillow==11.3.0 \
    && PLAYWRIGHT_BROWSERS_PATH=/opt/browsers playwright install chromium
COPY containers/assets/claude /usr/local/bin/claude
COPY containers/assets/opencode /usr/local/bin/opencode
COPY runtime/launch.py runtime/evaluate_worker.py /opt/bench/
RUN chmod 755 /usr/local/bin/claude /usr/local/bin/opencode \
    && mkdir -p /workspace/output /evidence
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace/output
