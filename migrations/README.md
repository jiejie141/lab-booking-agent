# 数据库迁移（alembic）

> 这个目录的存在理由：`create_all()` 只创建**缺失的表**，从不演进**已有的表**。
> 「给 `users` 加一列」这种最常见的改动，在老库上永远不会生效 ——
> 库会停在半新半旧的状态，报错是 `no such column:` 加一屏 SQLAlchemy 堆栈。
> 有了迁移，「这个库是什么结构」有了唯一答案：它的 revision。

## 常用命令

```bash
python main.py migrate              # 升到最新（等价 alembic upgrade head）
python main.py migrate --revision base   # 回滚到空库（会删掉所有表）
python main.py doctor               # 顺带打印「库 revision / 代码 head」
```

直接用 alembic CLI 也可以（`LAB_DATABASE_URL` 会从环境变量或 `.env` 读）：

```bash
alembic current                      # 库在哪个 revision
alembic upgrade head                 # 升级
alembic downgrade -1                 # 回退一步
alembic upgrade head --sql           # 只输出 SQL，不连库（发给 DBA 复核）
```

## 改结构的标准流程

1. 改 `src/lagent/models.py`
2. `alembic revision --autogenerate -m "加 xx 列"`
3. **人工复核生成的文件**（见下面「生成之后必须复核什么」）
4. `alembic upgrade head`
5. `pytest tests/test_migrations.py` —— 它会比对「迁移产物」与「模型」是否一致

## 三个刻意的设计决定

### 1. URL 只在一处（`env.py` 从 `LAB_DATABASE_URL` 读）

`alembic.ini` 里**故意没有** `sqlalchemy.url`。迁移升的库必须就是服务连的库；
两处各配一份 URL，迟早就出现「迁移升了 A 库、服务连着 B 库」——
而且这种错不报错，只是行为诡异。

### 2. `alembic.ini` 必须保持纯 ASCII

alembic 读 ini 用的是 **locale 编码**（`alembic/util/compat.py` 里
`read_config_parser(..., encoding="locale")`）。Windows 中文环境 locale 是 GBK，
ini 里出现任何非 ASCII 字节，alembic 会在**读到配置之前**就抛
`UnicodeDecodeError`。所以中文说明只能放在本文件里，不能放进 ini。

同一个坑还有第二个表现：ini 的日志段里 `%(levelname)-5.5s` 这类格式串与
configparser 的插值规则相互作用。这里索性不配日志段，改在 `env.py` 里
只给 `alembic` 这一个 logger 挂 handler —— 否则 `alembic upgrade head`
会**静默成功**，运维时看不到「正在升到哪个 revision」。

### 3. 用异步驱动跑迁移，不引入 psycopg2

本项目跑 `aiosqlite` / `asyncpg`。如果迁移走同步驱动，就会出现
「迁移说成功、服务连不上」这种由两种驱动差异造成的问题 ——
而这类问题最容易在「自以为验证过了」的时候出现。

## 生成之后必须复核什么

`--autogenerate` 是**比对**出来的，不是理解出来的。以下几条它容易漏，本仓库
`0001` 已人工复核并通过测试钉住：

| 容易丢的东西 | 后果 | 谁守住它 |
| --- | --- | --- |
| 部分索引的 `WHERE` 子句（`uq_res_active_slot` / `uq_permit_one_inside`） | 取消后时段订不回来 / 一个人同时出现在两个房间 | `tests/test_migrations.py` 直接断言 DDL 文本 |
| 唯一索引（`uq_equipment_slot` / `uq_lab_slot_seat`） | 并发下超卖、房间塞爆 | 同上 + `python main.py loadtest` |
| `ondelete="CASCADE"` | 删预约留下孤儿占位格 | 上面两条测试 |
| 列类型变更 | `compare_type=True` 已开，但 SQLite 上 alter 要 batch 模式 | `tests/test_migrations.py` 的「无差异」断言 |

## revision 一览

| revision | 做了什么 | 验证方式 |
| --- | --- | --- |
| `0001` | 建全部 10 张表（含 3 条部分唯一索引的 `WHERE` 子句） | 从**空库** autogenerate 后人工复核；`tests/test_migrations.py` 断言 DDL 文本与「产物 vs 模型 diff 为空」 |
| `0002` | 给 `audit_logs` / `access_events` 加 `request_id` 列（空串而非 NULL）与索引 | 见下节 —— **这是仓库里第一条给「已经有数据的表」加列的迁移** |

## `0002` 是怎么验的（以及为什么 `0001` 的验证不算数）

`0001` 是从空库一次性建起来的，而**空表上加列永远不会失败** ——
它证明不了任何事。真正会出事的是三件事凑在一起：表里已经有行、
新列是 `NOT NULL`（老行必须拿到确定的默认值）、而且同时还要建索引。

所以 `0002` 单独做了一层验证：

1. **退到 `0001`**（那时还没有 `request_id` 列），用裸 SQL 灌进几行"历史数据"
   —— 刻意不走 ORM：ORM 已经认识 `request_id` 了，用它插入等于让今天的代码
   去写昨天的结构；
2. 升到 head，断言 ① 数据一行不少 ② **老行的 `request_id` 是空串而不是 NULL**
   ③ 两条索引都建了出来 ④ 结构与模型 diff 为空；
3. **再退一次、再升一次**，确认这个往返不会吃掉数据 ——
   "能回滚"只有在这条通过时才算数。

这三条在 `tests/test_migrations.py::TestSecondRevisionOnPopulatedTables` 里。
另外在真实开发库上也手工走过一遍（`migrate` → `migrate --revision 0001 --down`
→ `migrate`），结论一致。

> 为什么 `request_id` 用**空串**而不是 NULL：空串是「这个库写入时没有请求上下文」
> （清扫任务、CLI、播种）的确定表示。用 NULL 的话
> `WHERE request_id = ''` 查不到这些行，于是它们会在按 id 检索时凭空消失。

## 已知限制

* **迁移管不住「改了 models.py 却没生成迁移」。** 迁移只保证
  「按 revision 升上来是对的」。这种情况由 `tests/test_migrations.py`
  在 CI 里比对模型与迁移产物的 diff 来守（diff 必须为空）。
* **老库是「接管」而不是「重建」。** 迁移引入之前建好的库
  （有表、没有 `alembic_version`）如果结构与模型一致，走 `stamp head`
  只登记版本、一行 DDL 都不执行；结构不一致则明确报错，要求重建 ——
  一个不知道自己是哪个结构的库没有「往前走」的起点。
