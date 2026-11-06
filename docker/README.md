# 镜像

构建共享镜像 `gdal-tools:1.0`（含全部工具的 CLI 与 MCP）：

```bash
docker compose -f deploy/docker-compose.yml build
```

默认入口：`python -m gdal_tools serve-mcp --transport sse`  
覆盖为单次计算：`docker run ... gdal-tools:1.0 run gdal.dem_slope --args '{...}'`
