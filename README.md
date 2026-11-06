# gdal-tools

一套可部署的 GDAL/OGR 地理处理工具包。一次封装，多平台用同一镜像；Agent 走 MCP，作业系统走 CLI。工具只认工作区内的文件路径，不绑定具体 DEM 产品或平台存储。

## 封装约定

- **一工具一契约**：`gdal_tools/contracts/tools/*.yaml`
- **多工具一个镜像**：共享 GDAL/PROJ/GEOS/Python
- **同一实现、两种入口**：`gdal-tools run`（作业）与 `gdal-tools serve-mcp`（Agent）
- **工作区**：`GDAL_TOOLS_WORKSPACES`（镜像内默认 `/data`）。已有部署可继续用 `MCP_WORKSPACES`，二者只设一个。

## 数据读写

**读取输入、写入结果都只允许在挂载盘上。** 容器内工作区是 `/data`，对应宿主机 `MCP_DATA`：

| 容器内路径 | 宿主机路径 | 用途 |
|------------|------------|------|
| `/data/...` | `$MCP_DATA/...` | 输入（DEM、矢量等）和输出都必须在此根下 |
| `/data/outputs/...` | `$MCP_DATA/outputs/...` | 仅文档示例子目录，**不是**引擎默认落点 |

调用时必须显式给出 `data_path` / `output_path`（均以容器内 `/data/...` 表示）。不传 `output_path` 会报错，工具不会自动写到 `outputs/`。路径落到 `/data` 以外会被拒绝。平台可自行约定子目录，例如 `/data/jobs/<id>/slope.tif`。

## 镜像构建与启动

顺序必须是：**先指定宿主机数据目录，再创建容器**。Volume 在 `up` 时写入，事后 `export` 不会改已有容器。

```bash
cd gdal-tools

# 1) 宿主机上已有数据的目录（示例，按生产实际路径改）
export MCP_DATA=/data2/gis

# 2) 再构建并启动（把 $MCP_DATA 挂到容器内 /data）
docker compose -f mcp/docker-compose.yml -f mcp/docker-compose.agent.yml \
  --profile gdal up --build -d gdal-tools
```

未设置 `MCP_DATA` 时，compose 默认挂项目旁的 `../data`，**不是**生产业务盘。  
若容器已经用错路径建过，改 `MCP_DATA` 后必须加 `--force-recreate` 再 `up` 一次。

- MCP SSE：`http://<主机>:7861/sse`
- 容器内执行单任务（路径都是容器内 `/data/...`，读写都在挂载盘上）：

```bash
docker exec <容器名> python -m gdal_tools list
docker exec <容器名> python -m gdal_tools describe gdal.dem_slope
docker exec <容器名> python -m gdal_tools run gdal.dem_slope --args '{
  "data_path": "/data/dem/gdem/example.tif",
  "output_path": "/data/outputs/slope.tif",
  "scale": 111120
}'
```

上例的 `/data/outputs/slope.tif` 只是调用方指定的路径，对应宿主机 `$MCP_DATA/outputs/slope.tif`。业务数据（GDEM、SRTM 等）由平台挂载到 `/data/...`，工具不关心产品名。

## 平台如何接入

1. 拉取/构建镜像 `gdal-tools:1.0`
2. 把本平台数据落到或挂载到工作区 `/data`（输入和输出都走这块盘）
3. 按需选择入口：
   - Agent：连接 MCP SSE，工具名见 `python -m gdal_tools list`
   - 作业调度：`python -m gdal_tools run <tool-id> --args '{...}'`，stdout 为 JSON 信封，退出码对应错误码
4. 能力发现（在**已启动的容器内**执行，宿主机通常没有 `gdal_tools`）：
   `docker exec mcp-gdal-tools-1 python -m gdal_tools list`
   `docker exec mcp-gdal-tools-1 python -m gdal_tools describe --all`
   Agent 也可连 MCP SSE 后列工具，资源为 `gdal-tools://contracts`

## 目录

| 路径 | 说明 |
|------|------|
| `gdal_tools/core/` | 算法实现（不依赖 MCP/平台） |
| `gdal_tools/contracts/` | 机器可读契约 |
| `gdal_tools/cli.py` | 统一命令行 |
| `mcp/gdal-tools-mcp/` | MCP 适配（薄封装，转调 `run_tool`） |
| `docker/gdal.Dockerfile` | 共享运行镜像 |

## 配置

| 项 | 默认 | 说明 |
|----|------|------|
| `GDAL_TOOLS_WORKSPACES` | `/data` | 允许读写的根目录（容器内）；对应宿主机挂载盘 `MCP_DATA` |
| `GDAL_COMMAND_TIMEOUT_SECONDS` | `14400` | 单次 GDAL 命令超时 |
| `GDAL_TOOLS_MAX_CONCURRENT_JOBS` | `2` | MCP 内并发重任务数 |
| `GDAL_TOOLS_PORT` | `7861` | MCP SSE 端口 |

本包不内置鉴权与数据检索；对外暴露 MCP 时请在前面加 TLS 与访问控制。
