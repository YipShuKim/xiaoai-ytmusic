"""XiaoAi YouTube Music Bridge 启动入口"""

from __future__ import annotations

import logging
import os
import sys

from aiohttp import web

from app.config import AppConfig
from app.web.server import ApplicationServer


def setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main() -> None:
    setup_logging()
    log = logging.getLogger("xiaoai-ytmusic")
    config = AppConfig.load()
    server = ApplicationServer(config)
    app = server.create_web_app()

    log.info(
        f"XiaoAi YouTube Music 服务启动: http://{config.host}:{config.port} "
        f"(局域网音频流地址: {config.base_url()})"
    )
    web.run_app(app, host=config.host, port=config.port, print=None)


if __name__ == "__main__":
    main()
