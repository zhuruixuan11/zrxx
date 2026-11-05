FROM python:3.12-slim

# 交易时段判断依赖本地时间，容器默认 UTC 会导致盯盘时间错位
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 使用锁定版本，保证任何人构建出同一套依赖（requirements.txt 为宽松版本）
COPY requirements.txt requirements.lock ./
RUN pip install -r requirements.lock

COPY app ./app

EXPOSE 8001

# 注意：进程内缓存、baostock 会话、预警轮询都是单进程状态，必须保持 workers=1
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
