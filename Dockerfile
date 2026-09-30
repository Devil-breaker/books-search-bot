# Use Python 3.11 slim image
FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source code
COPY goodreads_bot.py .
COPY src/ src/

# Run the Telegram bot
CMD ["python", "goodreads_bot.py"]
