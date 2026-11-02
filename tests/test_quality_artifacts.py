import base64
from hashlib import sha256
import io
from uuid import uuid4

import pytest

from apps.api.app.quality.artifacts import (
    ArtifactStorageError,
    S3ArtifactStorage,
    build_artifact_object_key,
    validate_png_bytes,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
)


class _MemoryS3:
    def __init__(self):
        self.objects = {}
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def test_artifact_object_key_is_server_generated_and_scoped_by_project_run_and_id():
    project_id, run_id, artifact_id = uuid4(), uuid4(), uuid4()

    key = build_artifact_object_key(project_id, run_id, artifact_id)

    assert key == f"projects/{project_id}/runs/{run_id}/artifacts/{artifact_id}.png"
    assert ".." not in key
    assert not key.startswith("/")


def test_png_validation_checks_format_dimensions_and_size():
    width, height = validate_png_bytes(PNG_1X1, max_bytes=128, max_pixels=1)

    assert (width, height) == (1, 1)
    with pytest.raises(ArtifactStorageError, match="PNG"):
        validate_png_bytes(b"not an image", max_bytes=128, max_pixels=100)
    with pytest.raises(ArtifactStorageError, match="size"):
        validate_png_bytes(PNG_1X1, max_bytes=8, max_pixels=100)


def test_s3_storage_keeps_objects_private_and_verifies_content_hash():
    client = _MemoryS3()
    storage = S3ArtifactStorage(client=client, bucket="testpilot-private")
    key = f"projects/{uuid4()}/runs/{uuid4()}/artifacts/{uuid4()}.png"

    stored = storage.put(key, PNG_1X1, content_type="image/png")
    retrieved = storage.get(key)

    assert stored == sha256(PNG_1X1).hexdigest()
    assert retrieved == PNG_1X1
    assert client.put_calls[0]["ContentType"] == "image/png"
    assert "ACL" not in client.put_calls[0]
    assert client.put_calls[0]["ServerSideEncryption"] == "AES256"
    storage.delete(key)
    assert ("testpilot-private", key) not in client.objects


def test_s3_storage_rejects_caller_supplied_path_traversal_keys():
    storage = S3ArtifactStorage(client=_MemoryS3(), bucket="testpilot-private")

    with pytest.raises(ArtifactStorageError, match="key"):
        storage.put("../../public.png", PNG_1X1, content_type="image/png")
