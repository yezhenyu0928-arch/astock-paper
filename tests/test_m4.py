# -*- coding: utf-8 -*-
"""M4 通知验收(SPEC 模块5)。模板格式 + 断主通道走备用邮件。免真实网络(monkeypatch 传输)。
可直接运行:python tests/test_m4.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import notify


def test_op_template():
    items = [{"side": "buy", "code": "sh510500", "name": "中证500ETF",
              "qty_desc": "约98%仓位≈7900股", "ref_price": 6.20, "reason": "动量轮动:最强"}]
    title, content = notify.build_op_message("s2_etf@v1", "2026-07-03", items)
    assert "【明日操作 | ETF动量轮动】2026-07-03 18:00" in content
    assert "① 买入 510500 中证500ETF" in content
    assert "参考价6.2" in content
    assert "理由:动量轮动:最强" in content
    assert "请于明日开盘后按开盘价附近跟单" in content
    print("[PASS] test_op_template")


def test_fill_and_heartbeat_and_alert():
    fi = [{"side": "sell", "code": "sh513100", "name": "纳指ETF", "shares": 300,
           "sim_price": 2.10, "fee": 5.0, "tax": 3.15, "status": "filled"}]
    t, c = notify.build_fill_message("2026-07-03", fi)
    assert "今日模拟成交回报" in c and "卖出 513100 纳指ETF" in c and "300股 @ 2.1" in c
    t, c = notify.build_heartbeat("2026-07-03", "2026-07-03", "今日无操作")
    assert c.startswith("【心跳】2026-07-03 系统正常 | 数据至2026-07-03 | 今日无操作")
    t, c = notify.build_alert("数据源故障")
    assert c == "【告警🔴】数据源故障"
    print("[PASS] test_fill_and_heartbeat_and_alert")


def test_fallback_to_smtp():
    """主通道 PushPlus 失败 → 自动走 SMTP。"""
    os.environ["PUSHPLUS_TOKEN"] = "dummy"
    os.environ["SMTP_AUTH_CODE"] = "dummy"
    calls = {"pp": 0, "smtp": 0}

    def fail_pp(title, content, token):
        calls["pp"] += 1
        raise RuntimeError("模拟主通道断开")

    def ok_smtp(title, content, cfg):
        calls["smtp"] += 1
        return True

    notify._push_pushplus = fail_pp
    notify._push_smtp = ok_smtp
    cfg = {"user": {"smtp": {"user": "a@b.com", "host": "smtp.b.com", "port": 465}}}
    ok = notify.push("t", "c", "alert", cfg=cfg)
    assert ok and calls["pp"] == 1 and calls["smtp"] == 1, calls
    print("[PASS] test_fallback_to_smtp (主断→备用邮件)")


def test_both_fail_raises():
    """主备都失败 → 抛异常(Actions 变红)。"""
    os.environ["PUSHPLUS_TOKEN"] = "dummy"
    os.environ["SMTP_AUTH_CODE"] = "dummy"

    def fail(*a, **k):
        raise RuntimeError("断")

    notify._push_pushplus = fail
    notify._push_smtp = fail
    raised = False
    try:
        notify.push("t", "c", "alert", cfg={"user": {"smtp": {}}})
    except RuntimeError:
        raised = True
    assert raised
    print("[PASS] test_both_fail_raises")


def test_no_channel_no_crash():
    """本地未配置任何通道 → 不崩,返回 False。"""
    os.environ.pop("PUSHPLUS_TOKEN", None)
    os.environ.pop("SMTP_AUTH_CODE", None)
    ok = notify.push("t", "c", "op", cfg={"user": {}})
    assert ok is False
    print("[PASS] test_no_channel_no_crash")


def _run_all():
    fns = [test_op_template, test_fill_and_heartbeat_and_alert, test_fallback_to_smtp,
           test_both_fail_raises, test_no_channel_no_crash]
    ok = 0
    for fn in fns:
        try:
            fn(); ok += 1
        except Exception as e:
            import traceback
            print(f"[FAIL] {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\nM4 测试: {ok}/{len(fns)} 通过")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
