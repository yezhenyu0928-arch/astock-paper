# -*- coding: utf-8 -*-
"""模拟撮合引擎(SPEC 模块2 + SPEC_FILL F1/F2)。
- DataContext:策略取数接口(防未来函数,只给 signal date 及之前的数据)。
- Engine:账户加载/保存、settle(开盘撮合)、run_strategies(收盘信号)。
所有金额 round 2 位;股数 int;nav 用**不复权收盘价**计。
"""
import os
import csv
import json
import math
import logging
import importlib
from dataclasses import asdict

import util
import conf
import trade_calendar as cal
from db import get_conn, init_db
from models import Order, Position, Account

log = logging.getLogger("engine")

TRADE_LOG = conf.STATE_DIR / "trade_log.csv"
TRADE_LOG_COLS = ["signal_date", "trade_date", "strategy_id", "code", "side", "shares",
                  "sim_price", "fee", "tax", "status", "reason", "real_price"]
MAX_DEFER = 5   # 一字跌停/停牌顺延上限,第 6 日强制成交(SPEC_FILL F1.3)


# ============================ DataContext ============================
class SqlContext:
    """base.DataContext 的实现。date=当前信号日,所有取数 <= date(防未来函数)。"""

    def __init__(self, date, conn, cfg=None, bar_cache=None):
        self.date = util.to_date_str(date)
        self.conn = conn
        self.cfg = cfg or conf.load_config()
        self._sec = None
        self._bc = bar_cache        # 非 None=启用内存K线缓存(回测提速;仅历史静态数据可用)

    # ---- 内存缓存(opt-in) ----
    def _rows(self, code):
        """该 code 全历史行(升序),缓存。列: date,o,h,l,c,vol,amt,lu,ld,susp,adj。"""
        r = self._bc.get(code)
        if r is None:
            data = self.conn.execute(
                "SELECT trade_date,open,high,low,close,volume,amount,limit_up,limit_down,"
                "is_suspended,adj_factor FROM daily_bar WHERE code=? ORDER BY trade_date", (code,)).fetchall()
            dates = [x[0] for x in data]
            r = (dates, data)
            self._bc[code] = r
        return r

    def _idx_le(self, dates):
        import bisect
        return bisect.bisect_right(dates, self.date)

    # ---- base 抽象方法 ----
    def close(self, code: str, n: int):
        """截至 date(含)最近 n 个后复权收盘价,升序。"""
        if self._bc is not None:
            dates, data = self._rows(code)
            i = self._idx_le(dates)
            return [util.r2(data[j][4] * (data[j][10] or 1.0)) for j in range(max(0, i - n), i)]
        rows = self.conn.execute(
            "SELECT close, adj_factor FROM daily_bar WHERE code=? AND trade_date<=? "
            "ORDER BY trade_date DESC LIMIT ?", (code, self.date, n)).fetchall()
        rows = rows[::-1]
        return [util.r2(r[0] * (r[1] if r[1] else 1.0)) for r in rows]

    def bar(self, code: str, date: str):
        date = util.to_date_str(date)
        if self._bc is not None:
            import bisect
            dates, data = self._rows(code)
            i = bisect.bisect_left(dates, date)
            if i < len(dates) and dates[i] == date:
                r = data[i]
            else:
                return None
        else:
            r = self.conn.execute(
                "SELECT open,high,low,close,volume,amount,limit_up,limit_down,is_suspended,adj_factor "
                "FROM daily_bar WHERE code=? AND trade_date=?", (code, date)).fetchone()
            if r is None:
                return None
            r = (date,) + tuple(r)          # 对齐缓存行的列偏移(首列为date)
        return {"open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5],
                "amount": r[6], "limit_up": r[7], "limit_down": r[8],
                "is_suspended": r[9], "adj_factor": r[10] or 1.0}

    def members(self, index_code: str, date: str):
        date = util.to_date_str(date)
        rows = self.conn.execute(
            "SELECT code FROM index_members WHERE index_code=? AND in_date<=? "
            "AND (out_date IS NULL OR out_date>?)", (index_code, date, date)).fetchall()
        return [r[0] for r in rows]

    def _sec_info(self, code):
        if self._sec is None:
            self._sec = {}
            for r in self.conn.execute("SELECT code,name,type,is_t0,list_date,status FROM security").fetchall():
                self._sec[r[0]] = {"name": r[1], "type": r[2], "is_t0": r[3],
                                   "list_date": r[4], "status": r[5]}
        return self._sec.get(code)

    def is_t0(self, code):
        s = self._sec_info(code)
        return bool(s and s.get("is_t0"))

    def is_tradable(self, code: str, date: str) -> bool:
        """未停牌、未退市、上市满60日、非ST、非北交所。
        若 config.custom.exclude_star_chinext=true,则科创板(688)/创业板(300/301)也不可买
        (用户未开通这些权限时用;卖出不受限,见策略)。"""
        date = util.to_date_str(date)
        cust = self.cfg.get("custom") or {}
        if util.is_bj(code):
            return False
        if cust.get("exclude_star_chinext") and util.is_star_or_chinext(code):
            return False
        if code in (cust.get("universe_exclude") or []):
            return False
        b = self.bar(code, date)
        if b is None or b.get("is_suspended"):
            return False
        s = self._sec_info(code)
        if s:
            if s.get("status") in ("D", "ST"):
                return False
            ld = s.get("list_date")
            if ld:
                # 上市满60自然日
                if (util.to_date_str(date) < ld) or _days_between(ld, date) < 60:
                    return False
        return True

    def avg_amount(self, code: str, n: int) -> float:
        return self._avg_col(code, n, 6, "amount")

    def avg_volume(self, code: str, n: int) -> float:
        return self._avg_col(code, n, 5, "volume")

    def _avg_col(self, code, n, col, name):
        if self._bc is not None:
            dates, data = self._rows(code)
            i = self._idx_le(dates)
            vals = [data[j][col] for j in range(max(0, i - n), i) if data[j][col] is not None]
            return float(sum(vals) / len(vals)) if vals else 0.0
        r = self.conn.execute(
            f"SELECT avg({name}) FROM (SELECT {name} FROM daily_bar WHERE code=? AND trade_date<=? "
            "ORDER BY trade_date DESC LIMIT ?)", (code, self.date, n)).fetchone()
        return float(r[0]) if r and r[0] else 0.0

    def fundamental(self, code: str):
        """截至 date 最近基本面(pe/pb/market_cap/dividend_yield),防未来函数。"""
        import fundamental as F
        return F.get_fundamental(code, self.date, self.conn)

    def dividend_years(self, code: str, years: int) -> int:
        """近 years 年中,每年至少有一次现金分红的年份数(用于连续分红判断)。
        无分红数据(海外 akshare/东财不可达)时从宽返回 years,避免据此全拒。"""
        lo = f"{int(self.date[:4]) - years}-{self.date[5:]}"
        rows = self.conn.execute(
            "SELECT substr(ex_date,1,4) y FROM dividend WHERE code=? AND ex_date>? AND ex_date<=? "
            "AND cash_per_share>0 GROUP BY y", (code, lo, self.date)).fetchall()
        if not rows:
            # 该票无分红记录:若分红表本身为空(海外缺失)→ 从宽通过
            cnt = self.conn.execute(
                "SELECT count(*) FROM dividend WHERE code=?", (code,)).fetchone()[0] or 0
            if cnt == 0:
                return years
        return len(rows)

    def is_last_trade_day_of_week(self, date: str) -> bool:
        return cal.last_trade_day_of_week(date)

    def is_last_trade_day_of_month(self, date: str) -> bool:
        return cal.last_trade_day_of_month(date)

    # ---- 便利方法(策略/引擎用) ----
    def raw_close(self, code, date=None):
        b = self.bar(code, date or self.date)
        return b["close"] if b else None

    def name(self, code):
        s = self._sec_info(code)
        return s["name"] if s else util.bare(code)


def _days_between(d1, d2):
    from datetime import datetime
    a = datetime.strptime(util.to_date_str(d1), "%Y-%m-%d")
    b = datetime.strptime(util.to_date_str(d2), "%Y-%m-%d")
    return abs((b - a).days)


# ============================ Engine ============================
class Engine:
    def __init__(self, config=None, registry=None, conn=None, cache_bars=False):
        self.cfg = config or conf.load_config()
        self.reg = registry or conf.load_registry()
        self._own_conn = conn is None
        self.conn = conn or get_conn()
        init_db(self.conn)
        self.state = {}          # sid -> state dict
        self._strategies = {}    # sid -> BaseStrategy 实例
        self._trade_log_keys = None
        # 内存K线缓存:仅回测(历史静态)开启,加速 ~10x;实盘/测试关闭(防数据更新后读到陈旧缓存)
        self._bar_cache = {} if cache_bars else None

    # ---------- 账户状态 ----------
    def _state_path(self, sid):
        return conf.STATE_DIR / f"{sid.replace('@','_at_')}.json"

    def load_account(self, strategy_id) -> Account:
        if strategy_id in self.state:
            return self.state[strategy_id]["account"]
        path = self._state_path(strategy_id)
        cap = float(self.cfg["user"]["capital"])
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            positions = {c: Position(**p) for c, p in d.get("positions", {}).items()}
            acct = Account(strategy_id=strategy_id, init_capital=d.get("init_capital", cap),
                           cash=d.get("cash", cap), positions=positions,
                           frozen=d.get("frozen", False), nav=d.get("nav", 1.0))
            st = {"account": acct, "nav_history": d.get("nav_history", []),
                  "pending": d.get("pending", []), "highest_nav": d.get("highest_nav", 1.0),
                  "settled_dates": set(d.get("settled_dates", [])),
                  "applied_div": set(d.get("applied_div", [])), "aux": d.get("aux", {})}
        else:
            acct = Account(strategy_id=strategy_id, init_capital=cap, cash=cap)
            st = {"account": acct, "nav_history": [], "pending": [], "highest_nav": 1.0,
                  "settled_dates": set(), "applied_div": set(), "aux": {}}
        self.state[strategy_id] = st
        return acct

    def save_account(self, account: Account):
        st = self.state[account.strategy_id]
        d = {
            "strategy_id": account.strategy_id, "init_capital": account.init_capital,
            "cash": util.r2(account.cash),
            "positions": {c: asdict(p) for c, p in account.positions.items()},
            "frozen": account.frozen, "nav": round(account.nav, 6),
            "nav_history": st["nav_history"], "pending": st["pending"],
            "highest_nav": st["highest_nav"], "settled_dates": sorted(st["settled_dates"]),
            "applied_div": sorted(st["applied_div"]), "aux": st.get("aux", {}),
        }
        self._state_path(account.strategy_id).write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

    def enabled_strategies(self):
        return [sid for sid, on in self.cfg.get("strategies", {}).items() if on]

    def get_strategy(self, sid):
        if sid in self._strategies:
            return self._strategies[sid]
        rc = self.reg[sid]
        module_name, cls_name = rc["class"].rsplit(".", 1)
        cls = getattr(importlib.import_module(module_name), cls_name)
        stg = cls()
        stg.strategy_id = sid
        stg.benchmark = rc.get("benchmark", "")
        stg.params = rc.get("params", {})
        stg.universe = rc.get("universe", [])
        stg.config = self.cfg
        self._strategies[sid] = stg
        return stg

    def ctx(self, date) -> SqlContext:
        return SqlContext(date, self.conn, self.cfg, bar_cache=self._bar_cache)

    # ---------- 价格 ----------
    def _price_of(self, date):
        """返回 code -> 截至 date 最近不复权收盘价(计 nav 用)。"""
        cache = {}

        def f(code):
            if code in cache:
                return cache[code]
            r = self.conn.execute(
                "SELECT close FROM daily_bar WHERE code=? AND trade_date<=? "
                "ORDER BY trade_date DESC LIMIT 1", (code, util.to_date_str(date))).fetchone()
            v = float(r[0]) if r else 0.0
            cache[code] = v
            return v
        return f

    # ---------- trade_log ----------
    def _load_trade_log_keys(self):
        if self._trade_log_keys is not None:
            return self._trade_log_keys
        keys = set()
        if TRADE_LOG.exists():
            with open(TRADE_LOG, "r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("status") in ("filled", "cancelled", "cut_liquidity"):
                        keys.add(f"{row['signal_date']}|{row['strategy_id']}|{row['code']}|{row['side']}")
        self._trade_log_keys = keys
        return keys

    def _append_trade_log(self, rows):
        exists = TRADE_LOG.exists()
        with open(TRADE_LOG, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_LOG_COLS)
            if not exists:
                w.writeheader()
            for r in rows:
                w.writerow(r)

    # ---------- 撮合建模(SPEC_FILL F1) ----------
    def _slippage(self, p, is_etf):
        base = self.cfg["costs"]["slippage"]["etf" if is_etf else "stock"]
        k = (self.cfg["custom"]["impact_k"]["etf" if is_etf else "stock"])
        if p <= 0.005:
            return base
        if p <= 0.02:
            return base + k * math.sqrt(p / 0.01)
        return base + k * math.sqrt(0.02 / 0.01)   # 上限(p>0.02 部分成交时用)

    def _fill(self, order, ctx, account, is_etf, is_t0, prev_close):
        """撮合单个 pending 订单。返回 (result_dict|None, keep_pending_bool)。
        result_dict 用于 trade_log 与账户更新。"""
        code, side = order["code"], order["side"]
        weight = order["weight"]
        bar = ctx.bar(code, ctx.date)
        if bar is None:                       # 无当日行情=停牌/缺数据 → 顺延
            return self._defer_or_force(order, ctx, account, is_etf, reason_no_bar=True)
        openp = bar["open"]
        lim_up, lim_dn = bar["limit_up"], bar["limit_down"]
        amount_open = (bar["amount"] or 0) * self.cfg["custom"]["open_frac"]["etf" if is_etf else "stock"]
        total = account.total(self._price_of(ctx.date))
        base_reason = order.get("reason", "")

        if side == "buy":
            # 昨收涨停 → 作废(SPEC 原规则)
            if prev_close is not None and lim_up and prev_close >= lim_up - 1e-6:
                return self._log_dict(order, ctx.date, 0, 0, 0, 0, "cancelled",
                                      base_reason + "[昨收涨停,买单作废]"), False
            # 一字涨停 → 成交概率≈0(F1.3)
            if lim_up and openp >= lim_up * 0.998:
                return self._log_dict(order, ctx.date, 0, 0, 0, 0, "cancelled",
                                      base_reason + "[一字涨停,无法买入]"), False
            if bar["is_suspended"]:
                return self._defer_or_force(order, ctx, account, is_etf, reason_no_bar=True)
            # 目标金额与股数
            target_amt = min(total * weight, account.cash, total * 0.98)  # 现金缓冲98%
            if target_amt < self.cfg["custom"]["min_order_amount"]:
                return self._log_dict(order, ctx.date, 0, 0, 0, 0, "cancelled",
                                      base_reason + f"[<最低单笔{self.cfg['custom']['min_order_amount']}元,跳过]"), False
            # 单手门槛剔除(F2.1):个股单手价 > 总资产×单票上限 → 不可买(ETF 是分散工具,豁免)
            if (not is_etf) and openp * 100 > total * self.cfg["risk"]["max_position_pct"]:
                return self._log_dict(order, ctx.date, 0, 0, 0, 0, "cancelled",
                                      base_reason + "[单手金额超单票上限,剔除]"), False
            # 冲击/滑点与部分成交
            p = target_amt / amount_open if amount_open > 0 else 1.0
            cut_note = ""
            if p > 0.02 and amount_open > 0:
                capped = 0.02 * amount_open
                cut_note = f"[流动性截断,原计划{target_amt:.0f}元仅成交{capped:.0f}元]"
                target_amt = capped
            slip = self._slippage(min(p, 0.02) if amount_open > 0 else 0.005, is_etf)
            price = openp * (1 + slip)
            if prev_close and openp > prev_close * 1.03:   # 高开>3% 追价(F1.3)
                price *= 1.002
            price = util.r2(price)
            shares = util.floor100(target_amt / price)
            if shares <= 0:
                return self._log_dict(order, ctx.date, 0, 0, 0, 0, "cancelled",
                                      base_reason + "[资金不足一手]"), False
            gross = price * shares
            fee = max(gross * self.cfg["costs"]["commission_rate"], self.cfg["costs"]["commission_min"])
            fee = util.r2(fee)
            status = "cut_liquidity" if cut_note else "filled"
            return self._log_dict(order, ctx.date, shares, price, fee, 0.0,
                                  status, base_reason + cut_note), False

        else:  # sell
            pos = account.positions.get(code)
            if pos is None or pos.shares <= 0:
                return self._log_dict(order, ctx.date, 0, 0, 0, 0, "cancelled",
                                      base_reason + "[无持仓,跳过]"), False
            # 一字跌停 / 停牌 → 顺延(F1.3)
            if bar["is_suspended"] or (lim_dn and openp <= lim_dn * 1.002):
                return self._defer_or_force(order, ctx, account, is_etf, sell_pos=pos)
            # T+1:当日买入且非T0 → 顺延
            if pos.buy_date == ctx.date and not is_t0:
                return self._defer_or_force(order, ctx, account, is_etf, sell_pos=pos, t1=True)
            shares = pos.shares if weight == 0 else min(pos.shares, util.floor100(account.total(self._price_of(ctx.date)) * weight / openp))
            if shares <= 0:
                shares = pos.shares
            p = (openp * shares) / amount_open if amount_open > 0 else 1.0
            slip = self._slippage(min(p, 0.02) if amount_open > 0 else 0.005, is_etf)
            price = openp * (1 - slip)
            if prev_close and openp < prev_close * 0.97:   # 低开>3% 折价(F1.3)
                price *= 0.998
            price = util.r2(price)
            gross = price * shares
            fee = util.r2(max(gross * self.cfg["costs"]["commission_rate"], self.cfg["costs"]["commission_min"]))
            tax = util.r2(gross * self.cfg["costs"]["stamp_tax_sell"])
            return self._log_dict(order, ctx.date, shares, price, fee, tax, "filled", base_reason), False

    def _defer_or_force(self, order, ctx, account, is_etf, reason_no_bar=False, sell_pos=None, t1=False):
        order["_defer"] = order.get("_defer", 0) + 1
        if order["_defer"] > MAX_DEFER and sell_pos is not None:
            bar = ctx.bar(order["code"], ctx.date)
            if bar:                                   # 第6日强制成交(如实记录深亏)
                openp = bar["open"]
                shares = sell_pos.shares
                gross = openp * shares
                fee = util.r2(max(gross * self.cfg["costs"]["commission_rate"], self.cfg["costs"]["commission_min"]))
                tax = util.r2(gross * self.cfg["costs"]["stamp_tax_sell"])
                return self._log_dict(order, ctx.date, shares, util.r2(openp), fee, tax,
                                      "filled", order.get("reason", "") + f"[顺延{MAX_DEFER}日强制成交]"), False
        why = "停牌/缺数据" if reason_no_bar else ("T+1未到" if t1 else "一字跌停")
        log.info("订单顺延(%s) %s %s", why, order["code"], order["side"])
        return None, True    # keep pending

    def _log_dict(self, order, trade_date, shares, price, fee, tax, status, reason):
        return {"signal_date": order["signal_date"], "trade_date": trade_date,
                "strategy_id": order["strategy_id"], "code": order["code"], "side": order["side"],
                "shares": int(shares), "sim_price": util.r2(price), "fee": util.r2(fee),
                "tax": util.r2(tax), "status": status, "reason": reason, "real_price": ""}

    # ---------- 除权 ----------
    def _apply_dividends(self, sid, account, date):
        st = self.state[sid]
        for code, pos in list(account.positions.items()):
            r = self.conn.execute(
                "SELECT cash_per_share,shares_ratio FROM dividend WHERE code=? AND ex_date=?",
                (code, date)).fetchone()
            if not r:
                continue
            key = f"{code}|{date}"
            if key in st["applied_div"]:
                continue
            cps, ratio = r[0] or 0, r[1] or 0
            if cps:
                account.cash = util.r2(account.cash + pos.shares * cps)   # 暂不扣红利税(简化)
            if ratio:
                new_shares = int(pos.shares * (1 + ratio))
                if new_shares > 0:
                    pos.avg_cost = util.r2(pos.avg_cost * pos.shares / new_shares)
                    pos.shares = new_shares
            st["applied_div"].add(key)

    # ---------- settle ----------
    def settle(self, date) -> list:
        """开盘撮合:昨日 pending + 今日除权 + 更新净值。幂等。返回本次新成交回报单。"""
        date = util.to_date_str(date)
        price_of = self._price_of(date)
        self._load_trade_log_keys()
        reports, log_rows = [], []
        for sid in self.enabled_strategies():
            acct = self.load_account(sid)
            st = self.state[sid]
            if date in st["settled_dates"]:
                continue
            ctx = self.ctx(date)
            self._apply_dividends(sid, acct, date)

            still_pending = []
            for order in st["pending"]:
                key = f"{order['signal_date']}|{order['strategy_id']}|{order['code']}|{order['side']}"
                if key in self._trade_log_keys:      # 幂等:已成交过
                    continue
                is_etf = _is_etf(order["code"])
                is_t0 = ctx.is_t0(order["code"])
                pc = self._prev_close(order["code"], date)
                res, keep = self._fill(order, ctx, acct, is_etf, is_t0, pc)
                if keep:
                    still_pending.append(order)
                    continue
                if res is None:
                    continue
                self._apply_fill(acct, res)
                log_rows.append(res)
                self._trade_log_keys.add(key)
                if res["status"] in ("filled", "cut_liquidity") and res["shares"] > 0:
                    reports.append(res)
            st["pending"] = still_pending

            # 更新持仓最高收盘 + nav
            self._update_highs(acct, date)
            nav = util.r2(acct.total(price_of)) / acct.init_capital
            acct.nav = round(nav, 6)
            st["highest_nav"] = max(st.get("highest_nav", 1.0), nav)
            _upsert_navhist(st["nav_history"], date, round(nav, 6))
            st["settled_dates"].add(date)
            self.save_account(acct)
        if log_rows:
            self._append_trade_log(log_rows)
        return reports

    def _apply_fill(self, account, res):
        code, shares = res["code"], res["shares"]
        if res["status"] not in ("filled", "cut_liquidity") or shares <= 0:
            return
        if res["side"] == "buy":
            cost = res["sim_price"] * shares + res["fee"]
            account.cash = util.r2(account.cash - cost)
            pos = account.positions.get(code)
            if pos:
                tot = pos.shares + shares
                pos.avg_cost = util.r2((pos.avg_cost * pos.shares + cost) / tot)
                pos.shares = tot
                pos.buy_date = res["trade_date"]
            else:
                account.positions[code] = Position(code=code, shares=shares,
                    avg_cost=util.r2(cost / shares), buy_date=res["trade_date"],
                    highest_close=res["sim_price"])
        else:  # sell
            pos = account.positions.get(code)
            if not pos:
                return
            proceeds = res["sim_price"] * shares - res["fee"] - res["tax"]
            account.cash = util.r2(account.cash + proceeds)
            pos.shares -= shares
            if pos.shares <= 0:
                del account.positions[code]

    def _update_highs(self, account, date):
        for code, pos in account.positions.items():
            c = self._prev_close_incl(code, date)
            if c:
                pos.highest_close = max(pos.highest_close or 0, c)

    def _prev_close(self, code, date):
        r = self.conn.execute(
            "SELECT close FROM daily_bar WHERE code=? AND trade_date<? ORDER BY trade_date DESC LIMIT 1",
            (code, date)).fetchone()
        return float(r[0]) if r else None

    def _prev_close_incl(self, code, date):
        r = self.conn.execute(
            "SELECT close FROM daily_bar WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
            (code, date)).fetchone()
        return float(r[0]) if r else None

    # ---------- run_strategies ----------
    def run_strategies(self, date) -> list:
        """收盘信号:risk.pre_check → 各策略 generate_orders → risk.post_check → 存 pending → 返回推送单。"""
        import risk
        date = util.to_date_str(date)
        for sid in self.enabled_strategies():
            self.load_account(sid)
            # 幂等:清掉本日已生成的 pending(重跑替换,不重复挂单),保留更早的 deferred
            st = self.state[sid]
            st["pending"] = [o for o in st["pending"] if o.get("signal_date") != date]
        states = {sid: self.state[sid] for sid in self.enabled_strategies()}
        accounts = {sid: st["account"] for sid, st in states.items()}
        ctx = self.ctx(date)
        pre = risk.pre_check(date, ctx, states, self.cfg)

        all_orders = []
        for sid in self.enabled_strategies():
            acct = accounts[sid]
            if acct.frozen:
                continue
            try:
                stg = self.get_strategy(sid)
                orders = stg.generate_orders(date, ctx, acct) or []
            except Exception as e:
                log.error("策略 %s 生成信号失败: %s", sid, e)
                orders = []
            for o in orders:
                o.signal_date = date
            all_orders.extend(orders)

        # 强制清仓单(熔断/黑天鹅)并入
        all_orders.extend(pre.get("forced_orders", []))
        kept = risk.post_check(date, ctx, all_orders, states, self.cfg, market_frozen=pre.get("market_frozen", False))

        # 存入各账户 pending(signal_date=date)
        for o in kept:
            st = self.state.get(o.strategy_id)
            if st is None:
                self.load_account(o.strategy_id); st = self.state[o.strategy_id]
            d = o.to_dict()
            d["_defer"] = 0
            st["pending"].append(d)
        for sid in self.enabled_strategies():
            self.save_account(accounts[sid])
        return kept

    def close(self):
        if self._own_conn:
            self.conn.close()


# ---------- 模块级工具 ----------
def _is_etf(code):
    six = util.bare(code)
    return six[0] == "5" or six[:2] in ("15", "16", "18")


def _upsert_navhist(hist, date, nav):
    if hist and hist[-1][0] == date:
        hist[-1][1] = nav
    else:
        hist.append([date, nav])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    e = Engine()
    print("enabled:", e.enabled_strategies())
    e.close()
