FROM node:22-bookworm-slim AS website
WORKDIR /src/sites/lianqi
COPY sites/lianqi/package.json sites/lianqi/pnpm-lock.yaml ./
RUN npm install -g pnpm@10.32.1 && pnpm install --frozen-lockfile
COPY sites/lianqi/ ./
RUN node --test tests/dashboard-state.test.mjs
RUN pnpm run build:vps

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=8080

WORKDIR /app
RUN useradd -r -u 1000 appuser

COPY locks/runtime-py311-linux-x86_64.txt /tmp/runtime-requirements.txt
RUN pip install --no-cache-dir --index-url https://pypi.org/simple --require-hashes -r /tmp/runtime-requirements.txt

COPY . .
COPY --from=website /src/public/site /app/public/site
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

CMD ["python", "app.py"]
