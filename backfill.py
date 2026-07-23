# -*- coding: utf-8 -*-
"""初始数据回填(个股策略用)。可断点续跑(增量:已到最新则跳过)。
覆盖:沪深300(S1/S3) + 中证1000(S4) 的 成分/日线/证券/分红/基本面 + 沪深300指数PE(S5)。
用法:python backfill.py。首次全量约数十分钟(baostock 逐只),之后增量很快。"""
import sys
import io
import time
import logging

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

import util
import data
import fundamental
import data_adapter as da
from db import get_conn, init_db

logging.basicConfig(level=logging.ERROR)
# 【扩池 2026-07-23】用户口径: 主板全市场可投(除创/科/北)。可投池由 build_universe.py 按
# "主板前缀 + 流动性(成交额)+ 市值"筛到约 1000-1500 只, 写入 index_members['mainboard']。
# 数据源: 腾讯 stock_zh_a_hist_tx(不复权+后复权), 实测 ~10s/只 → 1300只≈3.6h < 云端6h上限,
# 单次可完成; 且本脚本幂等可断点续跑(已到最新则跳过), 万一超时下次续跑即可。
# 环境变量 UNIVERSE=sh000300 可回退旧300只小池(应急)。
import os as _os
_UNIVERSE = _os.environ.get("UNIVERSE", "mainboard")
INDICES = [_UNIVERSE]                    # 主板流动性可投池(默认); 或 sh000300(应急)
DIV_INDEX = "sh000300"                   # 分红回填沪深300(红利策略 s1/s15 池, 保持不变)


def _members(conn, idx):
    return [r[0] for r in conn.execute("SELECT code FROM index_members WHERE index_code=?", (idx,)).fetchall()]


def main():
    t0 = time.time()
    conn = get_conn()
    init_db(conn)
    fundamental.ensure()

    print("== 1/6 交易日历 ==", flush=True)
    data.update_calendar()

    print("== 2/6 指数成分 ==", flush=True)
    for idx in INDICES:
        if idx == "mainboard":
            # 主板流动性可投池: 由 build_universe 按 前缀+成交额+市值 构建(云端快照可达时筛到~1300)
            import build_universe
            n = len(build_universe.build(conn=conn))
        else:
            n = data.update_members(idx, conn=conn)
        print(f"  {idx}: {n} 成分", flush=True)
    # 红利/核心策略仍需 沪深300 成分做分红池, 确保其存在(与主板池并存)
    if "sh000300" not in INDICES:
        try:
            data.update_members("sh000300", conn=conn)
        except Exception as e:
            print("  sh000300 成分更新跳过:", e, flush=True)

    codes = sorted(set(sum([_members(conn, idx) for idx in INDICES], [])))
    today = util.today_str()

    print(f"== 3/6 个股日线({len(codes)}只) ==", flush=True)
    for i, code in enumerate(codes, 1):
        mx = conn.execute("SELECT max(trade_date) FROM daily_bar WHERE code=?", (code,)).fetchone()[0]
        if not (mx and mx >= today):
            df = da.fetch_daily(code, da.DEFAULT_START if not mx else mx, today)
            da.upsert(df, "daily_bar", conn=conn)
        if i % 50 == 0:
            print(f"  日线 {i}/{len(codes)} ({time.time()-t0:.0f}s)", flush=True)

    print("== 4/6 证券信息 ==", flush=True)
    data.update_security(codes, conn=conn)

    print(f"== 5/6 分红(沪深300 {len(_members(conn, DIV_INDEX))}只) ==", flush=True)
    div_codes = _members(conn, DIV_INDEX)
    for i, code in enumerate(div_codes, 1):
        if not conn.execute("SELECT 1 FROM dividend WHERE code=? LIMIT 1", (code,)).fetchone():
            da.upsert(da.fetch_dividend(code), "dividend", conn=conn)
        if i % 50 == 0:
            print(f"  分红 {i}/{len(div_codes)} ({time.time()-t0:.0f}s)", flush=True)

    print(f"== 6/7 基本面 PE/PB/市值/股息率({len(codes)}只) + 指数PE ==", flush=True)
    for i, code in enumerate(codes, 1):
        fundamental.update_stock_fundamental([code], conn=conn)
        if i % 50 == 0:
            print(f"  基本面 {i}/{len(codes)} ({time.time()-t0:.0f}s)", flush=True)
    fundamental.update_index_pe("sh000300", conn=conn)

    # 年度ROE 走 baostock(~9s/只), 全主板池会顶爆云端6h上限 → 仅回填 沪深300(有界~48min);
    # 主板大池其余票的 ROE 因子在 mf_core 中自动降级为中性(stock_annual 空→从宽通过),
    # 差异化由 市值分段/动量/低波/估值/分红门槛 承担, 不依赖全池 ROE。
    roe_codes = _members(conn, "sh000300") or codes
    print(f"== 7/7 年度ROE/净利润({len(roe_codes)}只=沪深300,卡D:红利/核心用) ==", flush=True)
    for i, code in enumerate(roe_codes, 1):
        fundamental.update_annual_roe([code], conn=conn, start_year=2015)
        if i % 50 == 0:
            print(f"  年度ROE {i}/{len(roe_codes)} ({time.time()-t0:.0f}s)", flush=True)

    # ETF 份额折算校正(卡C),确保回填后的ETF动量/NAV口径正确
    try:
        cfg, reg = __import__("conf").load_config(), __import__("conf").load_registry()
        etfs = sorted(data.core_etf_codes(cfg, reg))
        da.reconcile_etf_splits(etfs, conn=conn)
    except Exception as e:
        print("  ETF折算校正跳过:", e, flush=True)

    conn.close()
    da.bs_logout()
    print(f"== 回填完成 {time.time()-t0:.0f}s,共{len(codes)}只 ==", flush=True)


if __name__ == "__main__":
    main()
