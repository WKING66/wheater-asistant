# 一个镜像装下前后端：前端构建成静态文件，后端用 FastAPI 同时伺服它。
# 这样只占一台机器、一个域名，也不用再配跨域代理。

# ---------- 第一阶段：构建前端 ----------
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------- 第二阶段：跑后端 ----------
FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=web /web/dist /app/web

# 告诉后端去哪找前端产物，并监听所有网卡（容器里必须 0.0.0.0）
ENV WEB_DIST=/app/web \
    HOST=0.0.0.0 \
    PORT=8000
EXPOSE 8000
CMD ["python", "main.py"]
