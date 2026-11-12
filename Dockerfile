FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ELEV_DATA_DIR=/data

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app.py ./
COPY elevation ./elevation

# 数据卷：SQLite 索引、WAL、64×64 块文件、临时文件全部位于 /data
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 2 worker × 2 线程：写事务在 SQLite 层 BEGIN IMMEDIATE 串行化，
# 读走 WAL 快照；容器内单写多读即可演示并发上传的排队与隔离。
# --factory 让 gunicorn 调用 create_app() 工厂，导入期不触碰数据目录。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--timeout", "60", "--factory", "app:create_app"]
