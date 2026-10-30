from __future__ import annotations

from src.autobots.conn.object_storage.protocol import ObjectStorage
from src.autobots.core.settings import Settings, SettingsProvider


def _settings(settings: Settings | None) -> Settings:
    return settings or SettingsProvider.sget()


def _require(settings: Settings, *names: str) -> None:
    missing = [name for name in names if not getattr(settings, name, None)]
    if missing:
        raise ValueError("Missing object-storage configuration: " + ", ".join(sorted(missing)))


def get_storage(
    settings: Settings | None = None,
    object_prefix: str = "default",
) -> ObjectStorage:
    settings = _settings(settings)

    if settings.OBJECT_STORAGE_PROVIDER == "azure":
        _require(
            settings,
            "AZURE_STORAGE_ACCOUNT_URL",
            "AZURE_STORAGE_AUTH_MODE",
            "AZURE_STORAGE_ACCOUNT_KEY",
            "OBJECT_STORAGE_PRIVATE_CONTAINER",
        )
        if settings.AZURE_STORAGE_AUTH_MODE != "account_key":
            raise ValueError(
                f"Unsupported Azure Storage auth mode: {settings.AZURE_STORAGE_AUTH_MODE}"
            )
        from src.autobots.conn.azure.azure_blob_storage import AzureBlobStorage

        return AzureBlobStorage(
            account_url=settings.AZURE_STORAGE_ACCOUNT_URL,
            account_key=settings.AZURE_STORAGE_ACCOUNT_KEY,
            container_name=settings.OBJECT_STORAGE_PRIVATE_CONTAINER,
            object_prefix=object_prefix,
            is_system_storage=True,
        )

    _require(
        settings,
        "AWS_S3_BUCKET_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_S3_BUCKET_NAME",
    )
    from src.autobots.conn.aws.aws_s3 import AwsS3

    return AwsS3(
        region_name=settings.AWS_S3_BUCKET_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        bucket_name=settings.AWS_S3_BUCKET_NAME,
        object_prefix=object_prefix,
        is_system_storage=True,
    )


def get_public_storage(
    settings: Settings | None = None,
    object_prefix: str = "default",
) -> ObjectStorage:
    settings = _settings(settings)

    if settings.OBJECT_STORAGE_PROVIDER == "azure":
        _require(
            settings,
            "AZURE_STORAGE_ACCOUNT_URL",
            "AZURE_STORAGE_AUTH_MODE",
            "AZURE_STORAGE_ACCOUNT_KEY",
            "OBJECT_STORAGE_PUBLIC_CONTAINER",
        )
        if settings.AZURE_STORAGE_AUTH_MODE != "account_key":
            raise ValueError(
                f"Unsupported Azure Storage auth mode: {settings.AZURE_STORAGE_AUTH_MODE}"
            )
        from src.autobots.conn.azure.azure_blob_storage import AzureBlobStorage

        return AzureBlobStorage(
            account_url=settings.AZURE_STORAGE_ACCOUNT_URL,
            account_key=settings.AZURE_STORAGE_ACCOUNT_KEY,
            container_name=settings.OBJECT_STORAGE_PUBLIC_CONTAINER,
            object_prefix=object_prefix,
            is_system_storage=True,
        )

    _require(
        settings,
        "AWS_S3_BUCKET_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_S3_PUBLIC_BUCKET_NAME",
    )
    from src.autobots.conn.aws.aws_s3 import AwsS3

    return AwsS3(
        region_name=settings.AWS_S3_BUCKET_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        bucket_name=settings.AWS_S3_PUBLIC_BUCKET_NAME,
        object_prefix=object_prefix,
        is_system_storage=True,
    )


def get_image_storage(
    settings: Settings | None = None,
    object_prefix: str = "",
) -> ObjectStorage:
    return get_public_storage(settings=settings, object_prefix=object_prefix)
