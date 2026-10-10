FROM python:3.11-slim

# libraries for kaleido's Chromium (chart export) and for PDF generation
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

# unbuffered, so log lines show up as they happen
ENV PYTHONUNBUFFERED=1

# dependencies first so this layer is cached
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# writable folders for uploads and generated files
RUN mkdir -p uploads/raw artifacts/reports artifacts/charts logs \
    && chmod -R 777 uploads artifacts logs

# 7860 by default; Cloud Run sets $PORT
EXPOSE 7860

# run from backend/ so the imports resolve
WORKDIR /app/backend

# one worker, two threads; the 10 minute timeout covers large merges and uploads.
# Shell form so $PORT expands, and exec so gunicorn gets SIGTERM.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-7860} --timeout 600 --workers 1 --threads 2 app:app"]
