# syntax=docker/dockerfile:1

# ===========================================================================
# 阶段 1：构建。把依赖装进独立 venv，最终镜像只拷贝这个 venv，
#         不带 pip 缓存、也不带任何编译期文件。
# ===========================================================================
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 依赖单独一层：requirements.txt 没变时可以直接复用缓存。
# 这里装的全部是有 manylinux wheel 的包（含 opencv-python-headless），
# 所以不需要 gcc / python3-dev，构建阶段也就没有编译工具链。
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# 源码目录必须和运行阶段一致（都是 /app）：下面用的是可编辑安装，
# 安装记录里写的是绝对路径，运行阶段原样复用。
WORKDIR /app
COPY pyproject.toml README.md ./
COPY cexpay ./cexpay
COPY web ./web

# --no-deps：依赖已由 requirements.txt 装好，不再重复解析一遍
# -e     ：cexpay/server.py 用 Path(__file__).resolve().parent.parent / "web"
#          定位前端静态文件。普通安装会把包搬进 site-packages，web/ 就成了
#          site-packages/web（不存在），/ 、/checkout、/admin 三个页面会 404，
#          只剩 API 可用。可编辑安装保持"包与 web/ 同级"的源码树布局。
RUN pip install --no-deps -e .

# ===========================================================================
# 阶段 2：运行。
# ===========================================================================
FROM python:3.12-slim

# 系统库只装一个：
#   libglib2.0-0  opencv-python-headless 的 wheel 里 cv2 会用到
#                   libgthread-2.0.so.0，slim 镜像默认不带。headless 版本不
#                   含 GUI，所以 libGL / libgtk / libsm 一律不需要装；
#                   Pillow 和 numpy 的 wheel 自带 zlib/libjpeg/libopenblas。
#                   健康检查用 python 的 urllib，所以也不装 curl / wget。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行。--no-log-init 避免 useradd 在大 UID 下生成巨大的 lastlog。
RUN useradd --create-home --no-log-init --uid 10001 \
        --shell /usr/sbin/nologin cexpay

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# 数据目录挂到卷上：SQLite 订单库、credentials.json、收款码原图都在这里
# （单独一条 ENV，不把注释塞进上面的续行里，各版本解析器对此的宽容度不一样）
ENV CEXPAY_DATA_DIR=/data

# 目录属主给 cexpay：docker 新建命名卷时会继承镜像里该目录的属主和权限，
# 容器里以非 root 身份也能写 /data/cexpay.sqlite3 和 /data/qr/。
RUN mkdir -p /data && chown -R cexpay:cexpay /data

VOLUME ["/data"]
WORKDIR /app
USER cexpay
EXPOSE 8787

# /api/health 不需要鉴权，返回 200 即视为健康。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=4).status == 200 else 1)"]

# 容器内必须听 0.0.0.0，否则宿主机端口映射进不来；
# 对外暴露范围由 docker-compose.yml 的 127.0.0.1:8787:8787 控制。
CMD ["uvicorn", "cexpay.server:get_app", "--factory", "--host", "0.0.0.0", "--port", "8787"]
