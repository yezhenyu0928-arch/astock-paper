#!/usr/bin/env bash
# 把含 profit_q 的本地完整库导出进 db-seed 分支(云端种子库)。
# 流程: ①主仓用相对路径 gzip+分卷(规避中文绝对路径在 python sqlite3.connect 的编码报错)
#      ②worktree 在 C:/seed-wt(无中文, C盘根, Git Bash 可靠) checkout db-seed
#      ③把分卷拷贝进 worktree(避开主仓 checkout db-seed 与未跟踪分卷的冲突)
#      ④worktree 内提交分卷 + force-push db-seed ⑤移除 worktree
# 前置: 本地 baostock 抓取已完成(profit_q 已入 db/market.sqlite) 且已跑 _export_seed_profitq.py 生成分卷。
# 用法: bash _push_seed.sh
set -e
REPO="/c/Users/zhenyu/Desktop/测试/astock-paper"
WT="C:/seed-wt"
cd "$REPO"

# 0. 确认 profit_q 已就位(相对路径, 无中文)
PQ=$(C:/Users/zhenyu/.workbuddy/binaries/python/versions/3.13.12/python.exe -c "import sqlite3;c=sqlite3.connect('db/market.sqlite');print(c.execute('SELECT COUNT(*) FROM profit_q').fetchone()[0])" 2>/dev/null || echo 0)
echo "profit_q 行数: $PQ"
[ "${PQ:-0}" -ge 1000 ] || { echo "profit_q 覆盖不足(<1000), 中止推送"; exit 1; }

# 1. 确认分卷已生成(由 _export_seed_profitq.py 产出)
for f in db_seed.sqlite.gz.part00 db_seed.sqlite.gz.part01 db_seed.sqlite.gz.part02 db_seed.sqlite.gz.part03 db_seed.sha256; do
  [ -f "$f" ] || { echo "缺少 $f, 请先跑 _export_seed_profitq.py"; exit 1; }
done

# 2. 清理可能残留的 worktree
git worktree prune >/dev/null 2>&1 || true
if git worktree list | grep -q "$WT"; then
  git worktree remove "$WT" --force >/dev/null 2>&1 || rm -rf "$WT"
fi

# 3. 开 worktree(checkout db-seed 孤儿分支, 仅含种子分卷)
git worktree add "$WT" db-seed

# 4. 把主仓新分卷 + sha 拷进 worktree(覆盖 db-seed 旧分卷)
cp db_seed.sqlite.gz.part00 db_seed.sqlite.gz.part01 db_seed.sqlite.gz.part02 db_seed.sqlite.gz.part03 db_seed.sha256 "$WT/"

# 5. worktree 内提交 + force-push
cd "$WT"
git add db_seed.sqlite.gz.part00 db_seed.sqlite.gz.part01 db_seed.sqlite.gz.part02 db_seed.sqlite.gz.part03 db_seed.sha256
git commit -m "seed: add profit_q (SUE factor data, 2026-07-28) rows=$PQ" || echo "(无新改动, 跳过 commit)"
git push origin db-seed --force

# 6. 清理 worktree
cd "$REPO"
git worktree remove "$WT" --force >/dev/null 2>&1 || rm -rf "$WT"
git worktree prune >/dev/null 2>&1 || true
echo "DONE 已推送 db-seed(含 profit_q $PQ 行), 云端 prep 将整库还原并拥有该数据。"
