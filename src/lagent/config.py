"""全局配置：冻结的 pydantic-settings 单例。

为什么冻结（frozen=True）：
    上个项目踩过一次「每次请求改写全局 settings 单例」的坑 —— 一个请求改了
    检索后端，后续所有请求都跟着变，测试之间还会互相污染，排查了很久。
    这里直接在模型层禁掉赋值，把「配置不可变」变成编译期就拦得住的约束，
    需要按请求覆盖时用 model_copy(update=...) 造副本。
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

AppMode = Literal["mock", "live", "degraded"]
RetrievalBackend = Literal["bm25", "vector", "hybrid"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LAB_",
        extra="ignore",
        frozen=True,
    )

    app_name: str = "lab-booking-agent"
    timezone: str = "Asia/Shanghai"

    # ---- Agent 模式 -------------------------------------------------------
    # mock     : 离线确定性假模型（默认）。不联网就能跑通全流程与评测。
    # live     : 直连 OpenAI 兼容接口。
    # degraded : 关闭 Agent，退化为「引导式表单」——这是刻意的降级路径，
    #            用来回答「大模型挂了业务还能不能跑」这个问题。
    app_mode: AppMode = "mock"

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout: float = 30.0
    llm_max_retry: int = 2

    # ---- 数据层 -----------------------------------------------------------
    # 本地默认 SQLite，开箱即跑；生产走 docker-compose 里的 PostgreSQL：
    #   postgresql+asyncpg://lab:lab@db:5432/lab
    database_url: str = "sqlite+aiosqlite:///./lab_booking.db"
    db_echo: bool = False
    db_pool_size: int = 20
    db_max_overflow: int = 10

    # ---- 预约并发 ---------------------------------------------------------
    # 乐观并发下最多重试几次；超过就明确回「繁忙请重试」而不是默默失败。
    booking_max_retry: int = 5
    slot_granularity_minutes: int = 30
    # 单次预约最长时长（小时），与设备自身的 max_hours 取小值
    default_max_hours: int = 4

    # ---- 检索 -------------------------------------------------------------
    retrieval_backend: RetrievalBackend = "bm25"
    retrieval_top_k: int = 4
    rrf_k: int = 60

    # ---- 认证与授权（P0-2）-------------------------------------------------
    # HS256 签名密钥。默认值只是为了让本地**开箱即跑**；
    # 它写在仓库里 = 公开的，生产必须用 LAB_JWT_SECRET 覆盖。
    # 服务启动时会检测并打告警（security.uses_default_secret）。
    jwt_secret: str = "dev-insecure-secret-change-me"
    # 访问令牌有效期（分钟）。刻意偏短：本项目没做 refresh token，
    # 与其签一个 7 天的令牌假装很安全，不如 2 小时一续、把风险窗口压小。
    jwt_ttl_minutes: int = 120
    # 密码 KDF 成本参数（scrypt 的 n，必须是 2 的幂）。2**14 ≈ 16MB / 140ms。
    # 调大更抗暴力破解，但每次登录都会等更久；测试里可调小以加速。
    password_kdf_n: int = 2 ** 14

    # ---- 服务 -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8200


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：清掉单例缓存，让下次 get_settings() 重新读环境变量。"""
    get_settings.cache_clear()
