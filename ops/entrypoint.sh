#!/bin/sh
# Entry point of the container of the food service (Dockerfile, compose.yml).
#
# It runs `food-data` from the source of the clone mounted on /repo, with the
# arguments given (`serve` by default), after having checked what would lose
# data or run the wrong code:
#
# - FOOD_DATA_DIR must be set and be a mount: a directory of the host. Without
#   that mount the databases would be written to the file system of the
#   container and disappear with it; the start is refused instead, never
#   replaced by a directory of the container;
# - the user the container runs as must be able to write there;
# - requirements.txt of the clone must be the one the image was built from: a
#   `git pull` that changes the dependencies needs `docker compose build`.
#
# For `serve`, the token of the writes is read from service.token in the data
# directory, created with a random value the first time (mode 600) and never
# printed. The secret the contributor keys are hashed with (contributor.key,
# mode 600) is created next to it by the service itself.
#
# It is not run by hand: `docker compose up -d` and `docker compose run --rm
# maintenance <command>` go through it. Exit status 78 is a refusal to start.

set -eu

REPO=/repo
DATA="${FOOD_DATA_DIR:-}"
BUILT_FROM=/opt/kratheos-food-data/requirements.txt

# Print why the container does not start, and stop.
refuse() {
    echo "kratheos-food-data: $*" >&2
    exit 78
}

[ -f "$REPO/src/kratheos_food_data/cli.py" ] \
    || refuse "no source in $REPO: mount the clone of the repository there (compose.yml does)."

[ -n "$DATA" ] \
    || refuse "FOOD_DATA_DIR is not set: no directory to keep the data in. Nothing was started."

# Field 5 of mountinfo is the mount point: the data directory must be one.
awk -v dir="$DATA" '$5 == dir { found = 1 } END { exit !found }' /proc/self/mountinfo \
    || refuse "$DATA is not a mount: the data would be written inside the container and lost" \
              "with it. Mount a directory of the host there (FOOD_DATA_DIR in .env, see compose.yml)."

[ -d "$DATA" ] && [ -w "$DATA" ] \
    || refuse "user $(id -u):$(id -g) cannot write in the data directory: give it to that user" \
              "on the host (chown), or set FOOD_DATA_UID and FOOD_DATA_GID to its owner in .env."

if [ -f "$BUILT_FROM" ] && ! cmp -s "$BUILT_FROM" "$REPO/requirements.txt"; then
    refuse "requirements.txt of the clone is not the one this image was built from: run" \
           "'docker compose build', then 'docker compose up -d'."
fi

if [ "${1:-}" = serve ] && [ -z "${FOOD_DATA_TOKEN:-}" ]; then
    TOKEN_FILE="$DATA/service.token"
    if [ ! -s "$TOKEN_FILE" ]; then
        # Written under another name first: a start interrupted here leaves no empty token.
        (
            umask 077
            python -c 'import secrets; print(secrets.token_urlsafe(36))' > "$TOKEN_FILE.new"
            mv "$TOKEN_FILE.new" "$TOKEN_FILE"
        )
        echo "kratheos-food-data: created service.token in the data directory (the token of the writes)." >&2
    fi
    FOOD_DATA_TOKEN="$(cat "$TOKEN_FILE")"
    export FOOD_DATA_TOKEN
fi

exec python -m kratheos_food_data.cli --root "$REPO" "$@"
