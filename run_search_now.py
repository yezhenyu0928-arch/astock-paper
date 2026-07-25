# -*- coding: utf-8 -*-
"""一键跑重构后的多臂搜参(search_arms.py)——跳过会失败的"建库"步骤, 直接吃现有 DB。

背景(2026-07-24): 用户要求助手自行跑通、自行修问题。命令执行后端反复掉线,
且海外/本机"建库"(update_all+backfill 抓 akshare/baostock)常失败/卡死。但本地
db/market.sqlite 已真实存在(daily_bar 252万行, 主板3344只, sh510300 全历史在 daily_bar),
足以直接回测。故本脚本:**只杀残留进程/清锁 → 校验DB → 跑 search_arms → 推报告**, 不建库。

用法:
    python run_search_now.py            # 全臂 × 6 策略 多臂搜参 + 推报告
    python run_search_now.py --quick    # 只跑前3臂(快, 先出初步数字)
    python run_search_now.py --no-push  # 跑完不推 GitHub(仅本地留报告)

退出码: 0=成功推报告; 2=DB 不满足条件(跳过); 其他=异常。
"""
import os
import sys
import time
import json
import logging
import subprocess
import sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
LOG = os.path.join(ROOT, "run_search_now.log")
LOCK = os.path.join(ROOT, "run_local.lock")  # 复用 run_local.bat 的锁文件

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG, encoding="utf-8"),
                               logging.StreamHandler()])
log = logging.getLogger("run_search_now")

DEFAULT_SIDS = [
    "s1_dividend@v3", "s15_core_allocation@v3", "s8_checklist@v3",
    "s4_smallcap@v3", "s13_growth_quality_rotation@v3",
    "s14_value_reversal_rotation@v3",
]


def _py():
    """优先用本机系统 Python(项目依赖装在它下面, 且 C 扩展稳定不段错误);
    找不到才退回启动本脚本的解释器。避免 managed 隔离 python 跑回测时
    出现 0xC0000005 段错误(退出码 3221225477)。"""
    sys_py = r"C:\Users\zhenyu\AppData\Local\Programs\Python\Python313\python.exe"
    if os.path.exists(sys_py):
        return sys_py
    return sys.executable


def self_heal():
    """清理其它 python 进程(避免抢 db/market.sqlite 写锁), 再清陈旧锁文件。
    注意: 必须排除本进程自身 PID, 否则 taskkill 会把脚本自己一起杀掉(自杀 bug)。"""
    mypid = os.getpid()
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
            capture_output=True, text=True, timeout=30).stdout or ""
        killed = []
        for line in out.splitlines()[1:]:  # 跳过 CSV 表头
            parts = line.strip().strip('"').split('","')
            # CSV 列: "Image Name","PID","Session Name","Session#","Mem Usage"
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            if pid != mypid and pid > 0:
                r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=10)
                if r.returncode == 0:
                    killed.append(pid)
        if killed:
            log.info("已清理 %d 个残留 python 进程: %s", len(killed), killed)
        else:
            log.info("无其它残留 python 进程需清理(本进程PID=%s 已排除)", mypid)
    except Exception as e:
        log.warning("清理残留进程失败(可忽略): %s", e)
    time.sleep(3)
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
            log.info("已清除陈旧锁 %s", LOCK)
    except Exception as e:
        log.warning("清除锁失败: %s", e)


def acquire_lock():
    """写 PID 锁; 若锁被存活进程持有则退出(防止并发抢库)。"""
    if os.path.exists(LOCK):
        try:
            old = int(open(LOCK).read().strip() or "0")
            # 进程仍在? (Windows: tasklist 查 PID)
            out = subprocess.run(["tasklist", "/FI", f"PID eq {old}"],
                                 capture_output=True, text=True, timeout=20).stdout
            if str(old) in out:
                log.error("另一实例(PID %s)仍在运行, 退出避免抢库", old)
                sys.exit(3)
        except Exception:
            pass
        try:
            os.remove(LOCK)
        except Exception:
            pass
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    log.info("获得锁 PID=%s", os.getpid())


def db_ready():
    """校验现有 DB 是否满足直接回测条件。返回 (ok, 详情str)。"""
    db = os.path.join(ROOT, "db", "market.sqlite")
    if not os.path.exists(db):
        return False, "无 db/market.sqlite"
    try:
        conn = sqlite3.connect(db)
        n = conn.execute("SELECT count(*) FROM daily_bar").fetchone()[0]
        mb = conn.execute(
            "SELECT count(*) FROM index_members WHERE index_code='mainboard'").fetchone()[0]
        hs = conn.execute(
            "SELECT count(*) FROM daily_bar WHERE code='sh510300'").fetchone()[0]
        conn.close()
        detail = f"daily_bar={n} mainboard={mb} sh510300={hs}"
        ok = (n >= 50000) and (mb >= 800) and (hs >= 200)
        return ok, detail + (" ✅" if ok else " ❌(不足)")
    except Exception as e:
        return False, f"DB 读取异常: {e}"


def ensure_benchmark():
    """若 sh510300 缺失, 尝试用腾讯源补(海外/本机腾讯可达); 失败则记日志, 闸门优雅降级。"""
    db = os.path.join(ROOT, "db", "market.sqlite")
    try:
        conn = sqlite3.connect(db)
        hs = conn.execute("SELECT count(*) FROM daily_bar WHERE code='sh510300'").fetchone()[0]
        conn.close()
        if hs >= 200:
            return
        log.warning("sh510300 仅 %s 行, 尝试补抓(腾讯源)...", hs)
        try:
            import data
            import conf
            data.update_index_daily(["sh510300"], conn=sqlite3.connect(db))
            log.info("sh510300 补抓完成")
        except Exception as e:
            log.warning("sh510300 补抓失败(闸门将降级为仅宏观分): %s", e)
    except Exception as e:
        log.warning("ensure_benchmark 异常: %s", e)


def run(cmd_args):
    """在 ROOT 下用本机 python 跑子进程, 实时转发输出到日志。返回 CompletedProcess。"""
    py = _py()
    log.info(">>> %s %s", py, " ".join(cmd_args))
    return subprocess.run([py, *cmd_args], cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace")


def collect_reports():
    """收集 search_arms 产出的报告文件(存在才推)。"""
    files = []
    arm = os.path.join(ROOT, "reports", "_arm_search.md")
    if os.path.exists(arm):
        files.append(arm)
    for sid in DEFAULT_SIDS:
        p = os.path.join(ROOT, "reports", sid.replace("@", "_at_") + ".md")
        if os.path.exists(p):
            files.append(p)
    return files


def main():
    args = sys.argv[1:]
    quick = "--quick" in args
    no_push = "--no-push" in args

    log.info("===== run_search_now 启动 (quick=%s no_push=%s) =====", quick, no_push)
    self_heal()
    acquire_lock()

    ok, detail = db_ready()
    log.info("DB 校验: %s", detail)
    if not ok:
        log.error("DB 不满足条件, 中止(本脚本不建库; 如需建库请用 run_local.bat 本机双击)。")
        try:
            os.remove(LOCK)
        except Exception:
            pass
        sys.exit(2)

    ensure_benchmark()

    # 跑多臂搜参(核心: 验证重构后的大盘趋势择时闸能否把回撤压到<10%、年化拉到>10%)
    sa_args = ["search_arms.py"]
    if quick:
        sa_args.append("--quick")
    if no_push:
        sa_args.append("--no-final")  # 不重生成五关(更快), 但仍出榜
    r = run(sa_args)
    log.info("search_arms 退出码=%s", r.returncode)
    if r.stdout:
        log.info("search_arms 输出尾段:\n%s", r.stdout[-3000:])

    # 推报告
    if not no_push:
        reports = collect_reports()
        if reports:
            log.info("准备推送 %d 个报告: %s", len(reports), [os.path.basename(x) for x in reports])
            pr = run(["_api_push.py",
                      "multi-arm search (local DB, capital=100k, trend-gate refactor)",
                      "config.yaml", *reports])
            log.info("_api_push 退出码=%s", pr.returncode)
            if pr.stdout:
                log.info("_api_push 输出:\n%s", pr.stdout[-1500:])
        else:
            log.warning("未找到任何报告文件, 跳过推送")

    # 写完成标记
    try:
        with open(os.path.join(ROOT, "search_done.flag"), "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass

    try:
        os.remove(LOCK)
    except Exception:
        pass
    log.info("===== run_search_now 结束 =====")


if __name__ == "__main__":
    main()
