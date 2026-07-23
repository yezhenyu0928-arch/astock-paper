# -*- coding: utf-8 -*-
"""构建"主板 + 流动性门槛"可投池, 写入 index_members(index_code='mainboard')。

用户口径(2026-07-23 确认): 只要不是创业板/科创板/北交所, 都可投 → 主板全市场,
再按流动性(日均成交额)+ 市值门槛筛到约 1000-1500 只可投池, 供 backfill / run_daily / 选股底座共用。

数据源策略(本地/云端通用, 优雅降级):
  1) ak.stock_info_a_code_name(): 全A代码+名称(端点稳定, 本地云端均可达)
     → 按主板前缀过滤 + 名称排除 ST/*ST/退 → 主板基础池(~3200)。
  2) ak.stock_zh_a_spot_em(): 实时快照(含成交额/总市值)
     → 云端(GitHub runner 网络干净)可达: 按 成交额≥min_amount & 总市值≥min_mcap 筛到目标规模。
     → 本地(东财实时被墙)不可达: 降级为主板基础池, 打印告警; 真正的流动性筛选交由云端回填时执行。

幂等: 每次重建 index_code='mainboard' 的成分(先删后插)。

用法:
  python build_universe.py                 # 默认门槛
  python build_universe.py --min-amount 100000000 --min-mcap 5000000000 --target 1300
"""
import sys
import io
import argparse
import logging

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.ERROR)

from db import get_conn, init_db

MAINBOARD_INDEX = "mainboard"
# 主板前缀: 沪市 600/601/603/605, 深市 000/001/002/003。
# 排除: 科创板 688 / 创业板 300·301 / 北交所 8xx·4xx·920。
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
_BAD_NAME = ("ST", "*ST", "退", "PT")


def _with_prefix(code6: str) -> str:
    """6 位裸码 → 带交易所前缀(与库内 daily_bar.code 一致)。"""
    c = str(code6).zfill(6)
    if c[:3] in ("600", "601", "603", "605", "900", "688"):
        return "sh" + c
    return "sz" + c


def _base_mainboard():
    """全A代码+名称 → 主板基础池 [(code6, name)]。端点稳定, 本地云端均可用。"""
    import akshare as ak
    df = ak.stock_info_a_code_name()
    out = []
    for r in df.itertuples(index=False):
        code, name = str(r.code).zfill(6), str(r.name)
        if code[:3] not in MAIN_PREFIX:
            continue
        if any(b in name for b in _BAD_NAME):
            continue
        out.append((code, name))
    return out


def _snapshot():
    """实时快照(含成交额/总市值)。返回 {code6: {'amount':元, 'mcap':元}} 或 None(不可达)。"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
    except Exception as e:
        print(f"  [降级] 实时快照不可达({repr(e)[:60]}) → 本次不做流动性筛选", flush=True)
        return None
    m = {}
    for r in df.itertuples(index=False):
        try:
            code = str(getattr(r, "代码")).zfill(6)
            amt = float(getattr(r, "成交额") or 0)      # 元
            mcap = float(getattr(r, "总市值") or 0)     # 元
            m[code] = {"amount": amt, "mcap": mcap}
        except Exception:
            continue
    return m


def build(min_amount=1.0e8, min_mcap=5.0e9, target=1300, conn=None):
    """构建主板可投池并写入 index_members。返回入选 code 列表(带前缀)。"""
    own = conn is None
    conn = conn or get_conn()
    init_db(conn)

    base = _base_mainboard()
    print(f"== 主板基础池(前缀+去ST): {len(base)} 只 ==", flush=True)

    snap = _snapshot()
    chosen = []  # [(code6, name)]
    if snap:
        scored = []
        for code, name in base:
            s = snap.get(code)
            if not s:
                continue                                   # 无快照(停牌/新股) → 跳过
            if s["amount"] < min_amount or s["mcap"] < min_mcap:
                continue                                   # 流动性/市值门槛
            scored.append((code, name, s["amount"], s["mcap"]))
        # 若通过门槛的仍多于 target, 按成交额降序保留 target 只(留最活跃的)
        scored.sort(key=lambda x: x[2], reverse=True)
        if target and len(scored) > target:
            scored = scored[:target]
        chosen = [(c, n) for c, n, _, _ in scored]
        print(f"== 流动性筛选(成交额≥{min_amount/1e8:.1f}亿 & 市值≥{min_mcap/1e8:.0f}亿): "
              f"{len(chosen)} 只入选 ==", flush=True)
    else:
        chosen = base
        print(f"== [降级] 未做流动性筛选, 主板基础池全量入选: {len(chosen)} 只 "
              f"(云端回填时会以快照重筛) ==", flush=True)

    # 写入 index_members(先删后插, 幂等)。in_date 统一置早期日(免费源无历史剔除日,
    # 存在幸存者偏差, 与 fetch_index_members 既有口径一致); out_date=NULL 表示仍在成分。
    codes = [_with_prefix(c) for c, _ in chosen]
    conn.execute("DELETE FROM index_members WHERE index_code=?", (MAINBOARD_INDEX,))
    conn.executemany(
        "INSERT OR REPLACE INTO index_members(index_code, code, in_date, out_date) "
        "VALUES (?, ?, '2018-01-01', NULL)",
        [(MAINBOARD_INDEX, c) for c in codes])
    conn.commit()
    print(f"== 写入 index_members['{MAINBOARD_INDEX}']: {len(codes)} 只 ==", flush=True)

    if own:
        conn.close()
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-amount", type=float, default=1.0e8, help="20日日均成交额下限(元), 快照用当日成交额代理")
    ap.add_argument("--min-mcap", type=float, default=5.0e9, help="总市值下限(元)")
    ap.add_argument("--target", type=int, default=1300, help="目标可投池规模上限")
    a = ap.parse_args()
    build(min_amount=a.min_amount, min_mcap=a.min_mcap, target=a.target)


if __name__ == "__main__":
    main()
