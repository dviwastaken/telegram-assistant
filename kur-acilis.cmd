@echo off
rem Cift tikla: acilis gorevini kurar. Betik calistirma izni kapali oldugu icin yalnizca bu calistirmada Bypass kullanilir.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0kur-acilis.ps1"
echo.
pause
