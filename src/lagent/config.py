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
# 执行模式（见 harness/runtime.py）：
#   deterministic  条件边决定调哪个工具（原路径，模型只做语言层）
#   react          模型通过 function calling 自己选工具再决定下一步
ExecutionMode = Literal["deterministic", "react"]


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

    # ---- Agent 运行时（harness）-------------------------------------------
    # 默认仍是 deterministic：它已被 322 条测试与 golden path 覆盖，
    # 改成 react 是一个应当有理由的动作，不该是偷偷换掉的默认值。
    execution_mode: ExecutionMode = "deterministic"
    # 单次模型调用的上下文 token 预算。超限时按 context.py 的优先级裁剪
    # （先丢最旧的工具结果 → 截断摘要 → 丢最旧的对话轮次）。
    context_max_tokens: int = 3000
    # ReAct 循环最多几步。到顶仍未收敛就如实报 "max_steps"，交由上层降级，
    # 而不是硬编一句话假装跑完了。
    react_max_steps: int = 6
    # 是否允许模型触发有副作用的工具（下单/取消）。
    # 默认 False —— 这是安全默认值，改成 True 需要能解释为什么。
    react_allow_side_effects: bool = False
    # 单次工具结果回喂给模型的字符上限，超出截断（见 harness/tools.py）。
    tool_result_limit: int = 1200

    # ---- 人员准入（门禁）--------------------------------------------------
    # 门禁设备调用 /api/access/verify 用的设备密钥。
    #
    # 门禁机不是"某个用户"，发不了用户令牌，所以走一把预共享密钥。
    # **留空 = 该接口只接受管理员令牌**（默认拒绝，不会因为忘了配就敞开）。
    # 这是一把能决定"开不开门"的凭据，必须当密钥管理：定期轮换、按设备下发、
    # 不要和其它系统复用。生产环境应当换成每台设备一把、可单独吊销的设备证书。
    gate_api_key: str = ""

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

    # ---- 边界加固（P0-3）---------------------------------------------------
    # 请求体大小上限（字节）。默认 64KB —— 本项目最大的合法请求体是
    # 一条 chat 消息（上限 2000 字符），64KB 留了两个数量级的余量。
    max_body_bytes: int = 64 * 1024
    # /api/agent/chat 的按用户限流（次/分钟）。0 = 关闭。
    # 这是**模型算力入口**：不设限等于把账算在自己头上。
    rate_limit_per_minute: int = 30
    # CORS 白名单，逗号分隔。**默认空 = 不发 CORS 头 = 只允许同源**。
    # 刻意不用 "*"：带 Authorization 的跨域请求本就不该对任意源开放。
    cors_origins: str = ""
    # 是否写审计日志。默认开 —— 关掉它应当是一个需要解释的动作。
    audit_enabled: bool = True

    # ---- 服务 -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8200

    # ---- 后台清扫（P1-2）--------------------------------------------------
    # 为什么需要它：三件事会随时间自然变坏，而**没有任何用户操作会去修它们** ——
    # 忘刷出场的人一直「在馆」（`uq_permit_one_inside` 让他再也进不了任何房间）、
    # 过期预约一直挂在 confirmed、审计表只追加不清理。
    sweep_enabled: bool = True
    # 清扫间隔。默认 5 分钟：这些状态变化的时间尺度是「小时」，跑太勤只是白烧 CPU。
    sweep_interval_seconds: int = 300
    # 实验楼关门后多久强制收尾。给人留一点离场缓冲，也避免把「刚好在关门时出门」
    # 的人误判成忘刷卡。
    permit_checkout_grace_minutes: int = 30
    # 审计 / 通行流水的保留天数。更早的先归档成 JSONL 再从库里删除。
    # 两者分开配，因为用途不同：审计面向合规（低频），通行流水面向安全（高频）。
    audit_retention_days: int = 90
    access_event_retention_days: int = 180
    archive_dir: str = "./var/archive"
    # 归档单个文件的行数上限：一次导出几百万行会把内存吃光。
    # 超过就分批，每批一个文件。
    archive_batch_size: int = 5000

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的白名单拆成列表（空串 → 空列表 → 不发 CORS 头）。"""
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：清掉单例缓存，让下次 get_settings() 重新读环境变量。"""
    get_settings.cache_clear()
