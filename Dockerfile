# imagefree-service —— imagefree.net 的图片生成异步出口
#
# ⚠️ `CMD` 里那对**括号不能省**：目标是**工厂**而不是模块级 `app` 对象。
#    写成 `app.main:app` 会得到 `Failed to find attribute 'app' in 'app.main'`，
#    而单测全绿也照样炸（它们都直接调 `create_app()`）。
#    `tests/test_wiring.py` 里有用例钉这条。
#
# 🔴 与 ../hailuo 的差别：本服务**没有上游凭据**，所以镜像里没有任何 secret 需要注入；
#    但它在**容器出口 IP** 上消耗上游免费额度 ⇒ 别把端口暴露到公网。

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# --- 依赖单独一层：改代码不会让依赖重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- 应用代码
COPY app ./app
COPY gunicorn_conf.py ./
COPY scripts ./scripts
COPY docs ./docs

# --- 非 root 运行
# 任务库默认落在工作目录（SQLite）⇒ 目录必须对运行用户可写。
RUN useradd --create-home --uid 10001 imagefree \
    && mkdir -p /app/data \
    && chown -R imagefree:imagefree /app
USER imagefree

# --- 端口（默认 8400，与 app/config.py 的 Settings.port 一致）
EXPOSE 8400

# --- 存活探针：**零依赖、不触上游、不消耗额度**。
#     刻意用 /healthz 而不是 /readyz —— 后者会查一次任务库。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8400/healthz', timeout=4).status==200 else 1)"

CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:create_app()"]
