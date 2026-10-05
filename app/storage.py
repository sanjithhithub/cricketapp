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
    """Public URL for an object served through CloudFront or direct S3.

    The bucket has Block all public access enabled, so these URLs are not
    readable without a signature. Images should be served through CloudFront or
    via a presigned URL instead; this helper only builds the location string.
    """
    return f"https://{BUCKET}.s3.{REGION}.amazonaws.com/{key}"
