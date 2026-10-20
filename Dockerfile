# ==============================================================================
# XiaoAi YouTube Music All-in-One 极简镜像
# 体积优化对比：
#   原 3 项目合计 ~1.9 GB (Chromium + Deno + FFmpeg + Node/Vue + Flutter + PyAV)
#   本整合镜像    ~28 MB 导出包 (基于 python:3.12-alpine + quickjs + aiohttp + yt-dlp)
# ==============================================================================
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="xiaoai-ytmusic" \
      org.opencontainers.image.description="All-in-One Xiaomi AI Speaker YouTube Music Replacement & DLNA Renderer" \
      org.opencontainers.image.version="1.1.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data \
    PORT=8080

WORKDIR /app

# 安装 HTTPS 根证书、时区与轻量级 QuickJS 引擎 (仅 ~1MB，供 yt-dlp 解密 YouTube JS 签名挑战)
COPY requirements.txt ./
RUN apk add --no-cache ca-certificates tzdata quickjs \
    && pip install --no-cache-dir --no-compile --root-user-action=ignore -r requirements.txt \
    && mkdir -p /app/data/cache

COPY app/ ./app/
COPY run.py ./

VOLUME ["/app/data"]

# 8080: Web 管理控制台 + 局域网音频流直链 + 兼容 ytmusic-bridge API + DLNA HTTP
# 1900/udp: DLNA SSDP 局域网多播发现（使用 network_mode: host 时自动生效）
EXPOSE 8080 1900/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/healthz', timeout=4)"

CMD ["python", "run.py"]
