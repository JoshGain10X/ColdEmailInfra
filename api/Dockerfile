FROM python:3.12-slim

# System deps for paramiko (SSH) and curl (Bison API)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl openssh-client gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the entire project (scripts/lib needs to be importable)
COPY . /app/

# Install all deps: parent requirements + API requirements
RUN pip install --no-cache-dir -r requirements.txt -r api/requirements.txt

WORKDIR /app/api

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
