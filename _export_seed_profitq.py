# -*- coding: utf-8 -*-
"""把含 profit_q 的本地完整库(db/market.sqlite) 重新导出为云端种子库分卷。

背景(2026-07-28): 冲刺20%+ 的 SUE/52周高 因子依赖 profit_q 表, 而云端 Runner 海外连不上
baostock, 无法自行抓取。故 profit_q 必须由本地一次性抓取后, 经本脚本导出进种子库、
force-push 到 db-seed 分支, 云端 prep 每次整库还原 → 云端永久拥有该数据(本地开不开机无所谓)。

用法(抓取完成后): python _export_seed_profitq.py
产出: db_seed.sqlite.gz + db_seed.sqlite.gz.part00/01/02/03 + db_seed.sha256
随后由 shell 把 4 个 part + sha256 提交进 db-seed 分支并 force-push。
"""
import gzip
import shutil
import hashlib
import os

SRC = "db/market.sqlite"
GZ = "db_seed.sqlite.gz"
PART = 99614720  # 95 MiB, 与既有分卷体积一致(确保云端 cat part* 顺序/体积兼容)


def main():
    if not os.path.exists(SRC):
        raise SystemExit(f"缺少 {SRC}, 请先完成本地 baostock 抓取")
    print(f"导出 {SRC} -> {GZ} ...", flush=True)
    with open(SRC, "rb") as f_in, gzip.open(GZ, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    sz = os.path.getsize(GZ)
    print(f"  gzip 完成, 体积 {sz/1e6:.1f} MB", flush=True)

    # 分卷(数字后缀 00..NN, 与云端 cat db_seed.sqlite.gz.part* 的字典序一致)
    n = 0
    with open(GZ, "rb") as f:
        chunk = f.read(PART)
        while chunk:
            with open(f"{GZ}.part{n:02d}", "wb") as o:
                o.write(chunk)
            n += 1
            chunk = f.read(PART)
    print(f"  分卷完成: {n} 个 part(各 {PART/1e6:.0f}MB)", flush=True)

    # sha256(供人工校验, prep 当前未强制校验)
    h = hashlib.sha256(open(GZ, "rb").read()).hexdigest()
    open("db_seed.sha256", "w").write(h)
    print(f"  sha256: {h}", flush=True)
    print("DONE 导出。下一步: 切换 db-seed 分支, 用新 part 替换并提交 force-push。")


if __name__ == "__main__":
    main()
