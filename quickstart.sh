#!/bin/bash

# Quickstart Installation Script for Noise Analysis Platform
# Run this script to quickly set up and run the application

# Ensure the script works when invoked from any directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

echo "🚀 Noise Analysis Platform - Quick Start"
echo "========================================"
echo ""

# Check Python installation
echo "✓ Checking Python installation..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.8 or later."
    exit 1
fi
PYTHON_VERSION=$(python3 --version)
echo "✓ Found: $PYTHON_VERSION"
echo ""

# Create virtual environment
echo "✓ Creating virtual environment..."
cd backend
python3 -m venv venv
echo "✓ Virtual environment created"
echo ""

# Activate virtual environment
echo "✓ Activating virtual environment..."
source venv/bin/activate
echo "✓ Virtual environment activated"
echo ""

# Install dependencies
echo "✓ Installing dependencies from requirements.txt..."
pip install --upgrade pip setuptools wheel
if ! pip install -r requirements.txt; then
    echo "❌ Failed to install dependencies"
    deactivate
    exit 1
fi
echo "✓ All dependencies installed successfully"
echo ""

# Create uploads directory
echo "✓ Creating uploads directory..."
mkdir -p ../uploads
mkdir -p ../logs
echo "✓ Directories created"
echo ""

# Start the application
echo "🎉 Setup complete! Starting the application..."
echo ""
echo "📊 Noise Analysis Platform is running at:"
echo "🌐 "
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

# Prefer IPv4 localhost for maximum browser compatibility.
export FLASK_HOST="127.0.0.1"

# Pick a free port starting at 5001 (common port conflicts can make the site look like a blank page).
PORT_START=5001
PORT_END=5010
FLASK_PORT=""
for p in $(seq $PORT_START $PORT_END); do
    if ! lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then
        FLASK_PORT=$p
        break
    fi
done

if [ -z "$FLASK_PORT" ]; then
    echo "❌ Could not find a free port in range ${PORT_START}-${PORT_END}."
    echo "   Please stop the process using one of these ports, or run with: FLASK_PORT=5050 ./quickstart.sh"
    deactivate
    exit 1
fi

export FLASK_PORT

echo "🌐 Open: http://127.0.0.1:${FLASK_PORT}"
echo "🌐 (Also works): http://localhost:${FLASK_PORT}"
echo ""

python app.py
