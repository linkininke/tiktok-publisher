@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0patches;%~dp0_tools\runtime_site;%~dp0_internal;%~dp0"
set "TCL_LIBRARY=%~dp0_internal\_tcl_data"
set "TK_LIBRARY=%~dp0_internal\_tk_data"

powershell -NoProfile -Command "try { $c=New-Object Net.Sockets.TcpClient; $c.ReceiveTimeout=300; $c.SendTimeout=300; $c.Connect('127.0.0.1',7892); $ok=$c.Connected; $c.Close(); if($ok){exit 0}else{exit 1} } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  set "HTTP_PROXY=http://127.0.0.1:7892"
  set "HTTPS_PROXY=http://127.0.0.1:7892"
)

echo 将逐个打开账号窗口，清理 TikTok「未保存草稿」弹窗...
"%~dp0_tools\py311\python.exe" "%~dp0tools_clear_drafts.py"
echo.
pause
