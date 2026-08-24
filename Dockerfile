FROM python:3.11-slim

# System libraries required by kaleido's bundled Chromium (chart image export)
# and by reportlab / cairo for PDF generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 \
    libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
    libx11-6 libxcb1 libxext6 \
    fonts-liberation libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer — only re-runs when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full application
COPY . .

# Create writable runtime directories (uploads, reports, charts persist within a session)
RUN mkdir -p uploads/raw artifacts/reports artifacts/charts logs \
    && chmod -R 777 uploads artifacts logs

# HuggingFace Spaces requires port 7860; Cloud Run injects its own $PORT.
EXPOSE 7860

# Run gunicorn from the backend directory so `from analysis.*` and `import retention` resolve
WORKDIR /app/backend

# 1 worker (enough for 5-10 non-concurrent users), 2 threads. 10-min timeout:
# a full-season merge (18 files, 10.7M rows) takes ~72 s to process, and a large
# multipart upload over a domestic connection can add several minutes on top.
#
# Shell form so $PORT expands at runtime: Cloud Run injects PORT and rejects a
# container that does not listen on it, while HuggingFace Spaces leaves it unset
# and expects 7860. `exec` keeps gunicorn as PID 1 so it still receives SIGTERM.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-7860} --timeout 600 --workers 1 --threads 2 app:app"]
