# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Optional broker client libraries, e.g. --build-arg EXTRAS="paho-mqtt kafka-python pulsar-client"
ARG EXTRAS=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SIM_HOST=0.0.0.0 \
    SIM_PORT=8000 \
    SIM_SCENARIO=scenario_a_basic_line \
    SIM_CHECKPOINT_DIR=/data/checkpoints

WORKDIR /app

RUN pip install "fastapi>=0.110" "uvicorn[standard]>=0.29" \
    && if [ -n "$EXTRAS" ]; then pip install $EXTRAS; fi

COPY simulator/ ./simulator/
COPY scenarios/ ./scenarios/
COPY web/ ./web/

RUN useradd --system --uid 10001 --home-dir /app sim \
    && mkdir -p /data/checkpoints && chown -R sim /data
USER sim

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"SIM_PORT\"]}/api/sim', timeout=2)" || exit 1

CMD ["python", "-m", "simulator"]
