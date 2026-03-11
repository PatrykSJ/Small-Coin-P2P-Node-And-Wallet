FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PORT=5000
ENV HTTP_PORT=7000
EXPOSE 5000 7000

ENTRYPOINT ["python", "-m", "app.main"]
