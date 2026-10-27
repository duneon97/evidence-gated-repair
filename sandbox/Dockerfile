# Sandbox image for the repair runner.
#
# Build once on the machine that runs the agent:
#     docker build -t repairgate-sandbox:py313 sandbox/
#
# Every test execution then runs with no network and no secrets mounted:
#     docker run --rm --network none \
#         -v "$PWD/candidate:/work" -w /work \
#         repairgate-sandbox:py313 python -m pytest -q
#
# The image is intentionally dumb: a pinned interpreter plus a pinned pytest. Target-project
# dependencies are installed at image-build time from the project's own lock file, never at
# test time, because test time has no network.
FROM python:3.13-slim

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install "pytest==8.3.4" "pytest-json-report==1.5.0"

# Unprivileged user: a patch that escapes the test process must not be root.
RUN useradd --create-home --uid 10001 runner
WORKDIR /work
USER runner

CMD ["python", "-m", "pytest", "-q"]
