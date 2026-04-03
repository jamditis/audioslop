"""Cloudflare R2 storage client for audioslop.

R2 is S3-compatible, so we use boto3 with a custom endpoint URL.

Environment variables:
    R2_ACCOUNT_ID   - Cloudflare account ID
    R2_ACCESS_KEY   - R2 access key ID
    R2_SECRET_KEY   - R2 secret access key

Bucket: audioslop
"""

import os

import boto3

BUCKET = "audioslop"

# Module-level client cache
_client = None


def get_client(account_id=None, access_key=None, secret_key=None):
    """Return the cached S3-compatible R2 client, creating it if needed.

    Explicit params override environment variables.
    """
    global _client
    if _client is not None:
        return _client

    account_id = account_id or os.environ["R2_ACCOUNT_ID"]
    access_key = access_key or os.environ["R2_ACCESS_KEY"]
    secret_key = secret_key or os.environ["R2_SECRET_KEY"]

    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )
    return _client


def upload_file(local_path, r2_key):
    """Upload a local file to R2.

    Args:
        local_path: Path to the local file.
        r2_key: Destination key in the R2 bucket.

    Returns:
        The r2_key that was uploaded.
    """
    client = get_client()
    client.upload_file(local_path, BUCKET, r2_key)
    return r2_key


def presigned_url(r2_key, expires_in=3600):
    """Generate a presigned GET URL for an object in R2.

    Args:
        r2_key: Key of the object in the R2 bucket.
        expires_in: URL expiry time in seconds (default 3600).

    Returns:
        Presigned URL string.
    """
    client = get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": r2_key},
        ExpiresIn=expires_in,
    )


def delete_prefix(prefix):
    """Delete all objects in R2 whose key starts with prefix.

    Args:
        prefix: Key prefix to match (e.g. "jobs/abc123/").

    Returns:
        Count of objects deleted.
    """
    client = get_client()
    paginator = client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=BUCKET, Prefix=prefix)

    deleted = 0
    for page in pages:
        objects = page.get("Contents", [])
        if not objects:
            continue
        keys = [{"Key": obj["Key"]} for obj in objects]
        client.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})
        deleted += len(keys)

    return deleted
