# calsync in a container. Build and run with docker-compose.yml (see README, "On a home server").
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY launcher.py ./
COPY app ./app

# Settings and keys live in /app/config, which docker-compose.yml mounts from the host.
VOLUME /app/config
EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8766/', timeout=4)" || exit 1

# --phone: listen inside the container (docker-compose.yml publishes it only on 127.0.0.1);
# --no-open: there is no screen to open a browser on.
CMD ["python", "launcher.py", "--phone", "--no-open"]
