@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM lock: prevent two instances from corrupting the shared DB
set LOCK=run_local.lock
if exist "%LOCK%" (
  echo.
  echo  ERROR: another run_local instance is already running (run_local.lock exists).
  echo  Close that window first, or delete run_local.lock if it is stale, then re-run.
  echo.
  pause
  exit /b 1
)
echo locked > "%LOCK%"

set LOG=run_local.log
echo [%date% %time%] === run_local start === > %LOG%

REM 0) find python (try python, py, python3 in order)
set PY=python
where python >nul 2>&1 || (where py >nul 2>&1 && set PY=py)
where %PY% >nul 2>&1 || (where python3 >nul 2>&1 && set PY=python3)
echo Using python: %PY%
echo [%date% %time%] Using python: %PY% >> %LOG%
where %PY% >nul 2>&1 || (
  echo ERROR: no python found on PATH. Install Python 3.10+ and re-run.
  echo [%date% %time%] ERROR: no python >> %LOG%
  pause
  exit /b 1
)

echo ===================================================
echo  A-share backtest - LOCAL build + run + push
echo  (GitHub overseas runner CANNOT fetch CN data,
echo   so we build the DB on YOUR machine instead)
echo ===================================================

REM 1) install deps (best-effort; skip if already present)
echo [1/5] installing python deps (may take a few min)...
%PY% -m pip install -r requirements.txt >> %LOG% 2>&1 || echo "  (deps already installed or pip skipped)"

REM 1.5) self-heal: kill any stray python (e.g. a leftover automation
REM        backfill.py) that is still writing the SAME db/market.sqlite.
REM        Two writers at once => "sqlite3.OperationalError: database is locked".
echo [1.5/5] killing any stray python processes to avoid DB lock...
taskkill /F /IM python.exe >nul 2>&1
timeout /t 3 >nul 2>&1

REM 2) build the market database locally (your PC can reach akshare/eastmoney)
echo [2/5] building market DB (universe + daily bars + fundamentals)...
%PY% -c "import data,conf; data.update_all(conf.load_config(), conf.load_registry(), with_members=True)" >> %LOG% 2>&1 || echo "  update_all issue, continuing to backfill"
%PY% backfill.py >> %LOG% 2>&1 || echo "  backfill issue"

REM 3) GUARD: abort with a clear message if the mainboard universe is empty
echo [3/5] verifying mainboard universe size...
%PY% -c "import sqlite3,os,sys; n=0; db='db/market.sqlite';
if os.path.exists(db):
    c=sqlite3.connect(db)
    try:
        n=c.execute(\"SELECT COUNT(*) FROM index_members WHERE index_code='mainboard'\").fetchone()[0]
    except Exception:
        try:
            n=c.execute('SELECT COUNT(*) FROM daily_bar').fetchone()[0]
        except Exception:
            n=0
    c.close()
print('MAINBOARD_COUNT', n);
sys.exit(0 if n>=800 else 1)" >> %LOG% 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: mainboard universe is EMPTY - data fetch failed on this machine.
    echo  Check your internet connection / akshare access, then re-run this script.
    echo  (see run_local.log for details)
    echo.
    echo [%date% %time%] ERROR: mainboard universe empty >> %LOG%
    del /f /q "%LOCK%" >nul 2>&1
    pause
    exit /b 1
)
echo  mainboard universe OK - proceeding to backtest.

REM 4) multi-arm parameter search (sweeps macro trend-gate thresholds per
REM    strategy, picks the best arm for target annual>10% & maxDD<10%, then
REM    regenerates the full 5-pass reports + monte-carlo under the winning arm).
echo [4/5] multi-arm parameter search + backtests + validation (this takes a while)...
%PY% search_arms.py >> %LOG% 2>&1 || (
    echo "  search_arms failed, falling back to single-arm run"
    for %%s in (s1_dividend@v3 s4_smallcap@v3 s8_checklist@v3 s13_growth_quality_rotation@v3 s14_value_reversal_rotation@v3 s15_core_allocation@v3) do (
        echo   -- %%s
        %PY% backtest.py report %%s >> %LOG% 2>&1 || echo "  report %%s failed"
        %PY% validate.py %%s >> %LOG% 2>&1 || echo "  validate %%s failed"
    )
)

REM 5) push the generated reports + config (capital=100k) to GitHub
echo [5/5] pushing reports to GitHub...
%PY% _api_push.py "backtest reports (local build, capital=100k, multi-arm search) %date%" ^
  config.yaml ^
  reports/_arm_search.md ^
  reports/s1_dividend_at_v3.md ^
  reports/s4_smallcap_at_v3.md ^
  reports/s8_checklist_at_v3.md ^
  reports/s13_growth_quality_rotation_at_v3.md ^
  reports/s14_value_reversal_rotation_at_v3.md ^
  reports/s15_core_allocation_at_v3.md >> %LOG% 2>&1

echo.
del /f /q "%LOCK%" >nul 2>&1
echo  DONE. Open the commit URL above to view the reports.
echo  Look for: annualized return (target ^> 10%) and max drawdown (target ^< 10%).
echo  (full log in run_local.log)
echo.
echo [%date% %time%] === run_local done === >> %LOG%
pause
