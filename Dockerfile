FROM node:22-bookworm-slim AS website
WORKDIR /src/sites/lianqi
COPY sites/lianqi/package.json sites/lianqi/pnpm-lock.yaml ./
RUN npm install -g pnpm@10.32.1 && pnpm install --frozen-lockfile
COPY sites/lianqi/ ./
RUN pnpm run build:vps

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=8080

WORKDIR /app
RUN useradd -r -u 1000 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.cloud.tencent.com/pypi/simple \
    --trusted-host mirrors.cloud.tencent.com \
    -r requirements.txt

COPY . .
COPY --from=website /src/public/site /app/public/site
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

CMD ["python", "app.py"]
