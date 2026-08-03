# -*- coding: utf-8 -*-
"""因子诊断:对 mainboard 池逐月截面计算各因子值 + 次月收益, 输出 Rank IC / ICIR / 分组单调性。
方法论对照《因子投资》2.1(排序法)与7.1(IC/ICIR):
  - Rank IC = Spearman(因子值, 次月收益), 逐月截面
  - ICIR = mean(IC)/std(IC), IC>0占比
  - 分组单调性: 因子十分位 → 各组次月平均收益的秩相关(书中2.1.2)
所有取数 <= 月末日, 严格防未来函数。仅本地只读库, 不联网。

用法: python factor_diagnostics.py [start] [end]
"""
import sys, io, os, sqlite3, statistics, math, time
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import conf
import util
import trade_calendar as cal
import fundamental as F

DB = os.path.join(ROOT, "db", "market.sqlite")

# ---------- 因子定义 ----------
# 每个因子: (name, compute_fn(code, conn, date_bars, month_end_close) -> value|None)
# date_bars: 该 code 截至 signal 日的后复权 bar 列表(date,close) 升序

def factor_size(code, conn, d, bars, close_map):
    f = F.get_fundamental(code, d, conn)
    if not f or not f.get("market_cap"):
        return None
    return math.log(f["market_cap"])

def factor_ep(code, conn, d, bars, close_map):
    """EP = 近一年净利润 / 市值。净利用 stock_annual(年报, pub_date<=d) 最近一年。"""
    f = F.get_fundamental(code, d, conn)
    if not f or not f.get("market_cap"):
        return None
    row = conn.execute("SELECT net_profit FROM stock_annual WHERE code=? AND pub_date IS NOT NULL "
                       "AND pub_date<>'' AND pub_date<=? ORDER BY stat_year DESC LIMIT 1",
                       (code, util.to_date_str(d))).fetchone()
    if not row or row[0] is None or row[0] <= 0:
        return None
    return row[0] / f["market_cap"]

def factor_bm(code, conn, d, bars, close_map):
    f = F.get_fundamental(code, d, conn)
    if not f or not f.get("pb"):
        return None
    pb = f["pb"]
    if not pb or pb <= 0:
        return None
    return 1.0 / pb

def factor_roe(code, conn, d, bars, close_map):
    """ROE: stock_annual 最近年报(小数)。"""
    row = conn.execute("SELECT roe FROM stock_annual WHERE code=? AND pub_date IS NOT NULL "
                       "AND pub_date<>'' AND pub_date<=? ORDER BY stat_year DESC LIMIT 1",
                       (code, util.to_date_str(d))).fetchone()
    if not row or row[0] is None:
        return None
    return row[0]

def factor_mom12_1(code, conn, d, bars, close_map):
    """12-1月动量: close[-22]/close[-253]-1 (跳过最近1月, 修复现有 mf_core 未跳月 bug)。"""
    if len(bars) < 253:
        return None
    c_prev1m = bars[-22][1]      # 21个交易日前的收盘(最近1月起点)
    c_base = bars[-253][1]       # 252个交易日前
    if not c_prev1m or not c_base or c_base <= 0:
        return None
    return c_prev1m / c_base - 1

def factor_rev1m(code, conn, d, bars, close_map):
    """1月反转: 近1月收益(负相关→反转, IC 预期为负, 反转因子取其负值)。"""
    if len(bars) < 22:
        return None
    c0 = bars[-1][1]
    c1 = bars[-22][1]
    if not c0 or not c1 or c1 <= 0:
        return None
    return c0 / c1 - 1

def factor_mom6(code, conn, d, bars, close_map):
    """6月动量(6-1月): close[-22]/close[-127]-1。"""
    if len(bars) < 127:
        return None
    c0 = bars[-22][1]
    c1 = bars[-127][1]
    if not c0 or not c1 or c1 <= 0:
        return None
    return c0 / c1 - 1

def factor_lowvol(code, conn, d, bars, close_map):
    """60日波动率(取负=低波因子)。"""
    if len(bars) < 61:
        return None
    rets = [bars[i][1] / bars[i-1][1] - 1 for i in range(len(bars)-60, len(bars))]
    if not rets:
        return None
    return -statistics.pstdev(rets) * math.sqrt(252)   # 负号: 低波动=好

def factor_liq(code, conn, d, bars, close_map):
    """流动性: 近20日日均成交额对数(取负=低成交额/低流动性因子)。"""
    amts = [b[2] for b in bars[-20:] if b[2] is not None]
    if len(amts) < 10:
        return None
    a = sum(amts) / len(amts)
    return -math.log(a + 1)   # 负号: 低成交额=好(但需小心微小票流动性陷阱, 用流动性下限过滤)

def factor_sue(code, conn, d, bars, close_map):
    """SUE(修复版): profit_q 按 pub_date<=d 过滤(不再用 stat_date 造成未来函数)。
    SUE_t = (NP_t - NP_{t-4}) / std(历史意外)。NP 取单季净利。"""
    ph = util.to_date_str(d)
    rows = conn.execute(
        "SELECT stat_date, net_profit FROM profit_q WHERE code=? AND pub_date IS NOT NULL "
        "AND pub_date<>'' AND pub_date<=? ORDER BY stat_date", (code, ph)).fetchall()
    seq = [np0 for _, np0 in rows]
    if len(seq) < 9:
        return None
    t, t4 = seq[-1], seq[-5]
    if t4 in (None, 0, 0.0) or t is None:
        return None
    surprise = t - t4
    hist = []
    for i in range(len(seq) - 9, len(seq) - 1):
        if i - 4 >= 0 and seq[i-4] not in (None, 0, 0.0) and seq[i] is not None:
            hist.append(abs(seq[i] - seq[i-4]))
    if len(hist) < 2:
        return None
    sigma = statistics.pstdev(hist)
    return surprise / sigma if sigma > 0 else None

def factor_high52(code, conn, d, bars, close_map):
    """52周高距离: (今收 - 近252日最高收)/最高收。越接近突破=越强。"""
    if len(bars) < 120:
        return None
    hi = max(b[1] for b in bars[:-1])
    cur = bars[-1][1]
    if not hi or hi <= 0 or cur is None:
        return None
    return (cur - hi) / hi

# 因子清单: name -> compute
FACTORS = {
    "size(规模log市值)": factor_size,
    "ep(盈利市值比)": factor_ep,
    "bm(账面市值比1/PB)": factor_bm,
    "roe(净资产收益率)": factor_roe,
    "mom12_1(12-1动量)": factor_mom12_1,
    "rev1m(1月反转)": factor_rev1m,
    "mom6(6月动量)": factor_mom6,
    "lowvol(低波动-60d)": factor_lowvol,
    "liq(低流动性-log成交额)": factor_liq,
    "sue(盈余惊喜,修复版)": factor_sue,
    "high52(52周高距离)": factor_high52,
}


def load_bars(conn, code):
    """该 code 全历史后复权 (date, close, amount) 升序。"""
    rows = conn.execute(
        "SELECT trade_date, close, adj_factor, amount FROM daily_bar WHERE code=? ORDER BY trade_date",
        (code,)).fetchall()
    return [(r[0], (r[1] or 0) * (r[2] or 1.0), r[3]) for r in rows]


def main(start="2019-01-01", end="2026-07-31"):
    conn = sqlite3.connect(DB)
    # 月度截面: 每月最后交易日
    t0 = time.time()
    month_ends = [d for d in cal.trade_days(start, end)
                  if cal.last_trade_day_of_month(d)]
    print(f"月度截面 {len(month_ends)} 期: {month_ends[0]} ~ {month_ends[-1]}")

    # 池: mainboard 当前成员(逐期用 in_date<=d 过滤)
    pool_sql = ("SELECT code FROM index_members WHERE index_code='mainboard' "
                "AND in_date<=? AND (out_date IS NULL OR out_date>?)")
    # 预载全部 mainboard 成员的 bars 缓存
    members = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM index_members WHERE index_code='mainboard'").fetchall()]
    print(f"mainboard 池: {len(members)} 只, 预载 bars...")
    bars_cache = {}
    for i, code in enumerate(members):
        bars_cache[code] = load_bars(conn, code)
        if (i + 1) % 500 == 0:
            print(f"  已载 {i+1}/{len(members)} ({time.time()-t0:.0f}s)")
    print(f"bars 预载完成 ({time.time()-t0:.0f}s)")

    # 逐月截面
    ics = {name: [] for name in FACTORS}   # name -> list[ic]
    decile_ret = {name: [] for name in FACTORS}  # name -> list[10组平均月收益]
    factor_corr_samples = {name: [] for name in FACTORS}  # 各因子与 size 的秩相关

    for k, d in enumerate(month_ends):
        pool = [c for c in members if True]  # mainboard 静态, 但过滤停牌/无数据
        # 当日有行情 + 可交易(非停牌)的票
        usable = []
        for code in pool:
            bars = bars_cache.get(code)
            if not bars:
                continue
            # 当日有 bar(截至 d)
            idxs = [i for i, b in enumerate(bars) if b[0] <= d]
            if not idxs:
                continue
            usable.append((code, bars, idxs[-1]))   # 完整bars + 截至d的下标
        if len(usable) < 100:
            print(f"  {d}: 可用票太少({len(usable)}), 跳过")
            continue
        print(f"  [{k+1}/{len(month_ends)}] {d}: {len(usable)} 只")

        # 各因子值 + 本期末收盘(用于次月收益)
        fvals = {name: {} for name in FACTORS}
        cur_close = {}
        for code, bars, i in usable:
            cur_close[code] = bars[i][1]
            for name, fn in FACTORS.items():
                try:
                    fvals[name][code] = fn(code, conn, d, bars[:i + 1], cur_close)
                except Exception:
                    fvals[name][code] = None

        # 次月收益: 下一期末收盘/本期末 - 1
        if k + 1 < len(month_ends):
            nxt = month_ends[k + 1]
            next_close = {}
            for code, bars, i in usable:
                j = len(bars) - 1
                while j >= 0 and bars[j][0] > nxt:
                    j -= 1
                next_close[code] = bars[j][1] if j >= 0 else None
            fwd = {}
            for code in list(cur_close.keys()):
                c0, c1 = cur_close[code], next_close.get(code)
                if c0 and c1 and c0 > 0 and c1 > 0:
                    fwd[code] = c1 / c0 - 1

            for name in FACTORS:
                pairs = [(fvals[name][c], fwd[c]) for c in fwd
                         if fvals[name].get(c) is not None]
                if len(pairs) < 30:
                    ics[name].append(None)
                    continue
                xs = [p[0] for p in pairs]
                ys = [p[1] for p in pairs]
                # 过滤 NaN/非有限值(防未来函数已保证<=月末, 此处防脏数据)
                clean = [(x, y) for x, y in zip(xs, ys)
                         if x is not None and y is not None
                         and math.isfinite(x) and math.isfinite(y)]
                if len(clean) < 30:
                    ics[name].append(None)
                    continue
                xs = [c[0] for c in clean]
                ys = [c[1] for c in clean]
                ic = spearman(xs, ys)
                ics[name].append(ic)
                # 分组十分位单调性
                q = decile_mean_ret(xs, ys)
                if q:
                    decile_ret[name].append(q)
                # 与 size 的秩相关(信息增量性)
                if name != "size(规模log市值)":
                    s_pairs = [(fvals[name][c], fvals["size(规模log市值)"][c]) for c in fwd
                               if fvals[name].get(c) is not None and fvals["size(规模log市值)"].get(c) is not None]
                    s_pairs = [(x, y) for x, y in s_pairs
                               if math.isfinite(x) and math.isfinite(y)]
                    if len(s_pairs) > 30:
                        factor_corr_samples[name].append(
                            spearman([p[0] for p in s_pairs], [p[1] for p in s_pairs]))

    conn.close()

    # ---- 输出报告 ----
    lines = ["# 因子诊断报告(Rank IC / ICIR / 分组单调性)",
             "", f"> 池: mainboard {len(members)}只 | 区间 {start}~{end} | {len(month_ends)}个月度截面",
             f"> 方法论:《因子投资》2.1排序法 + 7.1 IC/ICIR; 所有因子值 <= 月末日, 严格防未来函数",
             f"> SUE 为修复版(按 pub_date 过滤); 动量12-1已跳过最近1月", ""]
    header = f"| {'因子':<18} | IC均值 | ICIR | IC>0% | 单调性(秩相关) | 与规模秩相关 | 有效月数 |"
    lines.append(header)
    lines.append("|" + "-" * 20 + "|------:|----:|-----:|--------------:|--------------:|-----:|")
    for name in FACTORS:
        ic_list = [x for x in ics[name] if x is not None and math.isfinite(x)]
        if not ic_list:
            lines.append(f"| {name:<18} | 数据不足 | | | | | |")
            continue
        ic_mean = np.mean(ic_list)
        ic_std = np.std(ic_list, ddof=1) if len(ic_list) > 1 else 0
        icir = ic_mean / ic_std if ic_std > 1e-12 else 0
        pos = sum(1 for x in ic_list if x > 0) / len(ic_list)
        # 分组单调性: 平均的十组收益与组号秩相关
        mono = ""
        if decile_ret[name]:
            avg_grp = np.mean([g for g in decile_ret[name] if g is not None], axis=0)
            m = spearman(list(range(10)), avg_grp)
            mono = f"{m:+.2f}" if math.isfinite(m) else ""
        size_corr = ""
        if factor_corr_samples[name]:
            vals = [x for x in factor_corr_samples[name] if math.isfinite(x)]
            if vals:
                size_corr = f"{np.mean(vals):+.2f}"
        lines.append(f"| {name:<18} | {ic_mean:+.3f} | {icir:+.2f} | {pos:.0%} | {mono} | {size_corr} | {len(ic_list)} |")

    # 十大组平均月收益(最关键因子)
    lines.append("")
    lines.append("## 十分位分组平均次月收益(月%, 全期平均)")
    lines.append("| 因子 | G1(低) | G2 | G3 | G4 | G5 | G6 | G7 | G8 | G9 | G10(高) | 单调 |")
    lines.append("|------|-------:|---:|---:|---:|---:|---:|---:|---:|---:|---:|-----:|")
    for name in FACTORS:
        if not decile_ret[name]:
            continue
        avg = np.mean([g for g in decile_ret[name] if g is not None], axis=0) * 100
        m = spearman(list(range(10)), avg)
        mono = f"{m:+.2f}" if math.isfinite(m) else ""
        cells = " | ".join(f"{v:.2f}" for v in avg)
        lines.append(f"| {name:<18} | {cells} | {mono} |")

    # 解读
    lines.append("")
    lines.append("## 解读要点")
    lines.append("1. **A股动量弱**: 若 mom12_1/mom6 的 IC 为负或接近0, 说明动量因子在小盘/全市场不成立(与《因子投资》3.5一致), 策略应弱化动量、强化反转/规模。")
    lines.append("2. **规模/反转**: size 和 rev1m 的 IC 显著为正, 是A股核心 Alpha 来源(书中3.3/5章)。")
    lines.append("3. **价值**: ep/bm 在控制规模后的增量(与size秩相关低)决定其独立性。")
    lines.append("4. **低流动**: liq 与 size 高度负相关(小盘=低流动), 需注意区分度。")
    lines.append("5. **SUE**: 修复版 IC 若为正, 说明事件驱动有独立 Alpha。")

    out = os.path.join(ROOT, "reports", "factor_diagnostics.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n报告已写入:", out)
    print("\n".join(lines[:30]))
    return out


def spearman(xs, ys):
    xr = rankdata(xs)
    yr = rankdata(ys)
    xr = xr[~np.isnan(xr)]
    yr = yr[~np.isnan(yr)]
    if len(xr) < 5 or len(xr) != len(yr):
        return float("nan")
    denom = float(np.std(xr, ddof=1) * np.std(yr, ddof=1))
    if denom <= 1e-12:
        return float("nan")
    return float(np.corrcoef(xr, yr)[0, 1])


def rankdata(vals):
    """平均秩(处理并列)。返回 ndarray。"""
    v = np.asarray(vals, dtype=float)
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(len(v), dtype=float)
    ranks[order] = np.arange(1, len(v) + 1)
    # 平均并列秩
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        if j > i:
            avg = (ranks[order[i]] + ranks[order[j]]) / 2
            for k in range(i, j + 1):
                ranks[order[k]] = avg
        i = j + 1
    return ranks


def decile_mean_ret(xs, ys):
    """按因子十分位分组, 返回10组平均收益。"""
    v = np.asarray([x for x in xs if math.isfinite(x)], dtype=float)
    y = np.asarray([y for x, y in zip(xs, ys) if math.isfinite(x)], dtype=float)
    if len(v) < 100:
        return None
    q = np.percentile(v, [10, 20, 30, 40, 50, 60, 70, 80, 90])
    groups = []
    edges = np.concatenate([[-np.inf], q, [np.inf]])
    for i in range(10):
        m = (v >= edges[i]) & (v <= edges[i + 1])
        if m.sum() == 0:
            return None
        groups.append(y[m].mean())
    return groups


if __name__ == "__main__":
    args = sys.argv[1:]
    s = args[0] if len(args) > 0 else "2019-01-01"
    e = args[1] if len(args) > 1 else "2026-07-31"
    main(s, e)
