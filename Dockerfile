FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt

RUN groupadd --system --gid 10001 toilet2 \
    && useradd --system --uid 10001 --gid toilet2 \
        --home-dir /nonexistent --shell /usr/sbin/nologin toilet2 \
    && install -d -o toilet2 -g toilet2 -m 0750 /var/lib/lmio-toilet

COPY --chown=root:root . /app/toilet2

USER 10001:10001

EXPOSE 8000
VOLUME ["/var/lib/lmio-toilet"]

CMD ["python", "-m", "uvicorn", "toilet2.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
