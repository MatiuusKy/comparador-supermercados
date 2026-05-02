FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt && playwright install chromium --with-deps

COPY . .

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port $PORT"]
