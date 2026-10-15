"""
services/storage.py — Object storage for contact photos (§26).

Backed by **Cloudflare R2**, not AWS S3, per an explicit team decision.
R2 exposes an S3-compatible API, so this is a plain ``boto3`` S3 client
pointed at R2's ``endpoint_url`` (``settings.storage_endpoint_url``) —
nothing in this module is AWS-specific; it would work unchanged against
real AWS S3 or any other S3-compatible store by swapping the endpoint.

Why presigned **POST** rather than a bare presigned PUT URL
-------------------------------------------------------------
§26.3 requires the 5MB size limit to be "enforced via the presigned
URL's policy conditions (S3-native enforcement, not just a client-side
check)". A plain presigned PUT URL (``generate_presigned_url('put_object',
...)``) carries no such policy — S3/R2 would accept any size PUT to that
URL. A presigned **POST** (``generate_presigned_post``) does support
``Conditions`` (``content-length-range``, exact ``Content-Type`` match),
which R2 enforces at the storage layer itself before the object is ever
written. This is a deliberate reading of the architecture doc's "S3-native
enforcement" requirement, not what its upload-flow diagram's casual "PUT
directly to S3" phrasing would suggest at a glance — the mobile client
does a multipart form POST (``url`` + ``fields``), not a raw PUT.

Fallback behavior — deliberately NOT fail-open
-------------------------------------------------
Unlike Redis (core/redis_client.py) or the email/push stubs, there is no
sensible degraded mode for object storage: an upload either has somewhere
to go or it doesn't. If ``settings.storage_*`` isn't fully configured,
every function here raises ``AppError(500, STORAGE_NOT_CONFIGURED, ...)``
immediately rather than silently no-op'ing — a missing upload is not an
acceptable "fail open" the way a missing rate-limit cache is.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import status

from app.core.config import settings
from app.core.errors import AppError, ErrorCodes

_ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB, §26.3
_PRESIGN_EXPIRY_SECONDS = 300  # 5 min, §26.2
_GET_URL_EXPIRY_SECONDS = 300

_client: Any = None
_client_init_attempted = False


def _get_client() -> Any:
    global _client, _client_init_attempted
    if _client is not None:
        return _client
    if _client_init_attempted:
        _raise_not_configured()
    _client_init_attempted = True

    if not (
        settings.storage_endpoint_url
        and settings.storage_bucket_name
        and settings.storage_access_key_id
        and settings.storage_secret_access_key
    ):
        _raise_not_configured()

    import boto3
    from botocore.config import Config as BotoConfig

    _client = boto3.client(
        "s3",
        endpoint_url=settings.storage_endpoint_url,
        aws_access_key_id=settings.storage_access_key_id,
        aws_secret_access_key=settings.storage_secret_access_key,
        region_name="auto",  # R2 doesn't use regions; "auto" is its documented convention
        config=BotoConfig(signature_version="s3v4"),
    )
    return _client


def _raise_not_configured() -> None:
    raise AppError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ErrorCodes.STORAGE_NOT_CONFIGURED,
        "Object storage is not configured on this server.",
    )


def validate_content_type(content_type: str) -> None:
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.INVALID_CONTENT_TYPE,
            f"Unsupported content type '{content_type}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}",
        )


def build_object_key(*, user_id: uuid.UUID, content_type: str) -> str:
    """``{user_id}/contacts/{uuid}.{ext}`` per §26.3 — scoped by user so
    bulk deletion on account removal (a later milestone) is a simple
    prefix-delete; the uuid-based filename (not user-controllable) is
    what prevents path traversal/overwrite."""
    ext = _ALLOWED_CONTENT_TYPES[content_type]
    return f"{user_id}/contacts/{uuid.uuid4()}.{ext}"


def create_presigned_upload(*, object_key: str, content_type: str) -> dict:
    """Returns ``{"url": ..., "fields": {...}, "expires_at": ...}`` — the
    mobile client does a multipart form POST with these fields plus the
    file, not a raw PUT (see module docstring for why)."""
    validate_content_type(content_type)
    client = _get_client()

    presigned = client.generate_presigned_post(
        Bucket=settings.storage_bucket_name,
        Key=object_key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, MAX_UPLOAD_BYTES],
        ],
        ExpiresIn=_PRESIGN_EXPIRY_SECONDS,
    )
    return {
        "url": presigned["url"],
        "fields": presigned["fields"],
        "object_key": object_key,
        "expires_at": datetime.now(timezone.utc)
        + timedelta(seconds=_PRESIGN_EXPIRY_SECONDS),
    }


def head_object(object_key: str) -> dict | None:
    """Returns ``{"content_type": ..., "size_bytes": ...}`` if the object
    exists, or ``None`` if it doesn't (a 404 from the store) — never
    raises for "not found," since that's an expected, handled outcome at
    confirm time (§26.2's "backend verifies the object actually exists"
    step), not an error condition."""
    client = _get_client()
    from botocore.exceptions import ClientError

    try:
        response = client.head_object(Bucket=settings.storage_bucket_name, Key=object_key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return {
        "content_type": response.get("ContentType"),
        "size_bytes": response.get("ContentLength"),
    }


def delete_object(object_key: str) -> None:
    client = _get_client()
    client.delete_object(Bucket=settings.storage_bucket_name, Key=object_key)


def delete_objects_by_prefix(prefix: str) -> int:
    """Bulk-deletes every object under ``prefix`` — used by account
    deletion (§27.2 step 3: "enqueue S3 object deletion for the user's
    full object prefix"). Object keys are built as
    ``{user_id}/contacts/{uuid}.{ext}`` (``build_object_key`` above)
    specifically so this is a cheap prefix delete rather than needing to
    enumerate each ``ContactPhoto`` row's key individually. Returns the
    number of objects deleted. A no-op (returns 0) if storage isn't
    configured — account deletion shouldn't fail outright just because
    this one cleanup step has nowhere to run; see the account-deletion
    service module for why that's an acceptable, logged trade-off here,
    in contrast to this module's usual fail-loud stance."""
    if not (
        settings.storage_endpoint_url
        and settings.storage_bucket_name
        and settings.storage_access_key_id
        and settings.storage_secret_access_key
    ):
        return 0

    client = _get_client()
    deleted = 0
    continuation_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": settings.storage_bucket_name, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = client.list_objects_v2(**kwargs)
        objects = response.get("Contents", [])
        if objects:
            client.delete_objects(
                Bucket=settings.storage_bucket_name,
                Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
            )
            deleted += len(objects)
        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")
    return deleted


def create_presigned_get_url(object_key: str) -> str:
    """Short-lived read URL (§26.3 — "the object's URL is not stored as a
    public permanent URL ... read access is via short-lived presigned GET
    URLs issued on demand, keeping the bucket private")."""
    client = _get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.storage_bucket_name, "Key": object_key},
        ExpiresIn=_GET_URL_EXPIRY_SECONDS,
    )
