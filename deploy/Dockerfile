FROM python:3.11-slim

# tini gives the scheduler loop a real init so signals and zombies behave.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[gateway,cache]"

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# The generated site lives on a volume so the web container can serve it.
VOLUME ["/site"]

ENV FIREPLANNER_PROVIDER=gateway \
    IB_HOST=ib-gateway \
    IB_PORT=4002 \
    IB_CLIENT_ID=17 \
    OUT_DIR=/site \
    REDACT=--redact \
    SYMBOLS=SPY \
    CORE=0.40

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
