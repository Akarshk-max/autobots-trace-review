from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import botocore.auth
import pytest
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.integrations.integration_type.aws_auth import AwsAuth, AwsModel
from src.autobots.integrations.app_types import AppTypes
from src.autobots.data_model.context import Context


@pytest.mark.asyncio
async def test_aws_sigv4_s3_matches_botocore_basic_get(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_dt = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: fixed_dt)

    access_key = "AKIDEXAMPLE"
    secret_key = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    url = "https://s3.us-east-1.amazonaws.com/mybucket/test.txt"

    ctx = Context()

    app_auth = AwsModel(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        bucket_name="mybucket",
        region_name="us-east-1",
        object_prefix="",
    )

    config = APIRequest(
        app_name=AppTypes.aws,
        method="GET",
        url=url,
        headers={"X-Amz-Date": "20150830T123600Z"},
    )

    ours = await AwsAuth.get_auth_header(ctx=ctx, app_auth=app_auth, config=config)

    req = AWSRequest(method="GET", url=url, data=b"", headers={})
    signer = S3SigV4Auth(Credentials(access_key, secret_key), "s3", "us-east-1")
    signer.add_auth(req)

    assert ours["Authorization"] == req.headers["Authorization"]
    assert ours["X-Amz-Date"] == req.headers["X-Amz-Date"]
    assert ours["X-Amz-Content-SHA256"] == req.headers["X-Amz-Content-SHA256"]


@pytest.mark.asyncio
async def test_aws_sigv4_s3_matches_botocore_with_params(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_dt = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: fixed_dt)

    access_key = "AKIDEXAMPLE"
    secret_key = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    base_url = "https://s3.us-east-1.amazonaws.com/mybucket"

    params = {"list-type": 2, "prefix": "a b", "max-keys": 1000}

    ctx = Context()
    app_auth = AwsModel(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        bucket_name="mybucket",
        region_name="us-east-1",
        object_prefix="",
    )

    config = APIRequest(
        app_name=AppTypes.aws,
        method="GET",
        url=base_url,
        params=params,
        headers={"X-Amz-Date": "20150830T123600Z"},
    )

    ours = await AwsAuth.get_auth_header(ctx=ctx, app_auth=app_auth, config=config)

    # botocore signs based on the URL's querystring, so build it how httpx would:
    # use percent-encoding with spaces -> %20 (NOT '+').
    query = urlencode(params, quote_via=quote, safe="-_.~")
    full_url = f"{base_url}?{query}"

    req = AWSRequest(method="GET", url=full_url, data=b"", headers={})
    signer = S3SigV4Auth(Credentials(access_key, secret_key), "s3", "us-east-1")
    signer.add_auth(req)

    assert ours["Authorization"] == req.headers["Authorization"]
    assert ours["X-Amz-Date"] == req.headers["X-Amz-Date"]
    assert ours["X-Amz-Content-SHA256"] == req.headers["X-Amz-Content-SHA256"]
