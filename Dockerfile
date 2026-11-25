FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/bot.db

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p /data

RUN python selfcheck.py --build

CMD ["sh", "-c", "python selfcheck.py && exec python bot.py"]
