# Polymarket Trading Bot - Docker Image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./

# Create logs directory
RUN mkdir -p /app/logs

# Entry point (choose between main.py or main_fast.py)
# main.py = Original synchronous version
# main_fast.py = Optimized async version (recommended)
ENV BOT_ENTRYPOINT=main.py

# Default command
CMD ["sh", "-c", "python $BOT_ENTRYPOINT"]

