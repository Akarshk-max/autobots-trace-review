import typing

from httpx._types import PrimitiveData
from pydantic import BaseModel, ConfigDict

from src.autobots.integrations.app_types import AppTypes


class APIRequest(BaseModel):
    name: str | typing.Literal["anon"] = "anon"
    app_name: AppTypes = AppTypes.anon
    method: str
    url: str
    # content: typing.Optional[str] = None
    # data: typing.Optional[typing.Mapping[str, typing.Any]] = None
    headers: typing.Optional[typing.Mapping[str, str]] = None
    params: typing.Optional[typing.Mapping[str, typing.Union[PrimitiveData]]] = None
    files: typing.Optional[typing.Mapping[str, bytes]] = None
    body: typing.Optional[typing.Dict[str, typing.Any]] = None
    # json: typing.Optional[str] = None
    # cookies: typing.Optional[typing.Dict[str, str]] = None
    # auth: typing.Union[typing.Tuple[typing.Union[str, bytes]]] = None
    follow_redirects: typing.Union[bool] = False
    timeout: typing.Optional[float] = None
    # extensions: typing.Optional[typing.MutableMapping[str, typing.Any]] = None
    to_download: bool = (
        False  # When True, download content from URLs found in body values before sending
    )
    download_fields: typing.Optional[typing.List[str]] = (
        None  # When to_download=True, only resolve URLs in these body keys (None = all keys)
    )
    raw_body_url: typing.Optional[str] = (
        None  # When set, download bytes from this URL and send as raw body content (skips JSON serialization)
    )

    # Bytes (e.g. multipart upload file content) must be JSON-serializable at API boundaries.
    # We base64-encode bytes in JSON mode to avoid UnicodeDecodeError and JSON dumps failures.
    model_config = ConfigDict(arbitrary_types_allowed=True, ser_json_bytes="base64")

