# ============================================================
# 构建阶段（利用缓存：先装依赖再拷代码）
# ============================================================
FROM python:3.12-slim AS base

WORKDIR /app

# 安装系统依赖 + Python 依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pymysql \
 && rm requirements.txt

# 拷贝应用代码
COPY . .

# 以非 root 用户运行，提高安全性
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

# 数据目录（挂载卷）
ENV EVAL_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8888

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8888/api/admin/check')" || exit 1

CMD ["python", "main.py"]
