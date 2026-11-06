# Deprecated: use ../docker/gdal.Dockerfile (FROM mineral-tools-mcp-base:dev).
ARG PYTHON_IMAGE=docker.1ms.run/python:3.11-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/mineral-tools-mcp:/app/mineral-tools-mcp/mcp
ENV MCP_TRANSPORT=stdio

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gdal-bin \
        libgdal-dev \
        libproj-dev \
        proj-data \
        proj-bin \
        libgeos-dev \
        g++ \
    && rm -rf /var/lib/apt/lists/*

COPY mcp/gdal-tools-mcp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir rasterio==1.4.3 geopandas==1.0.1 pyproj==3.7.0 pyogrio==0.10.0 PyYAML==6.0.2

COPY mcp/shared /app/mineral-tools-mcp/mcp/shared
COPY mcp/gdal-tools-mcp /app/mineral-tools-mcp/mcp/gdal-tools-mcp
COPY metadata /app/mineral-tools-mcp/metadata

WORKDIR /app/mineral-tools-mcp/mcp
CMD ["python", "gdal-tools-mcp/server.py"]
