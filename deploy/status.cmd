@echo off
rem Windows helper: show bot status on the server. Set HOST and KEY below first.
set HOST=ubuntu@YOUR.SERVER.IP
set KEY=%USERPROFILE%\.ssh\your_key
chcp 65001 >nul
ssh -i "%KEY%" %HOST% "systemctl is-active qbot; ~/qbot-venv/bin/python -m livebot.bot status | head -4"
pause
