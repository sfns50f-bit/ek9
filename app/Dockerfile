# arm64（Raspberry Pi）でも x86 でもビルドできる公式イメージを使う
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY quizapp ./quizapp
# root 以外で動かす。データのフォルダ（/data）はこの uid（10001）が書き込めるようにしておく
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin quiz
USER 10001

EXPOSE 8000
CMD ["uvicorn", "quizapp.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
