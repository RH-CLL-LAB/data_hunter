# Reproducible environment for Data Hunter: Python dependencies,
# Node.js for the JavaScript tests, nothing else. The repository is bind-mounted at
# run time (see compose.yaml), so the image carries no code, data or credentials.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install -r /tmp/requirements.txt

# Default: rebuild the site and run both test suites against the mounted checkout.
CMD ["sh", "-c", "python Scripts/build_dataset_browser.py && python -m unittest discover -s tests && node --test tests/*.cjs"]
