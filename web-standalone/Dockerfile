# MC Repeater Map — stdlib-only Python proxy + static index.html.
# No dependencies, no storage: single small runtime stage.
FROM python:3.13-alpine

# CI (docker/metadata-action) overrides these with the building repo's values.
LABEL org.opencontainers.image.source="https://github.com/khagele/meshcore-triangulator" \
      org.opencontainers.image.description="MeshCore triangulator (web-standalone): static UI + stdlib proxy to mc-radar/meshcore/PDOK"

WORKDIR /app
COPY server.py index.html ./

# Inside the container we must bind all interfaces; restrict exposure when
# publishing the port instead (e.g. -p 127.0.0.1:8000:8000).
ENV HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

RUN adduser -D -H -u 10001 app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"]

ENTRYPOINT ["python", "server.py"]
CMD ["8000"]
