@echo off
cd C:\Users\tquar\polybot

start "PolyBot Scorer" cmd /k "venv\Scripts\activate && python main.py"
start "PolyBot Whale" cmd /k "venv\Scripts\activate && python main_whale.py"

timeout /t 3 /nobreak > nul

netstat -ano | findstr ":8080" | findstr "LISTENING" > nul
if %errorlevel% == 0 (
    echo Servidor ja a correr em 8080
) else (
    start /b python -m http.server 8080
    timeout /t 1 /nobreak > nul
)

start http://localhost:8080/dashboard.html