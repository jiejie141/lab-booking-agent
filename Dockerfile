FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依赖单独一层：只改业务代码时不必重装依赖
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && pip install "asyncpg>=0.29.0"

COPY . .

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8200

# 容器里必须绑 0.0.0.0，否则端口映射不到（默认配置是 127.0.0.1，只适合本机跑）
ENV LAB_HOST=0.0.0.0 \
    LAB_PORT=8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8200/api/health', timeout=3).status == 200 else 1)"

CMD ["python", "main.py", "serve"]
