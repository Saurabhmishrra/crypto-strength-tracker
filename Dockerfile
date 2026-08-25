# Stdlib only. There is nothing to install, so there is no build stage and no
# wheel cache: the image is the interpreter plus one package.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app
COPY terra_cpr/ ./terra_cpr/
COPY pyproject.toml README.md ./

# The loop publishes a snapshot, appends confirmed transitions, and commits the
# five-model completed-bar research archive. Mount a volume at /data to keep both
# histories across releases.
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
