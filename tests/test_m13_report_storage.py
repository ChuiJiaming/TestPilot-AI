from hashlib import sha256
import importlib
import io
from uuid import uuid4

import pytest


class _MemoryS3:
    def __init__(self):
        self.objects = {}
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

    def get_object(self, *, Bucket, Key):
        data = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def _storage_module():
    return importlib.import_module("apps.api.app.reports.storage")


def test_report_storage_accepts_only_server_report_keys_and_known_content_types():
    module = _storage_module()
    client = _MemoryS3()
    storage = module.S3ReportStorage(client=client, bucket="testpilot-private")
    project_id, run_id, export_id = uuid4(), uuid4(), uuid4()
    key = module.build_report_object_key(project_id, run_id, export_id, "json")
    payload = b'{"status":"PASSED"}'

    digest = storage.put(key, payload, content_type="application/json")

    assert digest == sha256(payload).hexdigest()
    assert storage.get(key) == payload
    assert client.put_calls[0]["ServerSideEncryption"] == "AES256"
    assert "ACL" not in client.put_calls[0]
    assert client.put_calls[0]["ContentType"] == "application/json"

    with pytest.raises(module.ReportStorageError, match="key"):
        storage.put("../../public.json", payload, content_type="application/json")
    with pytest.raises(module.ReportStorageError, match="content type"):
        storage.put(key, payload, content_type="image/png")


def test_report_storage_rejects_oversize_upload_and_download():
    module = _storage_module()
    client = _MemoryS3()
    storage = module.S3ReportStorage(client=client, bucket="testpilot-private", max_bytes=8)
    key = module.build_report_object_key(uuid4(), uuid4(), uuid4(), "json")

    with pytest.raises(module.ReportStorageError, match="size"):
        storage.put(key, b"123456789", content_type="application/json")
    assert client.put_calls == []

    client.objects[("testpilot-private", key)] = b"123456789"
    with pytest.raises(module.ReportStorageError, match="size"):
        storage.get(key)
