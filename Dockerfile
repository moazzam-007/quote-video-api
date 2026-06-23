FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port (Render sets PORT env variable, defaulting to 10000 here)
EXPOSE 10000

# Run gunicorn with 1 worker to fit within 512MB RAM free tier limit
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:10000", "--timeout", "300", "app:app"]
