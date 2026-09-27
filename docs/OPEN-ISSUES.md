# 未解决问题清单

> 更新时间：2026-09-27 · 对应 commit `4142e44` 之后的这一轮修复（改期 / 门禁开放时间 / 错误分类 / 通知 / 幂等 / 迁移演练 / 违约分解 / 分页 / 时钟 / CLI 收尾 / 密钥投递 / 清扫外置 / 目录过滤 / 告警规则 / 评测来源标注）
> 核对方式：**逐条回到代码里查**（`grep` / 实跑接口 / 读测试），不是复述旧文档。
>
> 三个来源并起来去重：
> * [`TRIAL-RUN.md`](TRIAL-RUN.md) —— 真实栈试运行撞出来的摩擦点（H / M / L 编号）
> * [`DELIVERY-READINESS.md`](DELIVERY-READINESS.md) —— 交付就绪度评估的整改项（P 编号）
> * [`PROJECT-OVERVIEW.md`](PROJECT-OVERVIEW.md) —— 能力完成度矩阵里标 ⚠️/❌ 的行
>
> **当前状态：18 条里 14 条已闭合**（每条都带测试，整套 `pytest` 全绿），
> 剩下 4 条：**1 条有触发条件**（#5）、**1 条缺外部资源**（#16）、**2 条是明确不做的取舍**（#17 / #18）。
> 它们列在下面第一节，理由与触发条件都写清楚了 —— 留着不是因为忘了。

---

## 一、仍未闭合（4 条）

### 5. 限流与会话在进程内存 —— **有触发条件，暂不改**

| | |
|---|---|
| **模块** | `ratelimit.py`（`SlidingWindowLimiter` 用进程内 dict）· `agent/state.py`（`SessionStore`） |
| **表现** | 两者都没有共享存储。 |
| **影响** | **单副本部署下完全不成立**（当前就是单副本）。一旦多副本：登录/对话配额变成「配额 × 副本数」；多轮对话的「上一轮备选」会在副本间漂移。 |
| **本轮做了什么** | 不改成共享存储（那是扩容时才做的事，现在做是浪费），但把这个**隐形前提变成可观测的**：两个组件构造时各自上报 `lagent_state_backend{component,backend}`（由组件自己报，换实现时指标自动跟着变）。多副本时同一个 component 会出现多条时间序列，`deploy/prometheus-alerts.yml` 里的 `LagentStateBackendSplitBrain` 会响。 |
| **触发条件** | 出现横向扩容需求时（判据就是上面那条指标）。重新评估的三条条件写在 `DELIVERY-READINESS.md` 的 P2-10。 |
| **到时怎么做** | 先把限流换成 Redis 计数器、会话外置（或加粘性会话）；这一步做完再谈多副本。 |

### 16. 真实模型评测尚未做过 —— **缺外部资源，不是代码问题**

| | |
|---|---|
| **模块** | `eval/` · `evaluation.py` |
| **表现** | 14/14 是 **mock 模型**下的链路自洽（README 已声明）。 |
| **影响** | 这个数字**不能当成模型准确率**。 |
| **本轮做了什么** | 把「数字被误用」这一半堵上：`EvalReport` 现在自带 `mode` / `model`，**非 live 模式的报告里直接印一行免责声明**，并暴露 `is_live_model`。理由：mock 数字最危险的用途不是被误读，而是被**断章取义地截图**写进简历 / 答辩 / 汇报 —— 文档不会被截图带过去，报告会。 |
| **剩下的** | 接真实模型（`LAB_APP_MODE=live` + `LAB_LLM_API_KEY`）单独跑一遍，单独记录；那份报告不带免责声明，可以引用。 |

### 17. 分布式追踪与队列 —— **明确不做**

| | |
|---|---|
| **模块** | — |
| **表现** | 自研 span 是节点级的，未接 OpenTelemetry；长请求（5–30s）无队列化，依赖中也没有 Redis/消息队列。 |
| **影响** | 请求一多会堵在单进程里（与第 5 条同源）。 |
| **为什么不做** | 这是「企业级」那一档，与当前规模不匹配（见 `ENTERPRISE-UPGRADE.md` 的分期结论）。记录在此，避免被当成遗漏。 |

### 18. 没有 refresh token —— **明确不做**

| | |
|---|---|
| **模块** | `security.py` |
| **表现** | 令牌 2 小时到期需重登。 |
| **为什么不做** | 把风险窗口压小，比签一个长令牌假装安全更诚实。要做得连吊销列表一起做，那是另一块工程量（见 README 的「已知限制」）。 |

---

## 二、与代码无关的环境 / 运维项

| 项 | 表现 | 处理 |
|---|---|---|
| **运行中的容器是旧镜像** | 本地那个 compose 栈是早先的镜像，**不含**新控制台与备份卷改动 | `docker compose up -d --build` 重建 api 容器即可（命令会让栈短暂中断几秒） |
| **Docker 守护进程写死 `127.0.0.1:7890` 代理** | Clash 一换端口或没开，`docker pull` 立刻失败，报错像"网络问题" | 配 registry mirror（`docker.m.daocloud.io` / `docker.1ms.run` 实测可用），彻底摆脱对代理的依赖 |
| **国内网络拉 Docker Hub 不通** | `registry-1.docker.io` 经代理与直连都是 000 | 同上，配镜像源 |
| **py3.10 job 的 pytest 曾失败一次且机制未知** | 本地用 Python 3.10.21 + 同源依赖（SQLAlchemy 2.0.54）复现不出；之后 5 次运行该 job 全绿 | **已埋伏**：失败时把 `FAILED` 行抬进 CI **注解**（无需鉴权可读）。下次复现先读注解拿用例名，别再花时间本地复现 |

---

## 三、已闭合（按编号索引，免得被重报）

### P1

| # | 是什么 | 怎么修的 | 在哪能验 |
|---|---|---|---|
| 1 | 预约不能改期 / 续约 | 新增 `PATCH /api/reservations/{id}`（领域层 `reschedule_reservation`）：**先校验并占新坑、成功后再放旧坑**；新时段不可用则原样保留旧时段。控制台加「改期」按钮 | `tests/test_booking_api.py::TestReschedule`（含"改期失败要保住原预约"） |
| 2 | 门禁不校验实验室开放时间 | `verify_entry` 加**闸门 8**，新增 `DENY_LAB_CLOSED`；管理员可带 `override_reason` 显式越权，**强制写审计** | `tests/test_access.py::TestLabClosedGate` / `TestAfterHoursOverride` |
| 3 | HTTP 错误只有人话没有分类 | 新增 `DomainError`，错误体带 `reason`（复用已有的 7 类 `BookingReason`）；越权类带 `reason="forbidden"` | `tests/test_booking_api.py` + `tests/test_api.py` 断言 `resp.json()["reason"]` |
| 4 | 通知只进库不出库、没有全系统视图 | `/api/notifications` 支持管理员 `all_users` + `status` 过滤，加 `X-Total-Count`，去掉硬编码 `.limit(200)`；健康检查已报积压 | `tests/test_api.py`、`tests/test_notify.py` |

### P2

| # | 是什么 | 怎么修的 | 在哪能验 |
|---|---|---|---|
| 6 | 下单没有幂等键 | `ReservationCreate.idempotency_key` + `(user_id, idempotency_key)` **部分唯一索引**（迁移 `0006_idempotency.py`）；命中即**回放首次结果**，HTTP 回 **200**（不是 201）并带 `replayed=true` | `tests/test_booking_api.py::TestIdempotencyKey` |
| 7 | 「给有数据的表加列」只演练到 0002 | 测试参数化到 **0003 / 0005**：先在旧 revision 上插数据，再升到 head，断言数据仍在、新列取默认值 | `tests/test_migrations.py::TestLaterRevisionsOnPopulatedTables` |
| 8 | 密钥投递仍是环境变量 | 新增 `LAB_JWT_SECRET_FILE` / `LAB_SMTP_PASSWORD_FILE`（Docker secret / K8s Secret 的通用约定），**文件优先**；读不出来或读到空 → **拒绝启动** | `tests/test_secret_delivery.py`（6 条） |
| 9 | 违约判定准确度没有数据支撑 | 接口加 `total`（判过几次）与 `pardoned`（其中豁免几次），与 `count`（实际计入）分开 | `tests/test_violations.py::TestViolationBreakdown` |
| 10 | 清扫跑在服务进程内 | CLI 新增 `sweep --loop [--interval N] [--ticks N]`；compose 里给出（默认注释掉的）独立 worker 段。单副本**不必**启用 | `tests/test_sweep.py::TestSweepLoop`（5 条） |

### P3

| # | 是什么 | 怎么修的 | 在哪能验 |
|---|---|---|---|
| 11 | CLI 在重建 / 恢复库后打印 `Event loop is closed` | 根因是**事件循环亲和性**（alembic 在线程里另开 loop，连接被绑到那个短命 loop）。改成命令跑完**在同一个 loop 里** dispose（新增 `_run_and_dispose`） | `tests/test_migrations.py::TestCliTeardown`（断言两个动作在同一个 loop 上） |
| 12 | 接口层时钟不可注入 | 新增 `LAB_FAKE_NOW`（默认空），解析不出来**直接报错**不静默回退；启用时启动打一条 CRITICAL | `tests/test_clock.py::TestFakeNowOverride` |
| 13 | 试运行 / 误建数据没有清理入口 | 设备本来就**只能改状态不能删**（历史预约要引用）。补上 `GET /api/labs?equipment_status=normal`：置成 `scrapped` 的设备可以从目录里滤掉（默认仍是全列，纯加成） | `tests/test_catalog_api.py::TestCatalogStatusFilter` |
| 14 | 分页只覆盖两个端点 | `/api/users`、`/api/reservations/pending`、`/api/notifications` 统一 `limit` / `offset` + `X-Total-Count`；**`/api/labs` 刻意不分页**（文档里写了理由） | `tests/test_api.py`（分页与 `X-Total-Count`） |
| 15 | 指标只有生产者没有消费者 | 新增 `deploy/prometheus-alerts.yml`（4 条规则：清扫过期 / 并发争抢 / 服务不可达 / 内存态分裂）；**指标改名会让测试失败** | `tests/test_metrics.py::TestAlertRulesReferenceRealMetrics` |

### 更早闭合的（旧索引）

| 曾经的编号 | 是什么 | 现在的状态 |
|---|---|---|
| TRIAL-RUN H1 | 审批 / 违约 / 通知 / 审计在控制台上零入口 | 已补四个面板（`c496315`）；本轮又补了改期按钮与 4 个管理员页签 |
| TRIAL-RUN H2 | 镜像里没有 `pg_dump` → 备份在容器里不可用 | 已装（`e5a701a`），构建期断言版本 |
| TRIAL-RUN H3 | `pg_dump` 17 导 16 的库，dump **恢复不回来** | 改成 PGDG 的 `postgresql-client-16`，CI 加「备份 + 恢复 + 逐表比对」守门 |
| TRIAL-RUN H4 | 备份/归档写在容器可写层，重建即丢 | compose 挂 `labvar` 卷；顺带修掉它引入的命名卷属主回归 |
| TRIAL-RUN L3 | `.dockerignore` 没排除 `var/` | 已提交 |
| DELIVERY-READINESS P0-1…P2-11a | 区间不变式 / 认证授权 / 审批 / 违约 / 通知 / 备份恢复 / 分页（部分）/ PG 实跑 / compose 实机 | 见 `DELIVERY-READINESS.md` 的复评表 |
| 两处**我自己的误判** | 「Docker 起不来」「沙箱代理没开」 | 更正记录在 `TRIAL-RUN.md` §4 与 `DELIVERY-READINESS.md` |

---

## 四、建议的处理顺序（更新版）

**已经没有必须在试点前处理的事项了**（原 P1 四条全部闭合）。

**试点期间观察**：第 9 条留下的三个数字（`count` / `total` / `pardoned`）—— 跑一个学期再决定是否打开处罚开关。
**触发才做**：第 5 条（判据是 `lagent_state_backend` 出现多条序列）、第 10 条（真的多副本了再加 worker）。
**等外部资源**：第 16 条（真实模型额度）。
**明确不做**：第 17、18 条。
