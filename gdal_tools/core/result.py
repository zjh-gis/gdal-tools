from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class ServiceResult:
    service_id: str
    status: str
    summary: str
    outputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "status": self.status,
            "summary": self.summary,
            "outputs": self.outputs,
            "warnings": self.warnings,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def require_path(path_value: str, field_name: str = "data_path") -> Path:
    if not path_value:
        raise ValueError(f"{field_name} is required")
    path = Path(path_value)
    if not path.exists():
        raise FileNotFoundError(f"{field_name} does not exist: {path}")
    return path
