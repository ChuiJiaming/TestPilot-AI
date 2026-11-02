"""Private bounded storage for rendered report files."""

from hashlib import sha256
import os
import re
from uuid import UUID

from apps.api.app.quality.artifacts import ArtifactStorageError


MAX_REPORT_BYTES = 8 * 1024 * 1024
REPORT_CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "json": "application/json",
    "junit": "application/xml",
}
_OBJECT_KEY = re.compile(
    r"^projects/[0-9a-f-]{36}/runs/[0-9a-f-]{36}/reports/[0-9a-f-]{36}\.(html|pdf|json|junit)$"
)


class ReportStorageError(ArtifactStorageError):
    """A report file is invalid, too large, or unavailable in private storage."""


def build_report_object_key(project_id, run_id, export_id, format: str) -> str:
    if format not in REPORT_CONTENT_TYPES:
        raise ReportStorageError("Report format is invalid")
    project = UUID(str(project_id))
    run = UUID(str(run_id))
    export = UUID(str(export_id))
    return f"projects/{project}/runs/{run}/reports/{export}.{format}"


class S3ReportStorage:
    def __init__(self, *, client, bucket: str, max_bytes: int = MAX_REPORT_BYTES):
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("an object storage bucket is required")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_REPORT_BYTES:
            raise ValueError("report storage limit must be between 1 byte and 8 MiB")
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
    def _validate_key(key: str) -> tuple[str, str]:
        if not isinstance(key, str) or _OBJECT_KEY.fullmatch(key) is None:
            raise ReportStorageError("Report object key is invalid")
        parts = key.split("/")
        try:
            for value in (parts[1], parts[3], parts[5].rsplit(".", 1)[0]):
                if str(UUID(value)) != value:
                    raise ValueError
        except (ValueError, IndexError) as exc:
            raise ReportStorageError("Report object key is invalid") from exc
        extension = parts[5].rsplit(".", 1)[1]
        return key, extension

    def put(self, key: str, data: bytes, *, content_type: str) -> str:
        safe_key, extension = self._validate_key(key)
        expected_type = REPORT_CONTENT_TYPES[extension]
        if content_type != expected_type:
            raise ReportStorageError("Report content type does not match its format")
        if not isinstance(data, bytes) or not data:
            raise ReportStorageError("Report data is empty or invalid")
        if len(data) > self.max_bytes:
            raise ReportStorageError("Report size exceeds the configured limit")
        digest = sha256(data).hexdigest()
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=safe_key, Body=data, ContentType=expected_type,
                ServerSideEncryption="AES256",
            )
        except Exception as exc:
            raise ReportStorageError("Report storage is unavailable") from exc
        return digest

    def get(self, key: str, *, max_bytes: int = MAX_REPORT_BYTES) -> bytes:
        safe_key, extension = self._validate_key(key)
        if type(max_bytes) is not int or max_bytes < 1:
            raise ReportStorageError("Report download limit is invalid")
        cap = min(self.max_bytes, max_bytes)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=safe_key)
            body = response["Body"]
            content_length = response.get("ContentLength")
            if isinstance(content_length, int) and content_length > cap:
                raise ReportStorageError("Report size exceeds the configured limit")
            stored_type = response.get("ContentType")
            if stored_type is not None and stored_type != REPORT_CONTENT_TYPES[extension]:
                raise ReportStorageError("Stored report content type does not match its format")
            data = body.read(cap + 1)
        except ReportStorageError:
            raise
        except Exception as exc:
            raise ReportStorageError("Report storage is unavailable") from exc
        finally:
            if "body" in locals():
                body.close()
        if not data or len(data) > cap:
            raise ReportStorageError("Report size exceeds the configured limit")
        return data

    def delete(self, key: str) -> None:
        safe_key, _ = self._validate_key(key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=safe_key)
        except Exception as exc:
            raise ReportStorageError("Report storage is unavailable") from exc

    def check(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            raise ReportStorageError("Report storage is unavailable") from exc
        return True
