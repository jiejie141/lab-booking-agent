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

## 已知限制

* **迁移管不住「改了 models.py 却没生成迁移」。** 迁移只保证
  「按 revision 升上来是对的」。这种情况由 `tests/test_migrations.py`
  在 CI 里比对模型与迁移产物的 diff 来守（diff 必须为空）。
* **老库是「接管」而不是「重建」。** 迁移引入之前建好的库
  （有表、没有 `alembic_version`）如果结构与模型一致，走 `stamp head`
  只登记版本、一行 DDL 都不执行；结构不一致则明确报错，要求重建 ——
  一个不知道自己是哪个结构的库没有「往前走」的起点。
