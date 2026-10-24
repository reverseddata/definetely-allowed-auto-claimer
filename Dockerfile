FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/reverseddata/definetely-allowed-auto-claimer" \
      org.opencontainers.image.description="BHOPP and BloxyMarket MM2 sniper, run from Discord" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data LOG_DIR=/app/logs
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -r -u 10001 claimer && mkdir -p data logs \
    && chown claimer data logs
USER claimer
CMD ["mm2claimer", "run"]
