# Shared image for all gdal-tools tasks (CLI + MCP).
# Build from package root:
#   docker build -f docker/gdal.Dockerfile -t gdal-tools:1.0 .

ARG PYTHON_IMAGE=docker.1ms.run/python:3.11-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app/gdal-tools
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/gdal-tools:/app/gdal-tools/mcp
ENV GDAL_TOOLS_WORKSPACES=/data
ENV MCP_TRANSPORT=sse
ENV GDAL_TOOLS_HOST=0.0.0.0
ENV GDAL_TOOLS_PORT=7861

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gdal-bin \
        python3-gdal \
        libgdal-dev \
        libproj-dev \
        proj-data \
        proj-bin \
        libgeos-dev \
        g++ \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "mcp>=1.12.0,<2" \
    "pydantic>=2.12.0,<3" \
    "PyYAML>=6.0.2" \
    "numpy>=1.26" \
    "rasterio==1.4.3" \
    geopandas==1.0.1 \
    pyogrio==0.10.0 \
    pyproj==3.7.0

COPY gdal_tools /app/gdal-tools/gdal_tools
COPY mcp /app/gdal-tools/mcp
COPY metadata /app/gdal-tools/metadata

ENTRYPOINT ["python", "-m", "gdal_tools"]
CMD ["serve-mcp", "--transport", "sse"]
