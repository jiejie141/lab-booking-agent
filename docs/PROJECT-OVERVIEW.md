# lab-booking-agent 项目概览评估

> 数据来源：对 commit `5d1cb05` 工作区的**实际统计**（`wc -l` / `pytest --collect-only` / `grep`），
> 非估算。评估时间 2026-09-24。

---

## 1. 项目规模

| 项 | 数量 |
|---|---|
| 生产代码 `src/lagent/` | **12,050 行**（含 747 行零依赖控制台 HTML） |
| 测试 `tests/` | **8,760 行 / 633 用例 / 22 个测试文件** |
| 脚本 `scripts/` | 1,046 行（HTTP 冒烟 83 项、清扫端到端、区间重叠竞态） |
| 迁移 `migrations/` | 2 个 revision（`0001` 建 10 表、`0002` 加 `request_id`） |
| 评测集 `eval/` | 14 条用例（意图 / 槽位 / 端到端） |
| **测试 : 生产代码** | **约 0.73 : 1**（这个比例在个人项目里很少见） |
| 数据表 | 10 张 |
| 配置项 | 45 个 `LAB_*` |
| 指标 | 15 个 + 三档健康检查 |
| HTTP 路由 | 19 个 |
| CLI 子命令 | 10 个 |
| 版本 | 1.3.0 |

**依赖只有 9 个直接包**：fastapi / uvicorn / langgraph / sqlalchemy[asyncio] / aiosqlite /
alembic / pydantic / pydantic-settings / tzdata / httpx / pyyaml
（`asyncpg` 与 `chromadb` 为可选，默认注释）。
scrypt 口令、HS256 JWT、BM25+RRF、Prometheus 文本格式、JSON 日志**全部标准库自实现**。

---

## 2. 目录结构与分层

```
main.py                    统一入口（无参=起服务，有参=CLI）
src/lagent/
├── cli.py          726     10 个 CLI 子命令
├── server.py        21     uvicorn 启动
├── api.py         1256     HTTP 层 · 19 路由 · 中间件链 · 三档健康检查
├── config.py               45 个 LAB_* 配置项
├── db.py                   engine / session_scope / 迁移版本校验
├── models.py               10 张表（ORM）
├── schemas.py              pydantic 契约（写接口 extra="forbid"）
├── security.py             scrypt + HS256 + RBAC（零依赖）
├── obs.py                  JSON 单行日志 + request_id 贯穿
├── metrics.py              15 指标 + 注册表（零依赖 Prometheus 文本格式）
├── audit.py                审计（独立事务）
├── ratelimit.py            进程内滑动窗口
├── sweep.py                3 个后台清扫任务 + 循环
├── seed.py / evaluation.py / clock.py / console.py
├── agent/         2009     LangGraph 编排（graph / llm / react_agent / state / tools）
├── domain/        2318     纯业务（availability / negotiate / booking / access）
├── harness/       1016     Agent 运行时（context / runtime / tools / trace）
├── knowledge/      337     BM25 + RRF 融合检索
└── web/index.html  747     零依赖单文件控制台
```

**分层边界是硬约束**：`harness/` **不 import 任何业务模块**
（实测 `grep "^from \.\." src/lagent/harness/*.py` 为空），
且有 AST 静态检查的测试钉住这条边界（`tests/test_harness.py`）。
这样 Agent 运行时可以被抽出来复用到别的项目。

---

## 3. 技术栈

| 层 | 选型 |
|---|---|
| Web | FastAPI + uvicorn（ASGI，全 async 端点） |
| Agent 编排 | **LangGraph** `StateGraph`，7 节点 + 条件边 |
| 模型接入 | 自写 OpenAI 兼容客户端（`httpx`），Mock / Real 可切换 |
| 数据 | SQLAlchemy 2.0 async；SQLite（默认）/ PostgreSQL 16（compose，未实机验证） |
| 迁移 | Alembic（结构变更唯一入口） |
| 校验 | pydantic v2 |
| 前端 | 单文件 HTML，无框架、无构建、无 CDN 依赖 |
| 部署 | Dockerfile + docker-compose（api + postgres） |

---

## 4. 入口文件与调用关系

**入口有两条**：

1. `main.py`（统一入口）→ 无子命令时 → `server.serve()` → `api.create_app()`；
   有子命令时 → `cli.main()`（10 个子命令）。
2. `src/lagent/server.py` —— 只做 `uvicorn.run`，21 行。

**一个预约请求的完整链路**：

```
main.py → server.serve() → api.create_app()
  → 中间件：请求上下文 ▸ 体积校验 ▸ CORS ▸ 认证 ▸ 限流
  → agent/graph.build_agent_from_settings()      ← LangGraph 编排
      parse → (ask | retrieve | negotiate) → book | cancel → compose
  → domain/availability + negotiate + booking    ← 约束判定与下单（纯业务）
  → agent/llm（抽槽/措辞） · knowledge/retriever（BM25+RRF） · harness（工具循环）
  → db.session_scope() → 10 张表
旁路：obs / metrics / audit / ratelimit / sweep（贯穿各层，不参与业务返回）
```

**关键边界**：`api.py` 依赖 `agent / domain / knowledge / metrics / obs` 等；
反向依赖不存在（`domain/`、`harness/` 不 import `api.py`）。
**模型不决定事实也不决定权限** —— 资质、时段冲突、可见范围全由 `domain/` 代码判定，
所以模型换代甚至挂掉，业务结论都不变。

---

## 5. 主要功能与典型使用场景

**场景 A · 师生预约**（对话式）
> 「明天下午两点想用荧光光谱仪两小时」
> → 模型抽槽 → 时段对齐校验 → 六道约束判定 → 找空闲窗口 →
> 冲突则给协商阶梯（挪时间 / 换设备 / 缩短）→ 唯一索引兜底下单 → 生成人话回复

**场景 B · 到实验室刷卡进门**
> 管理员签发凭证 → 门禁核验（资质 / 预约时段 / 人卡一致）→
> 座位占位（容量不变式）→ **单次核销**（截图转发进不去）

**场景 C · 管理员运维**
> 查审计 / 看在馆名单 / 手动释放忘刷出场的座位 / 跑清扫

**场景 D · 工程侧验证**
> `main.py doctor` 环境自检 · `eval` 评测 · `loadtest` 并发压测 · `sweep` 清扫单轮

---

## 6. 能力完成度矩阵

### ✅ 已实现（完整且带测试）

| 能力 | 证据 |
|---|---|
| 时段预约核心 | 粒度对齐、区间重叠（左闭右开）、六道约束判定、协商阶梯 + 接近度打分 |
| **并发安全下单** | 部分唯一索引兜底 + `IntegrityError` 裁决；PG 下 advisory lock；40 并发 ×3 轮**每轮恰好 1 成功** |
| **人员准入 / 门禁** | 4 张表 + 3 接口；未预约拦截、单次核销、房间容量不变式；`access-demo` 8/8 |
| 认证与授权 | scrypt + HS256 JWT + RBAC；`user_id` 一律从令牌取；401/403 严格分开；防账号枚举 |
| 越权防护 | 冒烟 [3][4][5][6]：越权读写 / 改 role / alg=none / 过期令牌全部挡住 |
| 审计 | 独立事务、失败与拒绝也记、`request_id` 贯穿 |
| 后台清扫 | 3 任务（过期预约 / 凭证超时 / 归档）、幂等、失败隔离、先落盘后删除 |
| **可观测** | JSON 单行日志 + `request_id` 跨访问日志/审计/门禁 + `/metrics` 15 指标 + 三档健康检查 |
| 数据库迁移 | Alembic 接管，含「给有数据的表加列」实证 |
| Agent Harness | 双执行模式（deterministic / react）、上下文预算、span、工具注册表 |
| 检索 | 手写 BM25 + RRF 融合（chromadb 可选） |
| 降级链 | react → deterministic → 引导式表单，边界如实标注 |
| 评测 | 14 条用例，意图/槽位/端到端三档 |

### ⚠️ 部分实现

| 能力 | 现状 | 缺口 |
|---|---|---|
| **预约入口** | 只有 `/api/agent/chat` | **无 `POST /api/reservations` 表单接口**，模型不可用时预约整体停摆 |
| 预约状态机 | 4 个状态常量已定义 | 默认直接 `confirmed`，`pending` **无人设置**，无改期/续约 |
| 实验室与设备 | `GET /api/labs` 只读 | 无增删改，全靠 `seed.py` 静态种子 |
| 用户管理 | `GET /api/users` 只读列表 | 无创建/禁用/改角色 |
| 数据生命周期 | 审计 90 天 / 流水 180 天归档 | **无备份与恢复**；归档≠备份 |
| 部署 | compose 配置正确、Dockerfile 可构建（CI 验证） | **`docker compose up` 从未实机跑通**；默认 SQLite 单进程 |
| 限流 | chat 按用户滑动窗口 | 仅单进程，多副本等于配额×副本数 |
| 会话状态 | `SessionStore` 可用 | **在进程内存**，重启即丢 |

### ❌ 缺失（无代码）

| 能力 | 核对方式 |
|---|---|
| **审批流程** | 无审批人/审批动作/审批记录；`STATUS_PENDING` 全仓无人写入 |
| **黑名单与违约规则** | `grep -rin "blacklist\|黑名单\|违约\|penalty\|no_show" src/` **零命中** |
| **通知提醒** | `users.email` 有字段，**无任何发送通道**（无模板/队列/记录） |
| **备份与恢复** | `grep -rin "backup\|备份\|restore" src/ scripts/` **零命中** |
| 分页 | 仅 `/api/audit` 有 limit；`list_reservations` 无上限 |
| 分布式追踪 | 自研 span 是节点级，未接 OpenTelemetry |
| Redis / 消息队列 | 依赖中不存在；长请求（5–30s）无队列化 |
| refresh token | 令牌 2 小时到期需重登 |

---

## 7. 成熟度评级

| 维度 | 评级 | 说明 |
|---|---|---|
| 代码组织与分层 | **优** | 分层清晰、边界有 AST 检查、harness 可独立复用 |
| 测试覆盖 | **优** | 633 用例、测试:生产 ≈ 0.73:1、冒烟 83 项对抗性清单 |
| 并发正确性 | **优** | 不变式压在数据库层，不靠应用层自觉 |
| 安全（越权方向） | **良** | 越权防护扎实；但默认密钥可伪造管理员、登录无限流 |
| 可观测性 | **优** | 日志+指标+三档健康检查，超出一般校内系统水平 |
| 业务功能完整度 | **不足** | 审批/黑名单/通知三块零代码 |
| 部署与运维 | **不足** | 零备份、compose 未实机验证 |
| 规模化能力 | **不足** | 单进程、会话在内存、无分页 |

**一句话定位**：这是一套**工程质量显著高于同类个人项目的 AI Agent 应用**
（并发、越权、可观测三块达到生产级思路），
但作为「院系实验室日常运营系统」还缺几整块业务功能 ——
**它更像一个完成度很高的技术验证平台，而不是一个可以开门营业的系统。**

> 交付就绪度的详细评估与整改清单见 [`DELIVERY-READINESS.md`](DELIVERY-READINESS.md)。
