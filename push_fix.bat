@echo off
cd /d "%~dp0"
"C:\Users\zhenyu\.workbuddy\binaries\python\versions\3.13.12\python.exe" _api_push.py "fix: build_universe multi-source fallback + macro baseline 0.80 + atv3 params + backtest.yml hardening" build_universe.py macro.py registry.yaml .github/workflows/backtest.yml
echo.
echo If you see "OK pushed main: ..." above, it succeeded.
echo GitHub will now auto-run the backtest with the fixed code.
echo.
pause
