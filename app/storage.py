"""S3 client built on the EC2 instance profile.

Credentials are never configured here. boto3's default credential chain finds
the IAM role attached to this EC2 instance and fetches short-lived keys from the
metadata service, so nothing long-lived lives in .env or in the image. The role
cricketapp-s3-upload is scoped to this one bucket.
"""

import os
from pathlib import PurePosixPath

import boto3
from botocore.config import Config

BUCKET = os.getenv("S3_UPLOAD_BUCKET", "cricketapp-uploads-crizz")
REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "eu-north-1"

# Image uploads over the same connection as everything else, so cap the pool
# rather than letting boto3's default of 10 idle sockets linger per worker.
_config = Config(region_name=REGION, retries={"max_attempts": 3, "mode": "standard"})


def _storage_backend() -> str:
    """Which backend put_image/read_image use.

    "s3" (the default) goes through the instance profile and is what production
    runs. "local" writes under UPLOADS_DIR so a development machine with no AWS
    role can still upload; the /uploads route already reads that directory.

    Read per call rather than cached at import so it is a .env change and a
    restart, matching UPLOADS_CDN_BASE_URL in app.main. The split is explicit
    rather than an automatic fallback: quietly writing to disk on an EC2 host
    that has a role would hide a broken upload path in production.
    """
    return os.getenv("STORAGE_BACKEND", "s3").strip().lower()


def _uploads_dir() -> str:
    """Directory the local backend reads and writes. Shared with app.main."""
    return os.getenv("UPLOADS_DIR", "uploads")


def _use_local() -> bool:
    return _storage_backend() == "local"


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


# The magic numbers of the formats the app accepts.
#
# A client-declared content type is a claim, not a fact: it is a header the
# caller controls, and anything can be sent with "Content-Type: image/png"
# attached. The bytes themselves are what say what the file is, so the type of
# an upload is detected from them and the declared header is never trusted.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_type(data: bytes) -> str | None:
    """The image type the bytes actually are, or ``None`` if they are not one.

    Detected from the file's own signature rather than from the content type
    the client sent, so a request that claims to be a PNG but carries a
    script is refused instead of being written to the bucket and served back
    with a content type that makes a browser run it.

    A WebP file is a RIFF container with "WEBP" at offset 8, so it needs its own
    check rather than a prefix match.
    """
    if not data:
        return None
    for signature, media_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return media_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _local_path(key: str) -> str:
    """Resolve an object key under the local uploads directory.

    Rejects anything that would escape that directory. put_image receives keys
    built by image_key (flat prefix + database id, so no traversal is possible),
    but read_image also sees keys that arrived over the wire, so the check lives
    here as the last line of defence.
    """
    candidate = PurePosixPath(key)
    if candidate.is_absolute() or not candidate.parts:
        raise InvalidImage("Invalid storage key")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise InvalidImage("Invalid storage key")

    root = os.path.abspath(_uploads_dir())
    path = os.path.abspath(os.path.join(root, *candidate.parts))
    if path != root and not path.startswith(root + os.sep):
        raise InvalidImage("Invalid storage key")
    return path


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
    """Upload image bytes to the configured backend.

    ContentType is set explicitly because presigned GETs are served with the
    stored content type. Without it a browser receives application/octet-stream
    and downloads the file rather than rendering it. The local backend keeps the
    extension in the key, which is what the /uploads route keys off instead.
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise InvalidImage(f"Image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)}MB limit")

    if _use_local():
        path = _local_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return

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
    if _use_local():
        try:
            path = _local_path(key)
        except InvalidImage:
            return None
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    from botocore.exceptions import ClientError

    try:
        response = get_client().get_object(Bucket=BUCKET, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        raise
    return response["Body"].read()


async def delete_image(key: str) -> bool:
    """Delete an object, and report whether there was one to delete.

    A missing object is not an error. The caller is cleaning up after a
    replace - the old photo of an official who has just uploaded a new one -
    and the file may already be gone, in which case the cleanup has nothing to
    do and the record is what matters.
    """
    if _use_local():
        try:
            path = _local_path(key)
        except InvalidImage:
            return False
        if not os.path.isfile(path):
            return False
        os.remove(path)
        return True

    from botocore.exceptions import ClientError

    try:
        get_client().delete_object(Bucket=BUCKET, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return False
        raise
    return True
