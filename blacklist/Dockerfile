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

COPY blacklist/crontab /app/blacklist/crontab
COPY blacklist/poll.py /app/blacklist/poll.py

WORKDIR /app
CMD ["/usr/local/bin/supercronic", "-passthrough-logs", "/app/blacklist/crontab"]
