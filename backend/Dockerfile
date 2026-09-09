FROM python:3.11-slim

# Optional mirrors for restricted networks (e.g. mainland China):
#   APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
#   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=""
ARG PIP_INDEX_URL="https://pypi.org/simple"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app
RUN if [ -n "$APT_MIRROR" ]; then sed -ri "s|deb\.debian\.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi \
    && apt-get update && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 libgl1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements.lock.txt ./
# Prefer the pinned lock file for reproducible builds; requirements.txt
# (supported ranges) remains the fallback.
RUN if [ -f requirements.lock.txt ]; then \
        pip install --no-cache-dir -r requirements.lock.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi
COPY . ./
EXPOSE 8010
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
