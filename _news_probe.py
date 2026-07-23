# -*- coding: utf-8 -*-
"""海外 Runner(GitHub Actions ubuntu-latest)新闻源可达性探针。
目的:纯云端方案下,必须以"海外 Runner 实测"为准决定用哪个新闻源(本地中国测试结果对云端无效)。
逐个测候选源,打印 PASS/FAIL + 条数 + 首条标题 + 耗时,便于据实选源。任何异常都被捕获,不中断其他源测试。
运行:在 workflow(runs-on: ubuntu-latest)里 `python _news_probe.py`。
"""
import time
import traceback


def _timed(name, fn):
    t0 = time.time()
    try:
        n, sample = fn()
        dt = time.time() - t0
        status = "PASS" if n > 0 else "EMPTY"
        print(f"[{status}] {name:28s} count={n:<4d} {dt:5.1f}s  sample={sample[:60]!r}")
        return n
    except Exception as e:
        dt = time.time() - t0
        print(f"[FAIL] {name:28s} {dt:5.1f}s  err={type(e).__name__}: {str(e)[:120]}")
        return 0


# ---- 候选源 ----
def p_sina_feed():
    import requests
    r = requests.get("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&num=30",
                     timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    data = r.json()
    rows = (data.get("result", {}) or {}).get("data", []) or []
    return len(rows), (rows[0].get("title", "") if rows else "")


def p_ak_em_global():
    import akshare as ak
    df = ak.stock_info_global_em()
    return (0 if df is None else len(df)), (str(df.iloc[0].get("标题", "")) if df is not None and len(df) else "")


def p_ak_sina_global():
    import akshare as ak
    df = ak.stock_info_global_sina()
    return (0 if df is None else len(df)), (str(df.iloc[0].to_dict()) if df is not None and len(df) else "")


def p_ak_cls_global():
    import akshare as ak
    df = ak.stock_info_global_cls()
    return (0 if df is None else len(df)), (str(df.iloc[0].get("标题", "")) if df is not None and len(df) else "")


def p_ak_cjzc_em():
    import akshare as ak
    df = ak.stock_info_cjzc_em()
    return (0 if df is None else len(df)), (str(df.iloc[0].to_dict())[:60] if df is not None and len(df) else "")


def p_ak_cctv():
    import akshare as ak
    df = ak.news_cctv()
    return (0 if df is None else len(df)), (str(df.iloc[0].get("title", "")) if df is not None and len(df) else "")


def p_em_np_raw():
    """东财资讯 np 接口(直连,不经 akshare)。"""
    import requests
    url = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
           "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=30&req_trace=1")
    r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://kuaixun.eastmoney.com/"})
    data = r.json()
    rows = ((data.get("data", {}) or {}).get("fastNewsList", []) or [])
    return len(rows), (rows[0].get("title", "") if rows else "")


def p_tencent_roll():
    """腾讯财经滚动(gtimg CDN 已知云端可达)。"""
    import requests
    url = "https://pacaio.match.qq.com/irs/rcd?cid=137&token=d0f13d594edfc180f5bf6b5f8673fa2d&num=30"
    r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    data = r.json()
    rows = data.get("data", []) or []
    return len(rows), (rows[0].get("title", "") if rows else "")


def p_sina_zhibo():
    """新浪财经7x24直播(zhibo lifeid接口)。"""
    import requests
    url = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=30&zhibo_id=152&tag_id=0"
           "&dire=f&dpc=1&pagesize=30&id=0&type=0")
    r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    data = r.json()
    rows = (((data.get("result", {}) or {}).get("data", {}) or {}).get("feed", {}) or {}).get("list", []) or []
    return len(rows), (rows[0].get("rich_text", "")[:50] if rows else "")


if __name__ == "__main__":
    print("=" * 90)
    print("海外 Runner 新闻源可达性探针  (ubuntu-latest)")
    print("=" * 90)
    try:
        import akshare as ak
        print("akshare version:", getattr(ak, "__version__", "?"))
    except Exception as e:
        print("akshare import FAIL:", e)
    print("-" * 90)
    _timed("requests: sina_feed", p_sina_feed)
    _timed("requests: em_np_raw", p_em_np_raw)
    _timed("requests: tencent_roll", p_tencent_roll)
    _timed("requests: sina_zhibo", p_sina_zhibo)
    _timed("akshare: em_global", p_ak_em_global)
    _timed("akshare: sina_global", p_ak_sina_global)
    _timed("akshare: cls_global", p_ak_cls_global)
    _timed("akshare: cjzc_em", p_ak_cjzc_em)
    _timed("akshare: news_cctv", p_ak_cctv)
    print("=" * 90)
    print("DONE")
