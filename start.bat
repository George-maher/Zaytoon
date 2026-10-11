@echo off
title Zaytoon AI

echo ============================================
echo   Zaytoon AI - Starting Application
echo ============================================
echo.

:: Check if backend venv exists
if not exist "backend\.venv\" (
    echo [SETUP] Creating Python virtual environment...
    cd backend
    python -m venv .venv
    call .venv\Scripts\activate
    pip install -r requirements.txt
    cd ..
)

:: Check if .env exists
if not exist "backend\.env" (
    echo [SETUP] Creating .env from .env.example...
    copy backend\.env.example backend\.env
    echo [WARN] backend\.env defaults to AI_PROVIDER=ollama: point it at a local
    echo [WARN] Ollama (no key), or set AI_PROVIDER=openrouter + OPENROUTER_API_KEY.
    echo [WARN] Zaytoon will not fall back between the two on its own.
)

:: Check if node_modules exist
if not exist "frontend\node_modules\" (
    echo [SETUP] Installing frontend dependencies...
    cd frontend
    call npm install
    cd ..
)

:: Copy proxy config to frontend for proper WebSocket routing
echo [APP] Starting services...

:: Start backend in new window
start "Zaytoon API" cmd /c "cd /d %~dp0backend && .venv\Scripts\activate && python main.py"

:: Wait for backend to start
echo [APP] Waiting for backend to initialize...
timeout /t 3 /nobreak > nul

:: Start frontend in new window
start "Zaytoon UI" cmd /c "cd /d %~dp0frontend && npm run dev"

:: Start the desktop shell (Python/customTkinter floating orb) if requested.
:: It manages the backend itself, so give it a moment to spawn/connect first.
if /i "%1"=="--desktop" (
    if not exist "desktop\.venv\" (
        echo [SETUP] Creating desktop virtual environment...
        python -m venv desktop\.venv
    )
    start "Zaytoon Desktop" cmd /c "cd /d %~dp0desktop && .venv\Scripts\python.exe main.py"
)

echo.
echo ============================================
echo   Zaytoon AI is running!
echo   Web UI: http://localhost:5173
echo   API:    http://127.0.0.1:8000
echo   Health: http://127.0.0.1:8000/health
echo ============================================
echo.
echo Press any key to stop all services...
pause > nul

:: Kill the started processes
echo [APP] Shutting down...
taskkill /f /fi "WINDOWTITLE eq Zaytoon API" > nul 2>&1
taskkill /f /fi "WINDOWTITLE eq Zaytoon UI" > nul 2>&1
echo [APP] All services stopped.
