FROM coldemail-api:v2

# Add supercronic on top of the v2 API image. We don't need parsedmarc here —
# this sidecar just polls EmailGuard's HTTP API and upserts into Supabase. All
# Python deps it needs (requests, supabase-py) are already in the base image.
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

ENV SUPERCRONIC_VERSION=v0.2.29
ENV SUPERCRONIC_SHA1SUM=cd48d45c4b10f3f0bfdd3a57d054cd05ac96812b
RUN curl -fsSLO "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" && \
    echo "${SUPERCRONIC_SHA1SUM}  supercronic-linux-amd64" | sha1sum -c - && \
    chmod +x supercronic-linux-amd64 && \
    mv supercronic-linux-amd64 /usr/local/bin/supercronic

COPY emailguard/crontab /app/emailguard/crontab
COPY emailguard/poll.py /app/emailguard/poll.py

WORKDIR /app
CMD ["/usr/local/bin/supercronic", "-passthrough-logs", "/app/emailguard/crontab"]
