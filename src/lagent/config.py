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

from pydantic import Field
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
    #
    # ⚠️ 默认是 **fail-closed**：用默认值或空值时服务**拒绝启动**
    # （``security.secret_problem()`` + ``InsecureSecretError``），
    # 不再只是打一条告警。理由：一个能伪造管理员身份的密钥，
    # 没有"先跑起来再说"的余地 —— 告警会被忽略，异常不会。
    jwt_secret: str = "dev-insecure-secret-change-me"
    # 显式承认「我就是要用不安全的默认密钥」。默认 false。
    #
    # 为什么留这个开关而不是无条件拒绝：本项目主打"克隆下来就能演示"，
    # 一刀切会把本地演示与 CI 一起打死。关键在于**默认是拒绝**，
    # 于是生产环境不可能"忘了配"—— 它只会因为有人**主动**打开才跑起来，
    # 而那种情况下每次启动都会打一条 CRITICAL（可被告警规则抓到）。
    #
    # 刻意**不**用「按 app_mode 或数据库类型推断是不是生产」这类启发式：
    # 让代码猜环境，等于给误配置留了一条连告警都不会响的路。
    allow_insecure_defaults: bool = False
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

    # ---- 结构化日志与请求关联（P1-3）---------------------------------------
    # 为什么需要它：单机能用 print 排障，量一上来就不行了 —— 一行日志串不起
    # 一条链路（一次 chat 会写审计、可能写门禁流水），也没有机器可读的字段，
    # 于是「最近 5 分钟 5xx 多少」只能靠正则去匹配自由文本。
    log_level: str = "INFO"
    # json : 一行一个 JSON（**默认**，可被机器消费）
    # text : 人类可读单行（本地开发用；在终端里看 JSON 很痛苦，但它不该是默认值）
    log_format: str = "json"
    # 异常堆栈是否进日志。默认关：堆栈几十行，整段塞进一行 JSON 会让日志体积
    # 翻倍，而排查时 90% 只看异常类型与消息。要深查时再打开。
    log_traceback: bool = False

    # ---- 备份（P0-2）-------------------------------------------------------
    # 备份落盘目录。默认放在 var/ 下（与归档一致，整个目录都在 .gitignore 里）。
    # ⚠️ 清扫里的「归档」**不是**备份：它把超过保留期的行导出后从库里删掉，
    #    保护的是日志表无限增长；保护不了「误删表 / 误 UPDATE / 库文件损坏」。
    #    后者才是要有备份的原因，两种动作别混为一谈。
    backup_dir: str = "./var/backup"

    # ---- 后台维护（P0-4）---------------------------------------------------
    # 后台授予资质时，授权记录的默认有效期（天）。
    # 不给默认值就等于"永久有效"，而这套系统从头到尾的立场是
    # **资质必须看有效期**（三年前考的证不等于今天还能用），
    # 所以这里宁可让它到期后需要复训，也不要出现一批永不失效的授权。
    cert_valid_days: int = Field(default=365, ge=1, le=3650)

    # ---- 指标（P1-4）-------------------------------------------------------
    # 总开关。关掉之后 /metrics 返回 404（而不是返回一份空表）——
    # 「功能关掉了」与「开着但一条数据都没有」必须在行为上分得开，
    # 否则抓取端配错了也没人发现。
    metrics_enabled: bool = True
    # 抓取 /metrics 用的预共享密钥（走 X-Metrics-Key 头）。
    #
    # **留空 = 该端点只接受管理员令牌**（与门禁 gate_api_key 同一套默认拒绝逻辑）。
    # 为什么不像 /api/health 那样公开：指标里有**业务量**（QPS、冲突率、
    # 模型调用数），公开等于把容量与增长曲线送出去。
    # 生产里更彻底的做法是把 /metrics 绑到独立的内部端口、由网络策略隔离，
    # 本项目只做到「需要凭据」这一步，边界写在 README 的已知限制里。
    metrics_api_key: str = ""

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
