# Image of the food service: Python and the dependencies, nothing else.
#
# The code is not in the image. The container runs it from a clone of this
# repository, mounted read-only on /repo (compose.yml): updating the service
# is `git pull` then `docker compose restart`. The image is only built again
# when requirements.txt changes (`docker compose build`); the entry point
# (ops/entrypoint.sh) refuses to start on a clone whose requirements.txt is
# not the one the image was built from.
#
# No data either, and no VOLUME on purpose: every file the service writes
# lives in /data, a directory of the host mounted by compose.yml. A declared
# volume would put the databases in an anonymous volume when the mount is
# forgotten, and the next `docker rm` would lose them; the entry point stops
# instead when /data is not a mount. See the README (Hosting).

FROM python:3.13-slim

# The exact versions of requirements.txt, verified by checksum. A copy stays
# in the image, to compare with the one of the clone at each start.
COPY requirements.txt /opt/kratheos-food-data/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /opt/kratheos-food-data/requirements.txt

# The source is imported from the clone, which is read-only: no bytecode is
# written next to it. HOME is a tmpfs of the container (compose.yml): the user
# the container runs as is the one of the host, unknown to the image.
ENV PYTHONPATH=/repo/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
WORKDIR /repo
EXPOSE 8750

# Never root, even when run without compose.yml (which gives the user of the host).
USER 65534:65534

ENTRYPOINT ["sh", "/repo/ops/entrypoint.sh"]
CMD ["serve"]
