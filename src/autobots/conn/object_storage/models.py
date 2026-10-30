from dataclasses import dataclass, field


@dataclass(frozen=True)
class StorageObject:
    """Provider-neutral summary returned by object-storage listing operations."""

    key: str
    size: int | None = None
    etag: str | None = None


@dataclass(frozen=True)
class SignedUpload:
    """Information required by a client to upload directly to object storage."""

    url: str
    public_url: str
    key: str
    headers: dict[str, str] = field(default_factory=dict)
