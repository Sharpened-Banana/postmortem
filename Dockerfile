# Dockerfile for the public Mythic Analyzer run-tracker site (site/mythic_site).
#
# Build context MUST be the repo root (not site/) -- this image needs both
# the parent `mythic_analyzer` package (src/, installed via the root
# pyproject.toml) and the site's own code (site/mythic_site/) plus its
# site/requirements.txt. A context scoped to just site/ could not see the
# parent package's source at all. Build from the repo root, e.g.:
#
#   docker build -t mythic-analyzer-site .
#
# This machine has no local Docker install, so this image has NOT been
# built or run locally -- it can only be validated via Fly's remote
# builder (`fly deploy`, which builds remotely when no local Docker
# daemon is available) or on a machine with Docker installed. See
# site/README.md for the honest rundown of what has and hasn't been
# verified.

# ---- builder stage: install both dependency sets into /install ----
FROM python:3.12-slim AS builder

WORKDIR /build

# pyproject.toml declares `readme = "README.md"`, so setuptools needs
# README.md present at build time too, not just pyproject.toml itself.
COPY pyproject.toml README.md ./
COPY src ./src
COPY site/requirements.txt site/requirements.txt

# The base package (mythic-analyzer) has zero unconditional runtime
# dependencies -- see pyproject.toml's [project] section, which has no
# `dependencies = [...]` list, only `optional-dependencies` (dev, site).
# `pip install .` here installs it from source (src/ layout, resolved via
# [tool.setuptools.packages.find] where = ["src"]).
RUN pip install --no-cache-dir --prefix=/install .
RUN pip install --no-cache-dir --prefix=/install -r site/requirements.txt

# ---- final stage: slim runtime image ----
FROM python:3.12-slim

# Non-root user for the app process. Note: Fly volumes are commonly
# root-owned at first mount regardless of what this image's build-time
# chown below sets on /data (a mountpoint's on-disk ownership isn't part
# of the image layer) -- if `mythic_site` can't write to /data after a
# real deploy, either fix ownership once via
# `fly ssh console -C "chown -R appuser:appuser /data"`, or drop the
# USER directive below and run as root. See site/README.md.
RUN useradd --system --create-home --home-dir /app --shell /usr/sbin/nologin appuser

COPY --from=builder /install /usr/local

WORKDIR /app/site
COPY site/mythic_site /app/site/mythic_site

# Makes `mythic_site` importable without it being pip-installed -- it's a
# plain directory, not a package on PyPI/in this image's site-packages.
ENV PYTHONPATH=/app/site

RUN mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8080

CMD ["uvicorn", "mythic_site.app:app", "--host", "0.0.0.0", "--port", "8080"]
