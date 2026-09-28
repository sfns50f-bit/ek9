FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY quizapp ./quizapp
RUN useradd --uid 10001 --create-home quiz && mkdir -p /data && chown quiz:quiz /data
USER quiz

EXPOSE 8000
CMD ["uvicorn", "quizapp.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
