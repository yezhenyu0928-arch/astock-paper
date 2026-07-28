#!/usr/bin/env bash
# 把含 profit_q 的本地完整库导出进 db-seed 分支(云端种子库)。
# 用 git worktree 隔离, 避免直接 checkout db-seed 覆盖刚生成的 part 文件。
# 前置: 本地 baostock 抓取已完成(profit_q 已入 db/market.sqlite)。
# 用法: bash _push_seed.sh
set -e
REPO="/c/Users/zhenyu/Desktop/测试/astock-paper"
SRC="$REPO/db/market.sqlite"
WT="/tmp/seed-wt"
PART=99614720   # 95 MiB, 与既有分卷一致

cd "$REPO"
[ -f "$SRC" ] || { echo "缺少 $SRC, 请先完成本地抓取"; exit 1; }

# 0. 确认 profit_q 已就位(否则推上去也没用)
PQ=$(C:/Users/zhenyu/.workbuddy/binaries/python/versions/3.13.12/python.exe -c "import sqlite3;c=sqlite3.connect(r'$SRC');print(c.execute('SELECT COUNT(*) FROM profit_q').fetchone()[0])" 2>/dev/null || echo 0)
echo "profit_q 行数: $PQ"
[ "${PQ:-0}" -ge 1000 ] || { echo "profit_q 覆盖不足(<1000), 中止推送"; exit 1; }

# 1. 开 worktree 指向 db-seed(孤儿分支, 仅含种子分卷)
git worktree add "$WT" db-seed

# 2. 在 worktree 内从完整库导出 gz + 分卷 + sha
cd "$WT"
gzip -c "$SRC" > db_seed.sqlite.gz
split -b $PART -d -a 2 db_seed.sqlite.gz db_seed.sqlite.gz.part
sha256sum db_seed.sqlite.gz > db_seed.sha256
ls -l db_seed.sqlite.gz.part* db_seed.sha256

# 3. 仅提交分卷(+sha) 到 db-seed 并强制推送
git add db_seed.sqlite.gz.part00 db_seed.sqlite.gz.part01 db_seed.sqlite.gz.part02 db_seed.sqlite.gz.part03 db_seed.sha256
git commit -m "seed: add profit_q (SUS/SUE factor data, 2026-07-28)"
git push origin db-seed --force

# 4. 清理 worktree
cd "$REPO"
git worktree remove "$WT"
echo "DONE 已推送 db-seed(含 profit_q), 云端 prep 将整库还原并拥有该数据。"
