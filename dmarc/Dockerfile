FROM coldemail-api:v2

# Add parsedmarc + supercronic. We layer on top of the v2 API image so we
# inherit the project tree at /app, paramiko + supabase + click, and the
# matched Python version. Anything that fails to install here also fails
# the API build — same surface = same dep version drift.
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# parsedmarc + mailsuite for IMAP polling. extras=imap pulls in
# IMAPClient + RFC 5322 parser.
RUN pip install --no-cache-dir "parsedmarc[imap]>=8.6" "mailsuite>=1.9"

# supercronic — a single-binary cron that runs as PID 1, logs to stdout,
# survives container restarts. Standard pattern for "one container, cron loop".
ENV SUPERCRONIC_VERSION=v0.2.29
ENV SUPERCRONIC_SHA1SUM=cd48d45c4b10f3f0bfdd3a57d054cd05ac96812b
RUN curl -fsSLO "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" && \
    echo "${SUPERCRONIC_SHA1SUM}  supercronic-linux-amd64" | sha1sum -c - && \
    chmod +x supercronic-linux-amd64 && \
    mv supercronic-linux-amd64 /usr/local/bin/supercronic

COPY dmarc/crontab /app/dmarc/crontab
COPY dmarc/dmarc_poll.py /app/dmarc/dmarc_poll.py

WORKDIR /app
CMD ["/usr/local/bin/supercronic", "-passthrough-logs", "/app/dmarc/crontab"]
