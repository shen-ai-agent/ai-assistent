@echo off
rem Khởi động Mimi bằng một lệnh: bấm đúp file này, hoặc chạy "start.bat" trong terminal.
rem Nạp model xong thì tự mở trình duyệt; server đã chạy sẵn thì chỉ mở trình duyệt.
rem Đóng cửa sổ (hoặc Ctrl+C) là tắt server.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m server --open %*
if errorlevel 1 pause
