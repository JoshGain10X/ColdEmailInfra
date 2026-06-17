FROM coldemail-api:v2

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

# Bundle the bison-deliverability poller + its transitive deps. These are
# copies of the same files in ~/.claude/skills/bison-deliverability/scripts/
# on the dev host; treat the dev-side as source of truth and re-sync if they
# change.
COPY warmup-poller/process_warmup_poller.py /app/warmup-poller/process_warmup_poller.py
COPY warmup-poller/instantly_admin.py /app/warmup-poller/instantly_admin.py
COPY warmup-poller/bison_pull.py /app/warmup-poller/bison_pull.py
COPY warmup-poller/score.py /app/warmup-poller/score.py
COPY warmup-poller/crontab /app/warmup-poller/crontab

WORKDIR /app/warmup-poller
CMD ["/usr/local/bin/supercronic", "-passthrough-logs", "/app/warmup-poller/crontab"]
