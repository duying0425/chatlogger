FROM python:3.12-slim

# 设置环境变量：非缓冲输出、时区
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 安装基础时区组件
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -fs /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 创建默认持久化数据与缓存目录
RUN mkdir -p /app/data /app/cache

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn_config.py", "app:app"]

