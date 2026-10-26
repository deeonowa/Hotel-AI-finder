# Один образ: собранный фронт + API. Жюри: `docker compose up --build` → http://localhost:8000
# ---------- 1. сборка фронта ----------
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
COPY web/ ./
RUN npm run gen:types && npm run build

# ---------- 2. рантайм ----------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend ./backend
RUN pip install .
COPY data ./data
COPY --from=web /web/dist ./web/dist
ENV DATA_DIR=/app/data WEB_DIST_DIR=/app/web/dist
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1
CMD ["python", "-m", "trip_agent", "serve", "--host", "0.0.0.0", "--port", "8000"]

# ---------- 3. тесты (docker compose --profile test run --rm tests) ----------
FROM runtime AS test
HEALTHCHECK NONE
RUN pip install "pytest>=8" "httpx>=0.27"
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]

# по умолчанию (`docker build .` без --target) собирается рантайм
FROM runtime
