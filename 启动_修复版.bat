@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0patches;%~dp0_tools\runtime_site;%~dp0_internal;%~dp0"
set "TCL_LIBRARY=%~dp0_internal\_tcl_data"
set "TK_LIBRARY=%~dp0_internal\_tk_data"

REM 仅在本机 7890 代理可用时注入，避免代理未开导致 API 全挂
powershell -NoProfile -Command "try { $c=New-Object Net.Sockets.TcpClient; $c.ReceiveTimeout=300; $c.SendTimeout=300; $c.Connect('127.0.0.1',7890); $ok=$c.Connected; $c.Close(); if($ok){exit 0}else{exit 1} } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  set "HTTP_PROXY=http://127.0.0.1:7890"
  set "HTTPS_PROXY=http://127.0.0.1:7890"
  echo [代理] 已启用 127.0.0.1:7890
) else (
  echo [代理] 未检测到 7890，按 .env / 系统代理直连
)

if not exist "%~dp0_tools\py311\python.exe" (
  echo [错误] 未找到嵌入式 Python：_tools\py311\python.exe
  pause
  exit /b 1
)

if not exist "%~dp0_tools\runtime_site\tiktok_uploader.pyc" (
  echo [错误] 运行时模块未就绪。
  pause
  exit /b 1
)

if not exist "%~dp0patches\fix_discard_draft.py" (
  echo [错误] 缺少补丁 patches\fix_discard_draft.py
  pause
  exit /b 1
)

echo 正在以「草稿弹窗修复版」启动 TikTokPublisher ...
echo 请勿再直接双击 TikTokPublisher.exe（裸 exe 无法处理「放弃此次发布」二次确认）
"%~dp0_tools\py311\python.exe" "%~dp0run_fixed.py"
if errorlevel 1 (
  echo.
  echo 启动失败，请查看 logs\publisher.log
  pause
)
