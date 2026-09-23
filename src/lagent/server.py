"""服务启动入口（uvicorn）。"""

from __future__ import annotations

import uvicorn

from .config import get_settings


def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        "lagent.api:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    serve()
