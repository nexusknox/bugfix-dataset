# The sandbox each record is verified in. Build from the project root:
#   python -m bugfix.cli build-image
FROM python:3.12-slim

# git to fetch the code; gcc for the occasional test dependency that compiles
RUN apt-get update \
 && apt-get install -y --no-install-recommends git gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

# uv: a fast installer that can ignore packages newer than a date
# (pytest is NOT preinstalled: each record gets the pytest of its own time)
RUN pip install --no-cache-dir uv

# Projects that read their version from git tags can't, in a 1-commit checkout
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 \
    PIP_NO_INPUT=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY bugfix/runner.py /runner/runner.py
WORKDIR /work
CMD ["python", "/runner/runner.py", "/work"]
