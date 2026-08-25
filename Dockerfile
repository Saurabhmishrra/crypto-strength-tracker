# Stdlib only. There is nothing to install, so there is no build stage and no
# wheel cache: the image is the interpreter plus one package.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app
COPY terra_cpr/ ./terra_cpr/
COPY pyproject.toml README.md ./

# The loop publishes a snapshot and appends to a transitions history. Mount a
# volume at /data to keep that history across releases; without one the feed
# restarts empty on every deploy, and the feed is the only record of what the
# scanner actually said at the time.
RUN useradd --create-home --uid 10001 scanner \
 && mkdir -p /data \
 && chown -R scanner:scanner /app /data
USER scanner

EXPOSE 8080

# Public data only. No credential, no signing code, no order path -- see the
# safety boundary in README.md. Binding 0.0.0.0 here is deliberate and assumes
# the platform terminates TLS in front of this process.
CMD ["python", "-m", "terra_cpr.cli", "serve", \
     "--output", "/data", "--host", "0.0.0.0", \
     "--live", "--universe", "40"]
