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

# ---------------------------------------------------------------------------
# PostgreSQL 客户端工具（pg_dump / pg_restore / psql）
#
# 为什么必须有：生产形态是 PG，而 `main.py backup` / `restore` 走的是
# pg_dump / psql。镜像里没有它们时，备份与恢复在容器里**直接不可用**，
# 而单元测试看不见 —— tests/test_backup.py 覆盖的是 SQLite 路径
# （VACUUM INTO），PG 那条一直只有"代码看起来对"。
#
# ★ 客户端主版本必须与**服务端一致**（compose 里是 postgres:16）。两个方向都不行：
#   * 客户端比服务端**旧** → pg_dump 拒绝导出更新的服务端；
#   * 客户端比服务端**新** → dump 头里会带上服务端不认识的 GUC。
#     实测（试运行时的恢复演练抓到的）：用 17 的 pg_dump 导 16 的库，
#     dump 里写了 `SET transaction_timeout = 0`（该参数 17 才有），
#     把它恢复到 16 上直接失败：
#       ERROR: unrecognized configuration parameter "transaction_timeout"
#     也就是说**"备份成功"与"能恢复"是两件事**。只验备份不验恢复的话，
#     这个坑会一直躺到真出事那一天。
#
# 所以这里不用基础镜像自带的客户端（Debian 13 带的是 17），
# 而是从 PGDG 装与服务端同版本的 16。
# ARG PG_MAJOR 与 compose 里的 postgres:16-alpine 是**一对**：改一个就得改另一个。
# 写成 ARG 是为了让这层耦合显式可见，而不是散在两处字面量里等人踩。
# ---------------------------------------------------------------------------
ARG PG_MAJOR=16
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
         https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "$VERSION_CODENAME")-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-client-${PG_MAJOR}" \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && pg_dump --version && psql --version

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
