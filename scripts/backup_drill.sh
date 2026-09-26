#!/usr/bin/env bash
# 备份 + 恢复演练：在**真实 compose 栈**上跑一遍，并逐表比对行数。
#
# 为什么不能只验"备份文件存在"：
#   实测撞过两次，都是"备份成功但根本恢复不回来"——
#   ① 镜像里压根没有 pg_dump，`main.py backup` 直接抛 BackupError；
#   ② 装了之后版本不对：pg_dump 17 导 16 的库，dump 头里写
#      `SET transaction_timeout = 0`（17 才有的参数），恢复到 16 上直接失败。
#   所以这道门必须是"**真的恢复一次**"。
#
# 也验容器里默认路径可写：compose 给 /app/var 挂了命名卷，而命名卷的属主
# 只在"卷为空且镜像里该路径存在"时才从镜像继承（见 Dockerfile 里的说明）。
# 用 bind mount 在本机验是验不出这个的 —— 必须走默认路径。
#
# 用法（在项目根，栈已经起来）：
#   bash scripts/backup_drill.sh
# 退出码非零 = 演练失败。

set -euo pipefail

COMPOSE="docker compose"
RESTORE_DB="lab_restore_drill"

echo "—— 1/4 备份（走容器里的默认路径，不是临时挂载）"
$COMPOSE exec -T api python main.py backup
DUMP="$($COMPOSE exec -T api sh -c 'ls -t /app/var/backup | head -1' | tr -d '\r')"
if [ -z "$DUMP" ]; then
    echo "::error::备份目录里没有文件 —— 备份看起来成功但什么也没写出来"
    exit 1
fi
echo "   备份文件：$DUMP"

echo "—— 2/4 建一个干净的目标库"
$COMPOSE exec -T db psql -U lab -d postgres -c "DROP DATABASE IF EXISTS $RESTORE_DB" >/dev/null
$COMPOSE exec -T db psql -U lab -d postgres -c "CREATE DATABASE $RESTORE_DB" >/dev/null

echo "—— 3/4 用应用自己的 restore 恢复进去"
# 单独一个一次性容器，只把库地址指到临时库；卷是同一个，所以能看到刚才那个 dump。
$COMPOSE run --rm -T \
    -e LAB_DATABASE_URL="postgresql+asyncpg://lab:lab@db:5432/$RESTORE_DB" \
    api python main.py restore "/app/var/backup/$DUMP" --yes

echo "—— 4/4 逐表比对行数"
TABLES="users laboratories equipment reservations reservation_slots audit_logs cert_grants entry_permits lab_occupancy access_events notifications"
failed=0
for table in $TABLES; do
    src="$($COMPOSE exec -T db psql -U lab -d lab          -t -A -c "select count(*) from $table" | tr -d '\r')"
    dst="$($COMPOSE exec -T db psql -U lab -d $RESTORE_DB -t -A -c "select count(*) from $table" | tr -d '\r')"
    if [ "$src" != "$dst" ]; then
        echo "::error::$table 行数不一致：源库 $src / 恢复库 $dst"
        failed=1
    else
        printf '   %-20s %s 行 ✓\n' "$table" "$src"
    fi
done

$COMPOSE exec -T db psql -U lab -d postgres -c "DROP DATABASE IF EXISTS $RESTORE_DB" >/dev/null

if [ "$failed" -ne 0 ]; then
    echo "::error::恢复后的数据与源库不一致 —— 这个备份不可信"
    exit 1
fi
echo "备份 + 恢复演练通过"
