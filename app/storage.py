"""S3 client built on the EC2 instance profile.

Credentials are never configured here. boto3's default credential chain finds
the IAM role attached to this EC2 instance and fetches short-lived keys from the
metadata service, so nothing long-lived lives in .env or in the image. The role
cricketapp-s3-upload is scoped to this one bucket.
"""

import os

import boto3
from botocore.config import Config

BUCKET = os.getenv("S3_UPLOAD_BUCKET", "cricketapp-uploads-crizz")
REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "eu-north-1"

# Image uploads over the same connection as everything else, so cap the pool
# rather than letting boto3's default of 10 idle sockets linger per worker.
_config = Config(region_name=REGION, retries={"max_attempts": 3, "mode": "standard"})


def get_client():
    """A boto3 S3 client using the instance profile.

    No signature is passed, so the default chain is used deliberately: on EC2
    that resolves to the instance profile. Construct this lazily rather than at
    import time so a missing role surfaces as a request failure, not an
    application that will not boot.
    """
    return boto3.client("s3", config=_config)


def object_url(key: str) -> str:
    """Direct S3 URL. Not readable on its own: the bucket blocks public access.

    Kept for logging and for identifying which object a record points at. Use
    presigned_url for anything a browser has to load.
    """
    return f"https://{BUCKET}.s3.{REGION}.amazonaws.com/{key}"


def presigned_url(key: str, expires: int = 3600) -> str:
    """Time-limited URL a browser can load without AWS credentials.

    Needed because Block all public access is enabled, so a plain S3 URL returns
    403. Until CloudFront is in front of the bucket this is how images reach the
    client. expires is capped by the caller's own need, not by S3, but keep it
    short: every request that uses one of these spends a signature.
    """
    return get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=expires,
    )


# Only formats the app actually renders. The extension is derived from the
# detected content type rather than from the client-supplied filename, so a
# caller cannot choose the stored type by naming the file.
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

MAX_IMAGE_BYTES = 5 * 1024 * 1024


class InvalidImage(ValueError):
    """Raised when an upload is not an allowed image or is too large."""


def image_key(prefix: str, entity_id: int, content_type: str) -> str:
    """Build the S3 key for an uploaded image.

    The key is built entirely from prefix and a database id. Nothing from the
    client-supplied filename reaches the key, which is what closes a path
    traversal: a request naming "x/../../evil.py" cannot escape the prefix
    because the filename is never used.

    prefix is still caller-supplied, so it is constrained to a flat token.
    """
    suffix = ALLOWED_IMAGE_TYPES.get(content_type)
    if suffix is None:
        allowed = ", ".join(sorted(ALLOWED_IMAGE_TYPES))
        raise InvalidImage(f"Unsupported image type. Allowed: {allowed}")

    if not prefix or "/" in prefix or "\\" in prefix or prefix in {".", ".."}:
        raise InvalidImage("Invalid upload prefix")

    return f"{prefix}/{entity_id}{suffix}"


async def put_image(key: str, data: bytes, content_type: str) -> None:
    """Upload image bytes to S3.

    ContentType is set explicitly because presigned GETs are served with the
    stored content type. Without it a browser receives application/octet-stream
    and downloads the file rather than rendering it.
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise InvalidImage(f"Image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)}MB limit")

    get_client().put_object(
        Bucket=BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type,
    )


async def read_image(key: str) -> bytes | None:
    """Fetch object bytes, or None if the object is missing.

    Returns None rather than raising so a deleted or migrated-away image
    degrades to a broken <img> instead of a 500 on the page that lists it.
    """
    from botocore.exceptions import ClientError

    try:
        response = get_client().get_object(Bucket=BUCKET, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        raise
    return response["Body"].read()
