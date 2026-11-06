# gdal-tools MCP 适配层

算法在 `gdal_tools.core`，本目录只提供 MCP 入口。请优先用仓库根目录的 `docker/gdal.Dockerfile` 构建。

## Local

```bash
# from mineral-tools-mcp/
set PYTHONPATH=..;%CD%\..
pip install -r gdal-tools-mcp/requirements.txt
# need gdal-bin on PATH (or use Docker)
python gdal-tools-mcp/server.py
```

## Docker

镜像默认走 `docker.1ms.run` 加速源：

```bash
docker pull docker.1ms.run/postgres:17-trixie
```

```bash
# 仅数据库
docker compose -f mcp/docker-compose.yml --profile db up -d

# GDAL MCP（会带上同 profile 下的 postgres）
docker compose -f mcp/docker-compose.yml --profile gdal up --build
```

