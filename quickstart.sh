#!/bin/bash
# Create a virtualenv, install the dependencies and start the app.
cd "$(dirname "${BASH_SOURCE[0]}")/backend" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is not installed"
    exit 1
fi

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip setuptools wheel
if ! pip install -r requirements.txt; then
    echo "could not install the dependencies"
    exit 1
fi

mkdir -p ../uploads ../logs

# first free port from 5001 to 5010
for p in $(seq 5001 5010); do
    if ! lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then
        FLASK_PORT=$p
        break
    fi
done

if [ -z "$FLASK_PORT" ]; then
    echo "no free port between 5001 and 5010"
    exit 1
fi

export FLASK_HOST=127.0.0.1 FLASK_PORT
echo "Open http://127.0.0.1:${FLASK_PORT}"
python app.py
