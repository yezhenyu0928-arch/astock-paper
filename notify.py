# -*- coding: utf-8 -*-
"""通知层(SPEC 模块5)。push(title,content,level):PushPlus 主通道失败→SMTP 备用;
两者都失败→抛异常(让 Actions 变红)。本地未配置任何通道时仅记录并跳过(不崩)。
消息模板严格按 SPEC 渲染。CLI:python notify.py --alert "..."。"""
import sys
import ssl
import smtplib
import logging
import argparse
from email.mime.text import MIMEText
from email.header import Header

import requests

import conf
import util

log = logging.getLogger("notify")

PUSHPLUS_URL = "http://www.pushplus.plus/send"

STRATEGY_CN = {
    "s2_etf@v1": "ETF动量轮动", "s1_dividend@v1": "红利低波", "s3_ma_trend@v1": "双均线趋势",
    "s4_smallcap@v1": "沪深300价值精选", "s5_grid@v1": "大盘估值网格", "s6_sector@v1": "行业ETF轮动",
    "s1_dividend@v2": "红利低波·质量增强",
}


def strategy_cn(sid):
    return STRATEGY_CN.get(sid, sid)


# ---------------- 传输 ----------------
def _push_pushplus(title, content, token):
    r = requests.post(PUSHPLUS_URL, json={"token": token, "title": title,
                                          "content": content, "template": "txt"}, timeout=15)
    j = r.json()
    if r.status_code == 200 and j.get("code") == 200:
        return True
    raise RuntimeError(f"PushPlus 返回 {r.status_code}/{j.get('code')}: {j.get('msg')}")


def _push_smtp(title, content, cfg):
    auth = conf.secret("SMTP_AUTH_CODE")
    smtp = (cfg.get("user") or {}).get("smtp") or {}
    user, host, port = smtp.get("user"), smtp.get("host"), int(smtp.get("port", 465))
    if not (auth and user and host):
        raise RuntimeError("SMTP 未配置(缺 SMTP_AUTH_CODE 或 smtp.user/host)")
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = user
    msg["To"] = user
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
        s.login(user, auth)
        s.sendmail(user, [user], msg.as_string())
    return True


def push(title, content, level="op", cfg=None, smtp_fallback=True) -> bool:
    """level∈{op,alert,heartbeat}。主 PushPlus(微信)→备 SMTP(邮箱,可关闭)。
    smtp_fallback=False 时(如心跳)仅走微信,失败不回落邮箱——避免心跳刷邮箱。
    完全未配置通道时:仅打印(本地开发),返回 False。"""
    cfg = cfg or conf.load_config()
    token = conf.secret("PUSHPLUS_TOKEN")
    auth = conf.secret("SMTP_AUTH_CODE")
    full = f"{title}\n{content}"
    if not token and not auth:
        log.warning("未配置任何推送通道(PUSHPLUS_TOKEN/SMTP_AUTH_CODE),仅打印:\n%s", full)
        print(full)
        return False
    errs = []
    if token:
        try:
            _push_pushplus(title, content, token)
            log.info("PushPlus 推送成功: %s", title)
            return True
        except Exception as e:
            errs.append(f"PushPlus:{e}")
            log.warning("PushPlus 失败%s: %s",
                        "" if smtp_fallback else "(不回落邮箱)", e)
    if smtp_fallback and auth:
        try:
            _push_smtp(title, content, cfg)
            log.info("SMTP 推送成功: %s", title)
            return True
        except Exception as e:
            errs.append(f"SMTP:{e}")
    if errs:
        # 关键通道(PushPlus)失败且无可用回落 → 抛异常让 Actions 变红(仅 heartbeat 关回落时常见)
        raise RuntimeError("所有推送通道失败: " + " | ".join(errs))
    return False


# ---------------- 消息模板(严格按 SPEC) ----------------
def build_op_message(sid, date, items):
    """明日操作预告。items=[{side,code,name,qty_desc,ref_price,reason}]。"""
    cn = strategy_cn(sid)
    title = f"【明日操作 | {cn}】{date}"
    lines = [f"【明日操作 | {cn}】{date} 18:00"]
    for i, it in enumerate(items, 1):
        act = "卖出" if it["side"] == "sell" else "买入"
        lines.append(f"{_circ(i)} {act} {util.bare(it['code'])} {it['name']}  "
                     f"{it['qty_desc']}  参考价{it['ref_price']}")
        lines.append(f"   理由:{it['reason']}")
    lines.append("→ 请于明日开盘后按开盘价附近跟单,成交后在看板回填实盘价")
    return title, "\n".join(lines)


def build_fill_message(date, items):
    """今日模拟成交回报。items=[{side,code,name,shares,sim_price,fee,tax,status}]。"""
    title = f"【今日模拟成交回报】{date}"
    lines = [title]
    for i, it in enumerate(items, 1):
        act = "卖出" if it["side"] == "sell" else "买入"
        tag = "" if it["status"] == "filled" else f"[{it['status']}]"
        lines.append(f"{_circ(i)} {act} {util.bare(it['code'])} {it['name']}  "
                     f"{it['shares']}股 @ {it['sim_price']} 费{it['fee']}税{it['tax']} {tag}")
    lines.append("→ 请在看板『操作流水』回填你的实盘成交价")
    return title, "\n".join(lines)


def build_heartbeat(date, last_date, note):
    title = f"【心跳】{date} 系统正常"
    content = f"【心跳】{date} 系统正常 | 数据至{last_date} | {note}"
    return title, content


def build_alert(text):
    return "【告警🔴】", f"【告警🔴】{text}"


def _circ(n):
    circ = "①②③④⑤⑥⑦⑧⑨⑩"
    return circ[n - 1] if 1 <= n <= len(circ) else f"{n}."


# ---------------- CLI ----------------
def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--alert", help="发送告警文本")
    ap.add_argument("--heartbeat", action="store_true", help="发送测试心跳")
    ap.add_argument("--test", help="发送测试消息")
    args = ap.parse_args(argv)
    try:
        if args.alert:
            t, c = build_alert(args.alert)
            push(t, c, "alert")
        elif args.heartbeat:
            t, c = build_heartbeat(util.today_str(), util.today_str(), "测试心跳")
            push(t, c, "heartbeat", smtp_fallback=False)
        elif args.test:
            push("【测试】", args.test, "op")
        else:
            print("用法: python notify.py --alert '内容' | --heartbeat | --test '内容'")
    except Exception as e:
        log.error("推送失败: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
