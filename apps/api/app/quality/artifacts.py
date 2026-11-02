"""Private S3-compatible storage and validation for bounded PNG evidence."""

from hashlib import sha256
from io import BytesIO
import os
import re
from uuid import UUID

from PIL import Image, UnidentifiedImageError


MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_PIXELS = 32_000_000
_OBJECT_KEY = re.compile(
    r"^projects/[0-9a-f-]{36}/runs/[0-9a-f-]{36}/artifacts/[0-9a-f-]{36}\.png$"
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ArtifactStorageError(ValueError):
    """An artifact is invalid, too large, or unavailable in object storage."""


def build_artifact_object_key(project_id, run_id, artifact_id) -> str:
    """Return a canonical key derived only from trusted database identifiers."""
    project = UUID(str(project_id))
    run = UUID(str(run_id))
    artifact = UUID(str(artifact_id))
    return f"projects/{project}/runs/{run}/artifacts/{artifact}.png"


def validate_png_bytes(data: bytes, *, max_bytes: int = MAX_ARTIFACT_BYTES,
                       max_pixels: int = MAX_ARTIFACT_PIXELS) -> tuple[int, int]:
    if not isinstance(data, bytes) or not data:
        raise ArtifactStorageError("PNG data is empty or invalid")
    if len(data) > max_bytes:
        raise ArtifactStorageError("PNG size exceeds the configured limit")
    if not data.startswith(_PNG_SIGNATURE):
        raise ArtifactStorageError("Artifact content must be a PNG")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG":
                raise ArtifactStorageError("Artifact content must be a PNG")
            width, height = image.size
            if width < 1 or height < 1 or width * height > max_pixels:
                raise ArtifactStorageError("PNG dimensions exceed the configured limit")
            image.verify()
    except ArtifactStorageError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ArtifactStorageError("PNG data is corrupt") from exc
    return width, height


class S3ArtifactStorage:
    """Store private PNG evidence in a private S3 or MinIO bucket."""

    def __init__(self, *, client, bucket: str, max_bytes: int = MAX_ARTIFACT_BYTES):
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("an object storage bucket is required")
        self.client = client
        self.bucket = bucket.strip()
        self.max_bytes = max_bytes

    @classmethod
    def from_environment(cls):
        bucket = os.getenv("TESTPILOT_S3_BUCKET", "").strip()
        if not bucket:
            return None
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("boto3 is required when TESTPILOT_S3_BUCKET is configured") from exc
        client = boto3.client(
            "s3",
            endpoint_url=os.getenv("TESTPILOT_S3_ENDPOINT_URL") or None,
            region_name=os.getenv("TESTPILOT_S3_REGION", "us-east-1"),
            aws_access_key_id=os.getenv("TESTPILOT_S3_ACCESS_KEY_ID") or None,
            aws_secret_access_key=os.getenv("TESTPILOT_S3_SECRET_ACCESS_KEY") or None,
            config=Config(
                connect_timeout=2, read_timeout=5,
                retries={"max_attempts": 1, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )
        return cls(client=client, bucket=bucket)

    @staticmethod
    def _validate_key(key: str) -> str:
        if not isinstance(key, str) or not _OBJECT_KEY.fullmatch(key):
            raise ArtifactStorageError("Artifact object key is invalid")
        parts = key.split("/")
        for index in (1, 3, 5):
            try:
                segment = parts[index].removesuffix(".png") if index == 5 else parts[index]
                if str(UUID(segment)) != segment:
                    raise ValueError
            except (ValueError, IndexError) as exc:
                raise ArtifactStorageError("Artifact object key is invalid") from exc
        return key

    def put(self, key: str, data: bytes, *, content_type: str) -> str:
        safe_key = self._validate_key(key)
        if content_type != "image/png":
            raise ArtifactStorageError("Only image/png artifacts are supported")
        validate_png_bytes(data, max_bytes=self.max_bytes)
        digest = sha256(data).hexdigest()
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=safe_key,
                Body=data,
                ContentType=content_type,
                ServerSideEncryption="AES256",
            )
        except Exception as exc:
            raise ArtifactStorageError("Artifact storage is unavailable") from exc
        return digest

    def get(self, key: str) -> bytes:
        safe_key = self._validate_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=safe_key)
            body = response["Body"]
            content_length = response.get("ContentLength")
            if isinstance(content_length, int) and content_length > self.max_bytes:
                raise ArtifactStorageError("PNG size exceeds the configured limit")
            data = body.read(self.max_bytes + 1)
        except ArtifactStorageError:
            raise
        except Exception as exc:
            raise ArtifactStorageError("Artifact storage is unavailable") from exc
        finally:
            if "body" in locals():
                body.close()
        validate_png_bytes(data, max_bytes=self.max_bytes)
        return data

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._validate_key(key))
        except Exception as exc:
            raise ArtifactStorageError("Artifact storage is unavailable") from exc

    def check(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            raise ArtifactStorageError("Artifact storage is unavailable") from exc
        return True
