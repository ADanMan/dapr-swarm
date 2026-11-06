FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OTEL_SDK_DISABLED=true ANONYMIZED_TELEMETRY=false
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 1000 --create-home swarm
COPY app ./app
USER swarm
CMD ["python", "-m", "uvicorn", "app.main:application", "--factory", "--host", "0.0.0.0", "--port", "8788", "--no-access-log"]
