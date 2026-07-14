FROM python:3.12-slim

WORKDIR /app

# 先装依赖以利用构建缓存；额外装 pymysql，方便按需切换到 MySQL
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pymysql

# 拷贝应用代码（数据文件由 .dockerignore 排除，不打进镜像）
COPY . .

# 数据（SQLite 库 + 会话文件）统一落到挂载卷，容器重建/升级都不丢
ENV EVAL_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8888

CMD ["python", "main.py"]
