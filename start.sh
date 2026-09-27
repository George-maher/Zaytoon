#!/usr/bin/env bash
set -e

echo "============================================"
echo "  Zaytoon AI - Starting Application"
echo "============================================"
echo ""

# Check backend venv
if [ ! -d "backend/.venv" ]; then
    echo "[SETUP] Creating Python virtual environment..."
    cd backend
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    cd ..
fi

# Check .env
if [ ! -f "backend/.env" ]; then
    echo "[SETUP] Creating .env from .env.example..."
    cp backend/.env.example backend/.env
    echo "[WARN] Edit backend/.env and set OPENROUTER_API_KEY (https://openrouter.ai/keys)"
    echo "[WARN] Zaytoon calls OpenRouter only, so the assistant cannot answer without a key."
fi

# Check node_modules
if [ ! -d "frontend/node_modules" ]; then
    echo "[SETUP] Installing frontend dependencies..."
    cd frontend
    npm install
    cd ..
fi

echo "[APP] Starting services..."

# Start backend
cd backend
source .venv/bin/activate
python main.py &
BACKEND_PID=$!
cd ..

# Wait for backend
echo "[APP] Waiting for backend to initialize..."
sleep 3

# Start frontend
cd frontend
npm run dev &
FRONTEND_PID=$!
cd ..

echo ""
echo "============================================"
echo "  Zaytoon AI is running!"
echo "  Web UI: http://localhost:5173"
echo "  API:    http://127.0.0.1:8000"
echo "  Health: http://127.0.0.1:8000/health"
echo "============================================"
echo ""
echo "Press Ctrl+C to stop all services"

# Trap and cleanup
trap "echo 'Shutting down...'; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo 'Done.'" EXIT

# Wait
wait
