from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ArtifactKind(str, Enum):
    screenshot = "screenshot"
    pdf = "pdf"
    download = "download"
    table = "table"
    chart = "chart"
    network_json = "network_json"
    storage_state = "storage_state"


class Artifact(BaseModel):
    kind: ArtifactKind
    label: Optional[str] = None

    # S3-backed artifact
    s3_url: Optional[str] = None
    mime: Optional[str] = None
    byte_length: Optional[int] = None
    sha256: Optional[str] = None

    data: Any = None

    # Provenance
    source_url: Optional[str] = None
    selector: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)