# syntax=docker/dockerfile:1
# Base image pinned by digest (scripts/pin_images.py); Dependabot moves tag and digest together.
FROM python:3.13-slim AS build
WORKDIR /src
# Every dependency, and the hatchling build backend, comes from a hash-locked file:
# a package that isn't listed, or whose hash doesn't match, fails the build.
COPY requirements.txt requirements-build.txt ./
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements-build.txt \
 && pip install --no-cache-dir --require-hashes --only-binary=:all: --prefix=/install -r requirements.txt
COPY pyproject.toml README.md ./
COPY src ./src
# --no-deps: nothing new is resolved here; --no-build-isolation: built with the locked hatchling.
RUN pip install --no-cache-dir --no-deps --no-build-isolation --prefix=/install .

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 pulsewatch && mkdir /data && chown pulsewatch /data
COPY --from=build /install /usr/local
USER pulsewatch
WORKDIR /home/pulsewatch
ENV PULSEWATCH_DB_PATH=/data/pulsewatch.db \
    PULSEWATCH_TARGETS_FILE=/config/targets.yaml \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1
ENTRYPOINT ["pulsewatch"]
