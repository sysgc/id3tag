FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Generate the browsable code reference (see /reference in main.py) from
# this project's docstrings. `docs/` must exist *before* pdoc runs: pdoc
# imports main.py to inspect it, and main.py's own startup code mounts a
# "docs" static folder, which would fail to find a directory that doesn't
# exist yet otherwise.
RUN mkdir -p docs && MUSIC_DIR=/music DATA_DIR=/tmp/pdoc-build python -m pdoc -o docs main matchers tagging

ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
