FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY sandbox_service ./sandbox_service
RUN useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin sandbox
USER sandbox

EXPOSE 8080
CMD ["python", "-m", "sandbox_service.server"]
