# Image of the food service, for anyone who hosts it without the Debian package.
#
# It holds the code and its Python environment (the exact versions of
# requirements.txt, verified by checksum) and no data. Every file the service
# reads or writes lives in /data, which must be a directory of the host:
#
#   docker build -t kratheos-food-data .
#   docker run --rm -p 127.0.0.1:8750:8750 -v "$PWD/data:/data" \
#       -e FOOD_DATA_TOKEN=a-long-shared-secret kratheos-food-data
#
# No VOLUME is declared on purpose: an undeclared mount would otherwise put the
# databases in an anonymous volume that the next `docker rm` loses. See
# compose.example.yml and the README (Hosting).

FROM python:3.13-slim

# git: the weekly export commits kratheos.sqlite in a clone kept in /data.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt pyproject.toml README.md LICENSE LICENSE-DATA ATTRIBUTION ./
COPY src ./src
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && pip install --no-cache-dir --no-deps . \
    && rm -rf build

# An unprivileged user; compose.example.yml runs as the owner of ./data instead.
RUN useradd --system --uid 10001 --home-dir /data --no-create-home food
USER food
WORKDIR /data

# Inside the container the service listens on every interface: which address
# of the host reaches it is decided by how the port is published.
ENV FOOD_DATA_DIR=/data \
    FOOD_DATA_HOST=0.0.0.0 \
    FOOD_DATA_PORT=8750 \
    HOME=/data \
    PYTHONUNBUFFERED=1
EXPOSE 8750

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8750/v1/health', timeout=4)"]

# --root names the clone of the repository, when one is kept in /data.
ENTRYPOINT ["food-data", "--root", "/data/repository"]
CMD ["serve"]
