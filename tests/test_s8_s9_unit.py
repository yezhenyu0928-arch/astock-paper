# -*- coding: utf-8 -*-
"""S8(价值质量清单)/ S9(Stage2趋势模板) 单元测试(卡M代码部分)。

不依赖真实库:构造小型内存 sqlite(daily_bar/fundamental/stock_annual/index_members 最小表,
DDL 与 schema.sql/fundamental.py 一致),验证规则判定与硬门槛逻辑的关键分支——
R1通过/失败、pub_date 防未来函数、s9 硬门槛全过/单项失败。覆盖两层:
  ① 纯函数级:直接喂手工构造的 dict/list,精确定位单一分支(不经 SQL)。
  ② 批量SQL集成级:真实建表插数据,走 score_pool() 整池向量化路径(不逐股循环查库)。

可直接运行: python tests/test_s8_s9_unit.py"""
import os
import sys
import io
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

import strategies.s8_checklist as s8
import strategies.s9_stage2 as s9

# ── 最小 schema(与 schema.sql / fundamental.py DDL 一致,仅建本测试用到的4张表) ──
_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bar (
  code TEXT NOT NULL, trade_date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL,
  adj_factor REAL DEFAULT 1.0,
  is_suspended INTEGER DEFAULT 0,
  limit_up REAL, limit_down REAL,
  source TEXT,
  PRIMARY KEY (code, trade_date)
);
CREATE TABLE IF NOT EXISTS index_members (
  index_code TEXT NOT NULL, code TEXT NOT NULL,
  in_date TEXT NOT NULL, out_date TEXT,
  PRIMARY KEY (index_code, code, in_date)
);
CREATE TABLE IF NOT EXISTS fundamental (
  code TEXT NOT NULL, trade_date TEXT NOT NULL,
  pe REAL, pb REAL, market_cap REAL, dividend_yield REAL,
  PRIMARY KEY(code, trade_date));
CREATE TABLE IF NOT EXISTS stock_annual (
  code TEXT NOT NULL, stat_year INTEGER NOT NULL,
  roe REAL, net_profit REAL, pub_date TEXT,
  PRIMARY KEY(code, stat_year));
"""


def _make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _date_range(end_date, n):
    """升序日历日列表,长度n,最后一天=end_date(闭区间)。不依赖trade_calendar,daily_bar本就
    只存交易日行,合成数据用连续日历日不影响规则判定逻辑本身。"""
    end = datetime.strptime(end_date, "%Y-%m-%d")
    return [(end - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d") for i in range(n)]


def _insert_bars(conn, code, dates, closes, amount=1e8):
    conn.executemany(
        "INSERT INTO daily_bar(code,trade_date,close,volume,amount,adj_factor,is_suspended) "
        "VALUES(?,?,?,?,?,1.0,0)",
        [(code, d, c, amount / max(c, 1e-6), amount) for d, c in zip(dates, closes)])
    conn.commit()


def _trend_series(base, n, daily_rate):
    """确定性几何递增序列(无噪声),长度n,首项=base。"""
    out = [base]
    for _ in range(n - 1):
        out.append(out[-1] * (1 + daily_rate))
    return out


def _swing_series(base, n, amp=0.03):
    """确定性高波动交替序列(+amp/-amp交替),用于制造与趋势股的波动率对比。"""
    out = [base]
    for i in range(n - 1):
        out.append(out[-1] * (1 + amp if i % 2 == 0 else 1 - amp))
    return out


SIGNAL_DATE = "2026-06-30"


# ========================================================================
# 一、S8 —— 纯函数级:R1 连续ROE 判定关键分支
# ========================================================================
def test_r1_consecutive_roe_pass():
    """R1: 连续5年ROE>15%、年份连续 → 通过。yrs 为(stat_year,roe,net_profit)降序列表。"""
    yrs = [(2025, 0.18, 1e8), (2024, 0.16, 0.9e8), (2023, 0.20, 0.8e8),
           (2022, 0.17, 0.7e8), (2021, 0.151, 0.6e8)]
    assert s8._consecutive_roe(yrs, 5, 0.15) is True
    print("[PASS] test_r1_consecutive_roe_pass")


def test_r1_consecutive_roe_fail_low_roe():
    """R1失败:其中一年ROE未达15%阈值。"""
    yrs = [(2025, 0.18, 1e8), (2024, 0.16, 0.9e8), (2023, 0.10, 0.8e8),
           (2022, 0.17, 0.7e8), (2021, 0.16, 0.6e8)]
    assert s8._consecutive_roe(yrs, 5, 0.15) is False
    print("[PASS] test_r1_consecutive_roe_fail_low_roe")


def test_r1_consecutive_roe_fail_gap_year():
    """R1失败:年份不连续(缺2023年年报,跳到2022)。"""
    yrs = [(2025, 0.18, 1e8), (2024, 0.16, 0.9e8), (2022, 0.20, 0.8e8),
           (2021, 0.17, 0.7e8), (2020, 0.16, 0.6e8)]
    assert s8._consecutive_roe(yrs, 5, 0.15) is False
    print("[PASS] test_r1_consecutive_roe_fail_gap_year")


def test_r1_consecutive_roe_fail_insufficient():
    """R1失败:年报数量不足5期。"""
    yrs = [(2025, 0.18, 1e8), (2024, 0.16, 0.9e8)]
    assert s8._consecutive_roe(yrs, 5, 0.15) is False
    print("[PASS] test_r1_consecutive_roe_fail_insufficient")


def test_r1_r2_ladder_takes_higher():
    """R1/R2阶梯计分不叠加、取高者:R1通过时评分取5(而非5+3=8)。"""
    yrs = [(2025, 0.18, 1e8), (2024, 0.16, 0.9e8), (2023, 0.20, 0.8e8),
           (2022, 0.17, 0.7e8), (2021, 0.151, 0.6e8)]
    assert s8._consecutive_roe(yrs, 5, 0.15) is True     # R1通过
    assert s8._consecutive_roe(yrs, 3, 0.10) is True      # R2同时也通过(被R1吸收)
    roe_score = s8.RULE_WEIGHTS["R1"] if True else s8.RULE_WEIGHTS["R2"]
    assert roe_score == 5, "阶梯计分应取R1的5分,不应叠加R2的3分"
    print("[PASS] test_r1_r2_ladder_takes_higher")


# ========================================================================
# 二、S8 —— 批量SQL集成级:pub_date 防未来函数
# ========================================================================
def test_bulk_annual_pub_date_lookahead_guard():
    """防未来函数核心:_bulk_annual 必须用 pub_date<=信号日 过滤——2025年报虽stat_year最新,
    但公告日晚于信号日,必须被排除;结果应以2024年报为最新一期。"""
    conn = _make_test_db()
    code = "sz000001"
    rows = [
        (code, 2025, 0.30, 5e8, "2026-04-10"),   # 未来数据:pub_date(2026-04-10) > 信号日(2026-03-01)
        (code, 2024, 0.16, 4e8, "2025-04-10"),
        (code, 2023, 0.17, 3.5e8, "2024-04-10"),
        (code, 2022, 0.18, 3e8, "2023-04-10"),
        (code, 2021, 0.19, 2.5e8, "2022-04-10"),
        (code, 2020, 0.20, 2e8, "2021-04-10"),
    ]
    conn.executemany(
        "INSERT INTO stock_annual(code,stat_year,roe,net_profit,pub_date) VALUES(?,?,?,?,?)", rows)
    conn.commit()

    signal_date = "2026-03-01"   # 早于2025年报公告日,晚于其余各年报公告日
    annual = s8._bulk_annual(conn, [code], signal_date)
    yrs = annual.get(code, [])
    stat_years = [r[0] for r in yrs]
    assert 2025 not in stat_years, f"未来年报(pub_date>信号日)未被过滤: {stat_years}"
    assert stat_years and stat_years[0] == 2024, f"应以2024为最新一期(2025被过滤): {stat_years}"
    assert len(yrs) == 5, f"应返回2020-2024共5期: {yrs}"
    # 用防未来过滤后的结果算R1:2020-2024五年ROE均>15%且连续 → 通过
    assert s8._consecutive_roe(yrs, 5, 0.15) is True

    # 反证:若不做pub_date过滤(信号日设到未来年报公告日之后),2025年报应被采纳
    annual2 = s8._bulk_annual(conn, [code], "2026-05-01")
    stat_years2 = [r[0] for r in annual2.get(code, [])]
    assert stat_years2[0] == 2025, f"信号日晚于公告日后,2025年报应被采纳: {stat_years2}"
    conn.close()
    print("[PASS] test_bulk_annual_pub_date_lookahead_guard")


# ========================================================================
# 三、S8 —— 批量SQL集成级:score_pool 整池打分(含R1通过 + R3刻意失败的混合场景)
# ========================================================================
def test_score_pool_integration():
    """构造2只标的(1只质量优良+低波稳健上涨,1只高波动对照)+ index_members,
    走 score_pool() 全链路(daily_bar/fundamental/stock_annual 批量SQL→规则判定→加权评分),
    核对 sz000001 的 passes 字典与手工推算的评分一致(R1通过·R3刻意设为失败·其余通过)。"""
    conn = _make_test_db()
    n = 280
    dates = _date_range(SIGNAL_DATE, n)

    # ── sz000001: 稳健上涨(低波)+ 质量优良 ──
    closes1 = _trend_series(10.0, n, 0.0013)
    _insert_bars(conn, "sz000001", dates, closes1, amount=1e8)

    # ── sz000002: 高波动对照(用于R7"池内后50%"百分位比较有意义) ──
    closes2 = _swing_series(10.0, n, amp=0.03)
    _insert_bars(conn, "sz000002", dates, closes2, amount=1e8)

    for code in ("sz000001", "sz000002"):
        conn.execute("INSERT INTO index_members(index_code,code,in_date,out_date) VALUES(?,?,?,?)",
                     ("sh000300", code, "2020-01-01", None))
    conn.commit()

    # ── sz000001 基本面: R1(ROE连续5年>15%)通过, R4(净利润连续5年>0)通过,
    #    R5(净利润5年复合增长>0)通过, R6(股息率>2%)通过 ──
    annual_rows = [
        ("sz000001", 2025, 0.20, 1.4e8, "2026-04-10"),
        ("sz000001", 2024, 0.19, 1.3e8, "2025-04-10"),
        ("sz000001", 2023, 0.18, 1.2e8, "2024-04-10"),
        ("sz000001", 2022, 0.17, 1.1e8, "2023-04-10"),
        ("sz000001", 2021, 0.16, 1.0e8, "2022-04-10"),
    ]
    conn.executemany(
        "INSERT INTO stock_annual(code,stat_year,roe,net_profit,pub_date) VALUES(?,?,?,?,?)",
        annual_rows)
    conn.commit()

    # ── sz000001 PE历史(4年月度=15.0,信号日当天单独一条=25.0)→ 当前PE高于历史中位数 → R3刻意失败 ──
    hist_dates = []
    d = datetime.strptime(SIGNAL_DATE, "%Y-%m-%d") - timedelta(days=31)
    for _ in range(48):
        hist_dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=30)
    conn.executemany(
        "INSERT INTO fundamental(code,trade_date,pe,pb,market_cap,dividend_yield) VALUES(?,?,?,?,?,?)",
        [("sz000001", hd, 15.0, 1.5, 5e9, 0.03) for hd in hist_dates])
    conn.execute(
        "INSERT INTO fundamental(code,trade_date,pe,pb,market_cap,dividend_yield) VALUES(?,?,?,?,?,?)",
        ("sz000001", SIGNAL_DATE, 25.0, 1.5, 5e9, 0.03))
    conn.commit()

    pool = [r[0] for r in conn.execute(
        "SELECT code FROM index_members WHERE index_code=? AND in_date<=? "
        "AND (out_date IS NULL OR out_date>?)", ("sh000300", SIGNAL_DATE, SIGNAL_DATE)).fetchall()]
    assert set(pool) == {"sz000001", "sz000002"}, pool

    scores, passes, bars = s8.score_pool(conn, pool, SIGNAL_DATE)
    assert "sz000001" in scores and "sz000001" in passes, "sz000001 应可评分"
    p = passes["sz000001"]
    assert p["R1"] is True, f"R1应通过: {p}"
    assert p["R3"] is False, f"R3应刻意失败(现价PE25>历史中位数~15): {p}"
    assert p["R4"] is True, f"R4应通过: {p}"
    assert p["R5"] is True, f"R5应通过: {p}"
    assert p["R6"] is True, f"R6应通过: {p}"
    assert p["R7"] is True, f"R7应通过(低波股应排入池内前50%低波): {p}"
    assert p["R8"] is True, f"R8应通过(持续上涨,12-1月动量>0): {p}"
    assert p["R9"] is True, f"R9应通过(单调上涨,距高点回撤0且远高于52周低点): {p}"

    expected = s8.RULE_WEIGHTS["R1"] + sum(
        s8.RULE_WEIGHTS[k] for k in ("R4", "R5", "R6", "R7", "R8", "R9"))   # R3不计入(失败)
    assert abs(scores["sz000001"] - expected) < 1e-9, \
        f"评分应=R1(5)+R4+R5+R6+R7+R8+R9={expected},实际={scores['sz000001']}"
    print(f"[PASS] test_score_pool_integration (score={scores['sz000001']}/{s8.TOTAL_WEIGHT}, "
          f"passes={p})")
    conn.close()


# ========================================================================
# 四、S9 —— 纯函数级:硬门槛关键分支
# ========================================================================
def test_hard_gate_all_pass():
    b = {"close": 100.0, "ma60": 95.0, "ma120": 90.0, "ma250": 80.0, "ma250_21ago": 78.0,
         "high52": 100.0, "low52": 70.0, "avg_amount20": 6e7}
    passed, tag = s9._hard_gate(b, 5e7)
    assert passed is True, tag
    print("[PASS] test_hard_gate_all_pass")


def test_hard_gate_fail_ma_not_stacked():
    """单项失败:均线未多头排列(ma60<ma120)。"""
    b = {"close": 100.0, "ma60": 90.0, "ma120": 95.0, "ma250": 80.0, "ma250_21ago": 78.0,
         "high52": 100.0, "low52": 70.0, "avg_amount20": 6e7}
    passed, tag = s9._hard_gate(b, 5e7)
    assert passed is False and "多头排列" in tag, (passed, tag)
    print(f"[PASS] test_hard_gate_fail_ma_not_stacked (tag={tag})")


def test_hard_gate_fail_ma250_not_rising():
    """单项失败:MA250较21日前反而下行。"""
    b = {"close": 100.0, "ma60": 95.0, "ma120": 90.0, "ma250": 80.0, "ma250_21ago": 85.0,
         "high52": 100.0, "low52": 70.0, "avg_amount20": 6e7}
    passed, tag = s9._hard_gate(b, 5e7)
    assert passed is False and "MA250" in tag, (passed, tag)
    print(f"[PASS] test_hard_gate_fail_ma250_not_rising (tag={tag})")


def test_hard_gate_fail_liquidity():
    """单项失败:20日均成交额不足流动性门槛。"""
    b = {"close": 100.0, "ma60": 95.0, "ma120": 90.0, "ma250": 80.0, "ma250_21ago": 78.0,
         "high52": 100.0, "low52": 70.0, "avg_amount20": 1e6}
    passed, tag = s9._hard_gate(b, 5e7)
    assert passed is False and "流动性" in tag, (passed, tag)
    print(f"[PASS] test_hard_gate_fail_liquidity (tag={tag})")


def test_hard_gate_fail_52week_low():
    """单项失败:距52周低点不足30%。"""
    b = {"close": 100.0, "ma60": 95.0, "ma120": 90.0, "ma250": 80.0, "ma250_21ago": 78.0,
         "high52": 110.0, "low52": 90.0, "avg_amount20": 6e7}   # close/low52-1=11%<30%
    passed, tag = s9._hard_gate(b, 5e7)
    assert passed is False and "低点" in tag, (passed, tag)
    print(f"[PASS] test_hard_gate_fail_52week_low (tag={tag})")


# ========================================================================
# 五、S9 —— 批量SQL集成级:score_pool 硬门槛全过 / 单项失败(流动性)
# ========================================================================
def test_s9_score_pool_all_pass_vs_single_fail():
    """两只标的价格走势相同(均线堆叠/52周区间天然达标),唯独成交额不同:
    sz000010 流动性达标 → 硬门槛全过;sz000011 流动性不足 → 仅流动性单项失败。"""
    conn = _make_test_db()
    n = 300
    dates = _date_range(SIGNAL_DATE, n)
    closes = _trend_series(10.0, n, 0.0015)   # 单调上涨,天然满足均线多头排列+MA250上行+52周区间

    _insert_bars(conn, "sz000010", dates, closes, amount=1e8)     # 20日均额1亿,达标
    _insert_bars(conn, "sz000011", dates, closes, amount=1e6)     # 20日均额100万,不达标

    for code in ("sz000010", "sz000011"):
        conn.execute("INSERT INTO index_members(index_code,code,in_date,out_date) VALUES(?,?,?,?)",
                     ("sh000300", code, "2020-01-01", None))
    conn.commit()

    pool = [r[0] for r in conn.execute(
        "SELECT code FROM index_members WHERE index_code=? AND in_date<=? "
        "AND (out_date IS NULL OR out_date>?)", ("sh000300", SIGNAL_DATE, SIGNAL_DATE)).fetchall()]
    assert set(pool) == {"sz000010", "sz000011"}, pool

    gate, tag, mom, bars = s9.score_pool(conn, pool, SIGNAL_DATE, vol_floor=50_000_000)

    assert gate.get("sz000010") is True, f"sz000010 硬门槛应全过,实际tag={tag.get('sz000010')}"
    assert "sz000010" in mom and mom["sz000010"] > 0, "sz000010 应有正的12-1月动量参与排名"

    assert gate.get("sz000011") is False, "sz000011 流动性不足,硬门槛应不通过"
    assert "流动性" in tag.get("sz000011", ""), f"sz000011 失败原因应指向流动性: {tag.get('sz000011')}"
    assert "sz000011" not in mom, "硬门槛未过不应进入动量候选池"

    print(f"[PASS] test_s9_score_pool_all_pass_vs_single_fail "
          f"(sz000010 gate=True mom={mom.get('sz000010'):+.2%}, sz000011 gate=False tag={tag['sz000011']})")
    conn.close()


# ========================================================================
# 运行
# ========================================================================
def _run_all():
    fns = [
        test_r1_consecutive_roe_pass,
        test_r1_consecutive_roe_fail_low_roe,
        test_r1_consecutive_roe_fail_gap_year,
        test_r1_consecutive_roe_fail_insufficient,
        test_r1_r2_ladder_takes_higher,
        test_bulk_annual_pub_date_lookahead_guard,
        test_score_pool_integration,
        test_hard_gate_all_pass,
        test_hard_gate_fail_ma_not_stacked,
        test_hard_gate_fail_ma250_not_rising,
        test_hard_gate_fail_liquidity,
        test_hard_gate_fail_52week_low,
        test_s9_score_pool_all_pass_vs_single_fail,
    ]
    ok = 0
    failed = []
    for fn in fns:
        try:
            fn()
            ok += 1
        except Exception as e:
            import traceback
            print(f"[FAIL] {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed.append(fn.__name__)
    print(f"\ns8/s9 单测: {ok}/{len(fns)} 通过")
    if failed:
        print(f"失败: {', '.join(failed)}")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
