# -*- coding: utf-8 -*-
"""本地快速对比 3 个全A股(all_a)候选策略的回测指标。
用法: python bt_alla.py [start] [end] [capital]
依赖 backfill_all_a.py 已构建 index_members['all_a'] 且新板日线/基本面已入库。
"""
import sys
import backtest

SIDS = [
    "s53_all_a_momentum_smallcap@v1",
    "s54_all_a_industry_mom@v1",
    "s55_all_a_value_quality@v1",
]
START = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
END = sys.argv[2] if len(sys.argv) > 2 else "2025-12-31"
CAP = int(sys.argv[3]) if len(sys.argv) > 3 else 100000


def main():
    print(f"=== 全A候选策略对比 {START} ~ {END} 资本{CAP} ===")
    for sid in SIDS:
        try:
            r = backtest.run_backtest(sid, START, END, capital=CAP)
            m = r.get("metrics", {})
            ann = m.get("annual")
            dd = m.get("max_dd")
            sharpe = m.get("sharpe")
            win = m.get("win_rate")
            end_eq = m.get("end_equity")
            dd_ok = (dd is not None and ann is not None and abs(dd) <= ann + 1e-9)
            print(f"\n[{sid}]")
            print(f"  年化={ann}  最大回撤={dd}  夏普={sharpe}  胜率={win}  终值={end_eq}")
            print(f"  回撤≤年化? {'✅' if dd_ok else '❌(违反铁律)'}")
            print(f"  达标(年化>10%且回撤≤年化)? {'✅' if (ann and ann>0.10 and dd_ok) else '❌'}")
        except Exception as e:
            import traceback
            print(f"\n[{sid}] 回测失败: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
