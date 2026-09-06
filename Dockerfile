FROM --platform=$BUILDPLATFORM node:24-bookworm-slim AS web
WORKDIR /build/web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack pnpm install --frozen-lockfile
COPY web/ ./
RUN corepack pnpm build

FROM python:3.14-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 COTRADER_STATIC_DIR=/app/web/dist
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.16 && useradd --uid 10001 --create-home cotrader
COPY pyproject.toml uv.lock ./
COPY src/ src/
RUN uv sync --frozen --no-dev --no-cache
COPY alembic.ini ./
COPY migrations/ migrations/
COPY --from=web /build/web/dist/ web/dist/
USER 10001
ENV PATH=/app/.venv/bin:$PATH
EXPOSE 8000
ENTRYPOINT ["cotrader"]
CMD ["api", "--host", "0.0.0.0"]
