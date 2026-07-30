#!/usr/bin/env bash
# 把本地完整库(db/market.sqlite, 含 profit_q/limit_up_count/analyst_report 等) 导出进 db-seed 分支(云端种子库)。
# 流程: ①主仓用相对路径 gzip+动态分卷(规避中文绝对路径在 python sqlite3.connect 的编码报错)
#      ②worktree 在 C:/seed-wt(无中文, C盘根, Git Bash 可靠) checkout db-seed
#      ③把分卷+sha 拷进 worktree(覆盖 db-seed 旧分卷)
#      ④worktree 内提交分卷 + force-push db-seed ⑤移除 worktree
# 前置: 本地抓取已完成且已跑 _export_seed_profitq.py 生成分卷(动态 part 数)。
# 用法: bash _push_seed.sh
set -e
REPO="/c/Users/zhenyu/Desktop/测试/astock-paper"
WT="C:/seed-wt"
cd "$REPO"

# 0. 确认关键表已就位(相对路径, 无中文)
PQ=$(C:/Users/zhenyu/.workbuddy/binaries/python/versions/3.13.12/python.exe -c "import sqlite3;c=sqlite3.connect('db/market.sqlite');print(c.execute('SELECT COUNT(*) FROM profit_q').fetchone()[0])" 2>/dev/null || echo 0)
LU=$(C:/Users/zhenyu/.workbuddy/binaries/python/versions/3.13.12/python.exe -c "import sqlite3;c=sqlite3.connect('db/market.sqlite');print(c.execute('SELECT COUNT(*) FROM limit_up_count').fetchone()[0])" 2>/dev/null || echo 0)
AR=$(C:/Users/zhenyu/.workbuddy/binaries/python/versions/3.13.12/python.exe -c "import sqlite3;c=sqlite3.connect('db/market.sqlite');print(c.execute('SELECT COUNT(*) FROM analyst_report').fetchone()[0])" 2>/dev/null || echo 0)
echo "profit_q=$PQ / limit_up_count=$LU / analyst_report=$AR"
[ "${PQ:-0}" -ge 1000 ] || { echo "profit_q 覆盖不足(<1000), 中止推送"; exit 1; }
[ "${LU:-0}" -ge 1000 ] || { echo "limit_up_count 覆盖不足, 中止"; exit 1; }
[ "${AR:-0}" -ge 1000 ] || { echo "analyst_report 覆盖不足, 中止"; exit 1; }

# 1. 确认动态分卷已生成(由 _export_seed_profitq.py 产出, 数量不定)
shopt -s nullglob
PARTS=(db_seed.sqlite.gz.part*)
shopt -u nullglob
[ "${#PARTS[@]}" -ge 1 ] || { echo "缺少分卷, 请先跑 _export_seed_profitq.py"; exit 1; }
echo "分卷数: ${#PARTS[@]}"
[ -f db_seed.sha256 ] || { echo "缺少 db_seed.sha256"; exit 1; }

# 2. 清理可能残留的 worktree
git worktree prune >/dev/null 2>&1 || true
if git worktree list | grep -q "$WT"; then
  git worktree remove "$WT" --force >/dev/null 2>&1 || rm -rf "$WT"
fi

# 3. 开 worktree(checkout db-seed 孤儿分支, 仅含种子分卷)
git worktree add "$WT" db-seed

# 4. 把主仓新分卷 + sha 拷进 worktree(覆盖 db-seed 旧分卷)
cp "${PARTS[@]}" db_seed.sha256 "$WT/"

# 5. worktree 内提交 + force-push
cd "$WT"
git add "${PARTS[@]}" db_seed.sha256
git commit -m "seed: full market.sqlite (profit_q=$PQ, limit_up_count=$LU, analyst_report=$AR) parts=${#PARTS[@]}" || echo "(无新改动, 跳过 commit)"
git push origin db-seed --force

# 6. 清理 worktree
cd "$REPO"
git worktree remove "$WT" --force >/dev/null 2>&1 || rm -rf "$WT"
git worktree prune >/dev/null 2>&1 || true
echo "DONE 已推送 db-seed(含 profit_q $PQ / limit_up_count $LU / analyst_report $AR 行, ${#PARTS[@]} 分卷), 云端 prep 将整库还原并拥有该数据。"
