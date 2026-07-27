# -*- coding: utf-8 -*-
"""分钟级数据 + "峰岭谷"日内择时因子(开源金工思路的轻量落地)。

设计目标(回答用户"云端分钟数据为什么拿不到"):
- GitHub Actions 的 Runner 在美国,baostock/东财 push2his 常不可达;
- 但 **Sina(money.finance.sina.com.cn) 与 腾讯(qt.gtimg.cn) 是全球 CDN,美国也可访问**,
  实测均返回 2026-07-27 当日真实 5 分钟 K 线。故分钟数据在云端完全可取得。
- 本模块以 Sina 为主源、腾讯/东财为兜底,让 run_intraday 在云端也能算日内信号并推送。

峰岭谷逻辑:在当日 5 分钟收盘序列上找局部峰/谷(窗口极值),
- 当前价贴近"最近谷"且明显低于"最近峰" → 买点(抄日内回撤);
- 当前价贴近"最近峰"且明显高于"最近谷" → 卖点(兑现日内冲高);
- 否则持有。叠加"当日涨跌幅"做风控过滤(暴涨后不再追买)。
"""
import time
import logging
import requests

import util

log = logging.getLogger("minute_factor")

_MIN_URL_SINA = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
                  "/CN_MarketData.getKLineData")
_MIN_URL_TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
_MIN_URL_EM = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

# Sina 一次最多 datalen 条;全天约 240 根 5 分钟,取 260 保险
_SINA_DATALEN = 260
_EM_LMT = 260
_W = 3  # 峰谷判定的局部窗口(±W 根)


def _to_sina(code: str) -> str:
    # sh600519 / sz000001 直接可用
    return code


def _to_em_secid(code: str) -> str:
    bare = util.bare(code)
    return ("1." if code.startswith("sh") else "0.") + bare


def _parse_sina(text: str):
    """Sina getKLineData -> [{t,o,h,l,c,v}]。"""
    import json
    out = []
    try:
        arr = json.loads(text)
    except Exception:
        return out
    for r in arr:
        try:
            out.append({
                "t": r["day"],
                "o": float(r["open"]), "h": float(r["high"]),
                "l": float(r["low"]), "c": float(r["close"]),
                "v": float(r.get("volume", 0) or 0),
            })
        except Exception:
            continue
    return out


def _parse_tencent(text: str):
    """腾讯 minute/query -> 当日 1 分钟序列,升采样为 5 分钟聚合。"""
    import json
    out = []
    try:
        j = json.loads(text)
        data = j["data"]
        code = list(data.keys())[0]
        rows = data[code]["data"]["data"]
    except Exception:
        return out
    # 每行: "HHMM price volume amount"
    cur_bucket = None
    agg = None
    for row in rows:
        parts = row.split()
        if len(parts) < 4:
            continue
        hhmm, price, vol, amt = parts[0], parts[1], parts[2], parts[3]
        try:
            px = float(price); vv = float(vol)
        except Exception:
            continue
        minute = int(hhmm[:2]) * 60 + int(hhmm[2:4])
        bucket = (minute // 5) * 5
        if bucket != cur_bucket:
            if agg:
                out.append(agg)
            cur_bucket = bucket
            agg = {"t": f"{hhmm[:2]}:{hhmm[2:4]}", "o": px, "h": px,
                   "l": px, "c": px, "v": vv}
        else:
            agg["h"] = max(agg["h"], px)
            agg["l"] = min(agg["l"], px)
            agg["c"] = px
            agg["v"] += vv
    if agg:
        out.append(agg)
    return out


def _parse_em(text: str):
    """东财 kline klt=5 -> [{t,o,h,l,c,v}]。"""
    import json
    out = []
    try:
        j = json.loads(text)
        klines = j["data"]["klines"]
    except Exception:
        return out
    for kl in klines:
        f = kl.split(",")
        if len(f) < 7:
            continue
        try:
            out.append({
                "t": f[0], "o": float(f[1]), "c": float(f[2]),
                "h": float(f[3]), "l": float(f[4]), "v": float(f[5]),
            })
        except Exception:
            continue
    return out


def _fetch_sina(codes, freq=5):
    res = {}
    for code in codes:
        try:
            r = requests.get(_MIN_URL_SINA, params={
                "symbol": _to_sina(code), "scale": freq,
                "ma": "no", "datalen": _SINA_DATALEN,
            }, timeout=8)
            bars = _parse_sina(r.text)
            if bars:
                res[code] = bars
        except Exception as e:
            log.warning("Sina 分钟 %s 失败:%s", code, e)
    return res


def _fetch_tencent(codes, freq=5):
    res = {}
    for code in codes:
        try:
            r = requests.get(_MIN_URL_TENCENT, params={"code": code}, timeout=8)
            bars = _parse_tencent(r.text)
            if bars:
                res[code] = bars
        except Exception as e:
            log.warning("腾讯分钟 %s 失败:%s", code, e)
    return res


def _fetch_em(codes, freq=5):
    res = {}
    for code in codes:
        try:
            r = requests.get(_MIN_URL_EM, params={
                "secid": _to_em_secid(code),
                "fields1": "f1", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "klt": freq, "fqt": 0, "end": "20500101", "lmt": _EM_LMT,
            }, timeout=8)
            bars = _parse_em(r.text)
            if bars:
                res[code] = bars
        except Exception as e:
            log.warning("东财分钟 %s 失败:%s", code, e)
    return res


def fetch_minute_today(codes, freq=5, cfg=None):
    """拉取当日分钟线。多源兜底:Sina(全球CDN,主)→腾讯→东财。
    返回 {code: [{t,o,h,l,c,v}, ...]}。"""
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return {}
    # 源优先级:云端(美国)Sina/腾讯最稳,东财兜底
    sources = [
        ("sina", _fetch_sina),
        ("tencent", _fetch_tencent),
        ("eastmoney", _fetch_em),
    ]
    out = {}
    for name, fn in sources:
        missing = [c for c in codes if c not in out]
        if not missing:
            break
        try:
            part = fn(missing, freq)
            if part:
                out.update(part)
                log.info("分钟源 %s 命中 %d/%d", name, len(part), len(missing))
        except Exception as e:
            log.warning("分钟源 %s 异常:%s", name, e)
    return out


def peak_valley_signal(bars, cur_px=None):
    """峰岭谷日内信号。bars=当日 5 分钟序列。返回可推送的信号 dict。"""
    if not bars or len(bars) < 2 * _W + 1:
        return {"signal": "hold", "reason": "分钟数据不足", "cur": cur_px}
    closes = [b["c"] for b in bars]
    cur = float(cur_px) if cur_px else closes[-1]
    open_px = closes[0]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]

    valleys, peaks = [], []
    for i in range(_W, len(closes) - _W):
        seg = closes[i - _W:i + _W + 1]
        if closes[i] <= min(seg):
            valleys.append((i, closes[i]))
        if closes[i] >= max(seg):
            peaks.append((i, closes[i]))
    last_valley = valleys[-1] if valleys else (0, lows[0])
    last_peak = peaks[-1] if peaks else (len(closes) - 1, highs[-1])

    day_chg = cur / open_px - 1 if open_px else 0.0
    dist_valley = cur / last_valley[1] - 1 if last_valley[1] else 0.0
    dist_peak = cur / last_peak[1] - 1 if last_peak[1] else 0.0

    # 信号判定:近谷+远低于峰→买;近峰+远高于谷→卖;否则持有
    if dist_valley <= 0.006 and dist_peak <= -0.012:
        signal = "buy"
        reason = (f"贴近日内谷({last_valley[1]:.2f})且低于峰({last_peak[1]:.2f})"
                  f"{abs(dist_peak)*100:.1f}%,可逢低介入")
    elif dist_peak >= -0.006 and dist_valley >= 0.012:
        signal = "sell"
        reason = (f"贴近日内峰({last_peak[1]:.2f})且高于谷({last_valley[1]:.2f})"
                  f"{dist_valley*100:.1f}%,可兑现冲高")
    else:
        signal = "hold"
        reason = (f"介于峰谷之间(距谷{dist_valley*100:+.1f}%/距峰{dist_peak*100:+.1f}%),观望")

    # 风控:当日已暴涨>7% 不再给买点(防追高)
    if signal == "buy" and day_chg > 0.07:
        signal = "hold"
        reason = f"日内已涨{day_chg*100:.1f}%,放弃追高,等回撤至谷"

    return {
        "signal": signal, "reason": reason,
        "cur": round(cur, 2), "open": round(open_px, 2),
        "day_chg": round(day_chg, 4),
        "valley": round(last_valley[1], 2), "peak": round(last_peak[1], 2),
        "bars": len(bars),
    }


def intraday_advice(codes, cur_prices=None, freq=5):
    """对一批代码算峰岭谷建议。cur_prices={code:实时价}可选。
    返回 [{code, name, ...signal}]。"""
    import conf
    cfg = conf.load_config()
    bars_map = fetch_minute_today(codes, freq)
    out = []
    for code in codes:
        bars = bars_map.get(code)
        if not bars:
            continue
        cur = (cur_prices or {}).get(code)
        sig = peak_valley_signal(bars, cur)
        sig["code"] = code
        out.append(sig)
    return out


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    test = ["sh600519", "sz000001", "sh601318"]
    print("=== 拉取当日分钟线 ===")
    bm = fetch_minute_today(test, 5)
    for c, bars in bm.items():
        print(f"{c}: {len(bars)} 根 5 分钟,最新 {bars[-1]}")
    print("=== 峰岭谷信号 ===")
    for c, bars in bm.items():
        print(c, json.dumps(peak_valley_signal(bars), ensure_ascii=False))
