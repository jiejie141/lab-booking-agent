# lab-booking-agent 项目概览

> 数据来源：对 commit `2d887aa` 工作区的**实际统计**（`wc -l` / `pytest --collect-only` /
> `grep` / GitHub Actions API），非估算。本次更新 2026-09-26。
>
> **历史版本**：本文初版基于 `5d1cb05`（2026-09-24），当时把「审批 / 黑名单与违约 /
> 通知 / 备份 / 分页」列在**缺失**一栏。那份评估的原文与逐项整改结果保留在
> [`DELIVERY-READINESS.md`](DELIVERY-READINESS.md)（含一节「复评」和两处**我自己的误判更正**）。
> 本文只描述**当前状态**。

---

## 1. 项目规模

| 项 | 数量 | 相比 `5d1cb05` |
|---|---|---|
| 生产代码 `src/lagent/` | **15,595 行**（含 849 行零依赖控制台 HTML） | +3,545 |
| 测试 `tests/` | **12,370 行 / 848 用例 / 31 个文件** | +3,610 行 / +215 用例 |
| 脚本 `scripts/` | 1,412 行（5 个：HTTP 冒烟、清扫端到端、区间竞态、CI 注解包装、**部署验收**） | +366 |
| 迁移 `migrations/` | **5 个 revision** | +3 |
| 评测集 `eval/` | 14 条用例（意图 / 槽位 / 端到端） | — |
| **测试 : 生产代码** | **约 0.79 : 1** | ↑ |
| 数据表 | **11 张** | +1 |
| 配置项 | **62 个 `LAB_*`**（55 项写进 `.env.example`） | +17 |
| 指标 | **16 个** + 三档健康检查 | +1 |
| HTTP 路由 | **33 个** | +14 |
| CLI 子命令 | **13 个** | +3 |
| CI job | **4 个**（quality 双版本矩阵 → 5 个检查项） | +1 |
| 版本 | 1.3.0 | — |

**依赖只有 13 个直接包**（含 2 个测试用）：fastapi / uvicorn / langgraph /
sqlalchemy[asyncio] / aiosqlite / alembic / pydantic / pydantic-settings / tzdata /
httpx / pyyaml / pytest / pytest-asyncio。
`asyncpg` 与 `chromadb` 为可选，默认注释。
scrypt 口令、HS256 JWT、BM25+RRF、Prometheus 文本格式、JSON 日志**全部标准库自实现**。

---

## 2. 目录结构与分层

```
main.py                    统一入口（无参=起服务，有参=CLI）
src/lagent/
├── cli.py                 13 个 CLI 子命令
├── server.py              uvicorn 启动
├── api.py                  HTTP 层 · 33 路由 · 中间件链 · 三档健康检查
├── config.py              62 个 LAB_* 配置项
├── db.py                  engine / session_scope / 迁移版本校验 / 结构漂移检查
├── models.py              11 张表（ORM）
├── schemas.py             pydantic 契约（写接口 extra="forbid"）
├── security.py            scrypt + HS256 + RBAC（零依赖）
├── obs.py                 JSON 单行日志 + request_id 贯穿
├── metrics.py             16 指标 + 注册表（零依赖 Prometheus 文本格式）
├── audit.py               审计（独立事务）
├── ratelimit.py           限流 + 登录失败锁定（进程内）
├── sweep.py               4 个后台清扫任务 + 循环
├── notify.py              通知（先落库再投递）
├── backup.py              备份/恢复（SQLite VACUUM INTO / PG pg_dump）
├── seed.py / evaluation.py / clock.py / console.py
├── agent/         2009    LangGraph 编排（graph / llm / react_agent / state / tools）
├── domain/        3085    纯业务（availability / negotiate / booking / access / catalog / violations）
├── harness/       1016    Agent 运行时（context / runtime / tools / trace）
├── knowledge/      337    BM25 + RRF 融合检索
└── web/index.html  849    零依赖单文件控制台
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
| 数据 | SQLAlchemy 2.0 async；SQLite（默认）/ PostgreSQL 16（compose，**已实机验收**） |
| 迁移 | Alembic（结构变更唯一入口，5 个 revision） |
| 校验 | pydantic v2 |
| 前端 | 单文件 HTML，无框架、无构建、无 CDN 依赖 |
| 部署 | Dockerfile + docker-compose（api + postgres）；`scripts/accept_deploy.py` 端到端验收 |
| CI | GitHub Actions：静态检查+单测（3.10/3.13 矩阵）/ 真实进程端到端 / **PG 实跑** / **compose 实机验收** |

---

## 4. 入口文件与调用关系

**入口有两条**：

1. `main.py`（统一入口）→ 无子命令时 → `server.serve()` → `api.create_app()`；
   有子命令时 → `cli.main()`（13 个子命令）。
2. `src/lagent/server.py` —— 只做 `uvicorn.run`。

**一个预约请求的完整链路**：

```
main.py → server.serve() → api.create_app()
  → 中间件：请求上下文 ▸ 体积校验 ▸ CORS ▸ 认证 ▸ 限流
  → POST /api/reservations（表单）  或  /api/agent/chat（对话）
      └─ 两条路都汇到 domain/booking.create_reservation（同一套不变式）
  → domain/availability + negotiate + booking + violations
  → 唯一索引兜底 + PG 下 advisory lock
  → 审批（requires_approval 的设备 → pending）/ 通知入队 / 审计
旁路：obs / metrics / audit / ratelimit / sweep / notify（贯穿各层，不参与业务返回）
```

**关键边界**：`api.py` 依赖 `agent / domain / knowledge / metrics / obs` 等；
反向依赖不存在。**模型不决定事实也不决定权限** ——
资质、时段冲突、违约判定、可见范围全由 `domain/` 代码判定，
所以模型换代甚至挂掉，业务结论都不变。

**这一点现在是可验证的**：`POST /api/reservations` 让预约完全绕开模型，
表单面板在控制台上默认可见；模型挂掉时系统只剩"对话功能不可用"，而不是"不能预约"。

---

## 5. 主要功能与典型使用场景

**场景 A · 师生预约（两条路）**
> ① 对话：「明天下午两点想用荧光光谱仪两小时」→ 抽槽 → 约束判定 → 找空闲窗口 →
> 冲突给协商阶梯 → 下单 → 人话回复。
> ② **表单**：选设备 → 选日期时段 → 提交。**不经过模型**，
> 失败时原样显示服务端给出的原因（时段冲突 / 资质不够 / 违约被限 / 超单次上限）。

**场景 B · 到实验室刷卡进门**
> 管理员签发凭证 → 门禁核验（资质 / 预约时段 / 人卡一致）→
> 座位占位（容量不变式）→ **单次核销**（截图转发进不去）

**场景 C · 高风险设备审批**
> 设备标 `requires_approval` → 学生下单即占坑、状态 `pending` →
> 管理员待办里通过/驳回 → 驳回释放时段

**场景 D · 管理员运维**
> 实验室/设备/用户的增删改（**没有删除路由**，只能停用）· 查审计 · 在馆名单 ·
> 违约记录与豁免 · 通知投递 · 备份与恢复 · 手动跑清扫

**场景 E · 工程侧验证**
> `doctor` 环境自检 · `eval` 评测 · `loadtest` 并发压测 · `sweep` 清扫单轮 ·
> `access-demo` 门禁实证 · `scripts/accept_deploy.py` **compose 部署端到端验收**

---

## 6. 能力完成度矩阵

### ✅ 已实现（完整且带测试）

| 能力 | 证据 |
|---|---|
| 时段预约核心 | 粒度对齐、区间重叠（左闭右开）、六道约束判定、协商阶梯 + 接近度打分 |
| **并发安全下单** | 部分唯一索引兜底 + `IntegrityError` 裁决；PG 下 advisory lock；40 并发 ×3 轮**每轮恰好 1 成功** |
| **人员准入 / 门禁** | 4 张表 + 3 接口；未预约拦截、单次核销、房间容量不变式；`access-demo` 8/8 |
| 认证与授权 | scrypt + HS256 JWT + RBAC；`user_id` 一律从令牌取；401/403 严格分开；防账号枚举 |
| **登录失败锁定** | 按「来源地址 # 用户名」计数；过期锁丢掉旧失败记录；锁定请求不进审计 |
| 越权防护 | 真实 uvicorn 进程上的对抗清单 83/83 |
| **表单式下单** | `POST /api/reservations`（201）+ 控制台「直接预约」面板；重复预约 409 而非 500 |
| **设备级审批** | 申请即占坑 / 驳回释放时段 / 重复处理 409 / 非管理员看不到待办 |
| **通知** | 四类业务事件留痕；**先落库再投递**；没配 SMTP 如实报"跳过 N 条"而不是假装成功 |
| **违约判定** | **无门禁流水就不判**；迟到在宽限内不算；被门口拦下不算到场；默认只记不罚；豁免留痕 |
| **备份与恢复** | SQLite `VACUUM INTO` / PG `pg_dump`；**含恢复演练**（没演练过的备份不算备份） |
| **后台维护 CRUD** | 显式 null 不动字段；停用即失效；**刻意没有删除路由** |
| 审计 | 独立事务、失败与拒绝也记、`request_id` 贯穿 |
| 后台清扫 | **4 任务**（过期预约 / 凭证超时 / 归档 / 违约判定）、幂等、失败隔离、先落盘后删除 |
| **可观测** | JSON 单行日志 + `request_id` 跨访问日志/审计/门禁 + `/metrics` 16 指标 + 三档健康检查 |
| 数据库迁移 | Alembic 接管，含「给有数据的表加列」实证 |
| Agent Harness | 双执行模式（deterministic / react）、上下文预算、span、工具注册表 |
| 检索 | 手写 BM25 + RRF 融合（chromadb 可选） |
| 降级链 | react → deterministic → 引导式表单，边界如实标注 |
| 评测 | 14 条用例，意图/槽位/端到端三档；**用例的日历无关性有机械守卫** |
| **部署与验收** | `compose up` 实机跑通、两容器 healthy、真 PG 上 `revision=0005`、`accept_deploy.py` **25/25** |

### ⚠️ 部分实现

| 能力 | 现状 | 缺口 |
|---|---|---|
| **审批流程** | 两级（通过 / 驳回），设备级开关 | 无多级审批、无会签、无委托 |
| **通知** | 只有邮件一种通道 | 无短信/微信；无"即将到期"提醒；无失败重试（**故意的**） |
| **违约** | 覆盖「约了不来」 | 违规操作、损坏设备仍无记录；**处罚默认关闭**，需先观察判定准确度 |
| 预约状态机 | `pending` 已有人写（审批） | **无改期 / 续约** |
| 限流 | chat 按用户滑动窗口 + 登录锁定 | 仅单进程；多副本等于配额×副本数（发布拓扑是单副本，见 P2-10） |
| 会话状态 | `SessionStore` 可用 | **在进程内存**，重启即丢；多副本会漂移 |
| 部署 | compose 实机验收通过 | **单 worker**；默认仍 SQLite（compose 用 PG） |
| 指标 | 吐得出 Prometheus 文本 | 无时序存储/面板/告警；无跨副本聚合 |
| 密钥 | fail-closed（默认密钥拒绝启动） | `LAB_JWT_SECRET` 仍走 `.env`，未接 Docker secrets / KMS |

### ❌ 缺失（无代码）

| 能力 | 说明 |
|---|---|
| 幂等键 | 下单没有客户端生成的幂等键，客户端重试会产生两条预约 |
| 分布式追踪 | 自研 span 是节点级，未接 OpenTelemetry |
| Redis / 消息队列 | 依赖中不存在；长请求（5–30s）无队列化 |
| refresh token | 令牌 2 小时到期需重登（刻意：把风险窗口压小，而不是签长令牌假装安全） |
| 清扫出进程 | 循环跑在服务进程内；多副本时每个副本都扫一遍（幂等所以不算错，只是白花 CPU） |

---

## 7. 当前已知问题与阻塞

**没有阻塞项。** 需要跟踪的是两个"已知且已定性"的东西：

| 项 | 性质 | 现状 |
|---|---|---|
| **py3.10 job 的 pytest 曾失败过一次** | 间歇性，机制未知 | 本地用 Python 3.10.21 + 同源依赖（SQLAlchemy 2.0.54）复现不出来（848 条 exit=0）；期间无相关代码改动。**已埋伏**：CI 失败时会把 `FAILED` 行抬进**注解**（无需鉴权即可读），下次复现直接能拿到用例名 |
| **限流与会话在进程内存** | 已评估后接受 | 发布拓扑是单副本，所以"配额 × 副本数"在默认形态下不成立；重新评估的三条触发条件写在 `DELIVERY-READINESS.md` 的 P2-10 |

**环境侧（与代码无关，留个记录）**：国内网络下 `registry-1.docker.io` 不可达，
`docker pull` 需要配 registry mirror（实测 `docker.m.daocloud.io` / `docker.1ms.run` 可用）。

---

## 8. 成熟度评级

| 维度 | 评级 | 说明 |
|---|---|---|
| 代码组织与分层 | **优** | 分层清晰、边界有 AST 检查、harness 可独立复用 |
| 测试覆盖 | **优** | 848 用例、测试:生产 ≈ 0.79:1、真实进程对抗清单、PG 与 compose 各有专项 |
| 并发正确性 | **优** | 不变式压在数据库层，不靠应用层自觉；SQLite 与 PG 两条路径都有验证 |
| 安全（越权方向） | **良** | 越权防护扎实、密钥 fail-closed、登录有锁；**密钥托管仍在 `.env`** |
| 可观测性 | **优** | 日志+指标+三档健康检查，超出一般校内系统水平 |
| 业务功能完整度 | **中** | 主干流程齐了（预约/审批/准入/违约/通知/备份），但审批两级、通知只有邮件、违约只覆盖未到场 |
| 部署与运维 | **良** | 备份+恢复演练、compose 实机验收、CI 四类 job；扣分在单 worker 与密钥托管 |
| 规模化能力 | **中** | 分页有了；但单进程、会话在内存、无队列 |

**一句话定位**：这是一套**工程质量达到生产级思路的 AI Agent 应用**
（并发、越权、可观测、迁移、备份五块都做了实证），
业务功能也已补齐主干 —— **从"技术验证平台"变成了"可以开门营业、但需要按院系流程做少量定制"的系统。**

---

## 9. 接下来

按"收益 ÷ 风险"排序，不含已在文档里显式接受的项：

1. **先把它放一个真实实验室试运行一个学期**（最大的一件事，且不需要写代码）。
   期间观察两件事：违约判定准不准（门禁漏刷、宽限够不够），
   以及 `LAB_NOSHOW_BLOCKING_ENABLED` 该不该打开。
2. **密钥托管**：把 `LAB_JWT_SECRET` 从 `.env` 挪到 Docker secret。
   代码侧已经 fail-closed，剩下的只是投递方式。
3. **改期 / 续约**：现在只有"新约"和"取消"，用户要挪时间只能取消重约
   （而重约可能就约不上了）。这是日常使用里最先被抱怨的缺口。
4. **幂等键**：下单接口加客户端幂等键，挡住"点了两下"和"客户端重试"。
5. **通知多通道 + 到期提醒**：短信/微信通道与"预约前 N 小时提醒"，
   后者对降低违约率的效果比处罚更直接。
6. **清扫出进程 / 会话外置**：只在真的要横向扩容时才做（触发条件已写明）。

---

> 交付就绪度的详细评估、逐项整改清单与 `commit` 索引见
> [`DELIVERY-READINESS.md`](DELIVERY-READINESS.md)；
> 技术选型与分期升级方案见 [`ENTERPRISE-UPGRADE.md`](ENTERPRISE-UPGRADE.md)；
> **真实栈上的试用结果与摩擦点清单**见 [`TRIAL-RUN.md`](TRIAL-RUN.md)。
