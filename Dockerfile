# ===========================================================================
# 阶段一：构建控制台前端（React + Tailwind）
#
# 为什么要在镜像里构建，而不是把 dist/ 提交进仓库：
#   dist/ 里的文件名带内容哈希，提交进去等于每次改一行样式就多一份几百 KB
#   的二进制噪声 diff，而且**没法审**——你看到的是一串新文件名。
#
# 为什么这一步不能省：
#   api.py 的 GET / 读的是 src/lagent/web/dist/index.html，不是源码。
#   镜像里没有这个目录时，容器起来了、健康检查也绿，但打开控制台是 503 ——
#   "服务健康"与"功能可用"是两件事。CI 的 docker job 会跑
#   scripts/accept_deploy.py，它真的打开页面，所以这种情况在那里会红。
#
# 目录结构刻意与仓库保持一致（/app/src/lagent/web/ui），因为 vite.config.ts
# 里 outDir 写的是相对路径 "../dist"。换个工作目录，产物就会掉到别处去 ——
# 而那种错误的表现是"镜像里没有 dist"，要往回查很久。
# ===========================================================================
FROM node:22-slim AS web

WORKDIR /app/src/lagent/web/ui

# 先只拷依赖清单：package-lock.json 唯一决定依赖树，源码改动不会让它失效
COPY src/lagent/web/ui/package.json src/lagent/web/ui/package-lock.json ./
RUN npm ci

COPY src/lagent/web/ui/ ./
# build = tsc --noEmit && vite build —— 类型错误也会让镜像构建失败，
# 不允许"能跑但类型是错的"前端进镜像
RUN npm run build


# ===========================================================================
# 阶段二：运行镜像
# ===========================================================================
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

# 控制台构建产物来自上一阶段。放在 COPY . . 之后：源码可以随便改，
# 这一层只依赖前端构建结果，命中缓存时不必重跑 npm ci。
COPY --from=web /app/src/lagent/web/dist ./src/lagent/web/dist

# 万一前端构建被跳过（改了 Dockerfile 的 stage 依赖、或有人把上面那行删了），
# 这里让**构建期**就失败，而不是等到有人打开控制台才发现是 503。
RUN test -f src/lagent/web/dist/index.html || \
    (echo "控制台构建产物缺失：src/lagent/web/dist/index.html" && exit 1)

# 非 root 运行
#
# ★ 这里必须**先把运行态产物目录建出来并 chown 给 appuser**，不能只靠 compose 挂卷：
#   Docker 只会在「卷是空的 **且** 镜像里该路径存在」时，才把目录的**内容与属主**
#   一起复制进新卷。`.dockerignore` 排除了 `var/`，所以镜像里原本没有 /app/var ——
#   于是 compose 挂上来的命名卷根目录属主是 **root**，而容器以 appuser(10001) 跑，
#   `main.py backup` 的第一步 mkdir 就炸：
#     PermissionError: [Errno 13] Permission denied: 'var/backup'
#
#   这个坑是"给 api 挂备份卷"这个修复**自己引进来的**，而且是 CI 那道
#   "备份 + 恢复演练"的守门当场抓住的 —— 本机验不出来，因为本机那次
#   跑的是 bind mount（Windows 上权限宽松），不是命名卷。
#   教训：**改挂载方式之后，要用"全新空卷"验一次**，别拿已经跑过的容器验。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/var/backup /app/var/archive \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8200

# 容器里必须绑 0.0.0.0，否则端口映射不到（默认配置是 127.0.0.1，只适合本机跑）
ENV LAB_HOST=0.0.0.0 \
    LAB_PORT=8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8200/api/health', timeout=3).status == 200 else 1)"

CMD ["python", "main.py", "serve"]
