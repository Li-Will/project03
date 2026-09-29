# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# 企业客服多 Agent 工单系统（Ticket Agent）—— 运行镜像
#
# 构建（默认走 PyPI；国内网络可换源）：
#   docker build -t ticket-agent:0.1.0 .
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t ticket-agent:0.1.0 .
#
# 运行：
#   docker run --rm -p 8000:8000 ticket-agent:0.1.0                       # 起服务；工作台在 http://127.0.0.1:8000/
#   （FAQ 检索需先建索引，见下；未建索引时寒暄/转人工等链路仍可用）
#   docker run --rm ticket-agent:0.1.0 python eval/run_eval.py --mode mock # 容器内评测（不依赖索引）
#   docker volume create ta-data && docker run --rm -v ta-data:/app/data ticket-agent:0.1.0 \
#     python scripts/init_index.py                                        # 建 FAQ 向量索引（需模型，见下）
#
# 设计取舍：
#   * 三阶段构建：frontend(node) → builder(uv) → runtime。运行镜像里**没有 Node、没有 npm、
#     没有前端源码**，只有打包好的 frontend/dist（0.1 MB 级）—— 镜像不因为"好看"而膨胀；
#   * builder 里有 uv/编译器，runtime 只带运行时依赖（不含 pytest/uv）；
#   * 依赖层与源码层分离：改代码不触发重装依赖（lock 不变则缓存命中）；
#   * 非 root 运行（uid 10001）；`/app/data` 归 app —— 业务库、checkpoint、Qdrant
#     索引、评测产物都在这里落地，root 建目录会让非 root 进程静默失败；
#   * HEALTHCHECK 用标准库 urllib，镜像里不装 curl；
#   * 镜像不含 embedding 模型：`scripts/init_index.py` 首次运行需要联网下载（或挂载
#     宿主 HF 缓存：-v ~/.cache/p408qa:/home/app/.cache/p408qa -e HF_HOME=...）。
#     起服务本身不需要模型 —— 留给部署时按需准备，避免镜像几百 MB 的静默膨胀。
# ---------------------------------------------------------------------------

# 基础镜像可覆盖（国内加速器 / 内网仓库）：
#   --build-arg PYTHON_IMAGE=docker.1ms.run/library/python:3.13-slim
ARG PYTHON_IMAGE=python:3.13-slim
ARG NODE_IMAGE=node:22-alpine
# 国内网络可覆盖：--build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=https://registry.npmjs.org

# ---------- 阶段 0：前端产物（工作台）----------
FROM ${NODE_IMAGE} AS frontend

ARG NPM_REGISTRY
WORKDIR /web
# 依赖层与源码层分离：只改前端代码时 npm ci 仍然命中缓存
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry ${NPM_REGISTRY} && npm ci --include=dev --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------- 阶段 1：依赖与项目安装 ----------
FROM ${PYTHON_IMAGE} AS builder

# 国内网络可覆盖为镜像源，例如 https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system

RUN pip install --no-cache-dir "uv==0.12.5"

WORKDIR /app

# 依赖层：只拷依赖描述文件 → lock 不变则命中缓存
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# 项目层：拷源码后装项目自身
COPY src ./src
RUN uv sync --frozen --no-dev

# ---------- 阶段 2：运行时 ----------
FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10001
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    QDRANT_MODE=local \
    QDRANT_PATH=/app/data/qdrant \
    DB_PATH=/app/data/tickets.db \
    SLA_SCAN_SECONDS=300

# 非 root 用户运行；容器里没有需要 root 的动作
RUN useradd --create-home --uid ${APP_UID} app

WORKDIR /app

COPY --from=builder --chown=app:app /opt/venv /opt/venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app eval ./eval
COPY --chown=app:app static ./static
# 前端产物（只有 dist：运行镜像里没有前端源码，也没有 Node）
COPY --from=frontend --chown=app:app /web/dist /app/frontend/dist
COPY --chown=app:app README.md pyproject.toml ./
# 数据目录归 app：业务库 / checkpoint / Qdrant 索引 / 审计都落这里
RUN mkdir -p /app/data && chown -R app:app /app/data

USER app
EXPOSE 8000

# 健康检查：用标准库，镜像里没有 curl（live 探针不查依赖，ready 探针会查 DB+Qdrant）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "project03.api.main:app", "--host", "0.0.0.0", "--port", "8000"]