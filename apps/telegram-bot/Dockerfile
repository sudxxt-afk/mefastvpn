FROM python:3.11-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /usr/src/app

RUN addgroup -S appgroup && adduser -S appuser -G appgroup

RUN apk add --no-cache --virtual .build-deps \
        gcc musl-dev libffi-dev openssl-dev && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt && \
    apk del --no-cache .build-deps && \
    rm -rf /root/.cache

FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /usr/src/app

RUN addgroup -S appgroup && adduser -S appuser -G appgroup

COPY --from=builder /install /usr/local

COPY . .

RUN chown -R appuser:appgroup /usr/src/app

USER appuser

EXPOSE 8080
CMD ["python", "-m", "bot"]
