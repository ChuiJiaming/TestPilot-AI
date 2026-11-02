import html
import importlib
import json
from xml.etree import ElementTree

import pytest


def _renderer():
    module = importlib.import_module("apps.api.app.reports.rendering")
    return module.ReportRenderer()


def _snapshot(**run_overrides):
    run = {
        "id": "10000000-0000-4000-8000-000000000001",
        "project_id": "20000000-0000-4000-8000-000000000001",
        "environment_id": "30000000-0000-4000-8000-000000000001",
        "branch": "release/1.2",
        "commit_sha": "abc123def456",
        "browser_matrix": ["chromium"],
        "trigger_source": "ci",
        "status": "FAILED",
        "started_at": "2026-11-02T08:00:00+00:00",
        "finished_at": "2026-11-02T08:00:03+00:00",
        "duration_seconds": 3.0,
    }
    run.update(run_overrides)
    return {
        "schema_version": 1,
        "run": run,
        "summary": {
            "counts": {
                "total": 2, "passed": 1, "failed": 1, "error": 0,
                "skipped": 0, "cancelled": 0, "queued": 0, "running": 0,
                "other": 0,
            },
            "duration_seconds": 3.0,
        },
        "cases": [
            {
                "id": "40000000-0000-4000-8000-000000000001",
                "title": "Login works",
                "version": 3,
                "browser": "chromium",
                "status": "PASSED",
                "duration_seconds": 1.0,
                "steps": [],
            },
            {
                "id": "40000000-0000-4000-8000-000000000002",
                "title": "Checkout fails",
                "version": 1,
                "browser": "chromium",
                "status": "FAILED",
                "duration_seconds": 2.0,
                "steps": [{"sequence": 1, "status": "FAILED", "error_code": "ASSERTION_FAILED"}],
            },
        ],
        "artifacts": [],
        "usage": [],
        "snapshot_hash": "d" * 64,
    }


def test_json_and_junit_match_snapshot_facts():
    renderer = _renderer()
    snapshot = _snapshot()

    json_report = renderer.render(snapshot, format="json")
    junit_report = renderer.render(snapshot, format="junit")

    assert json.loads(json_report.data) == snapshot
    assert json_report.content_type == "application/json"
    root = ElementTree.fromstring(junit_report.data)
    suite = root.find("testsuite")
    assert suite is not None
    assert suite.attrib["tests"] == "2"
    assert suite.attrib["failures"] == "1"
    assert suite.attrib["errors"] == "0"
    cases = suite.findall("testcase")
    assert [case.attrib["name"] for case in cases] == ["Login works", "Checkout fails"]
    assert cases[0].find("failure") is None
    assert cases[1].find("failure").attrib["message"] == "ASSERTION_FAILED"
    assert "abc123def456" in junit_report.data.decode("utf-8") or "abc123def456" in str(root.attrib)


def test_html_escapes_untrusted_run_and_case_text():
    snapshot = _snapshot(branch="<script>alert(1)</script>")
    snapshot["cases"][0]["title"] = "<img src=x onerror=alert(1)>"

    report = _renderer().render(snapshot, format="html")
    rendered = report.data.decode("utf-8")

    assert "<script>" not in rendered
    assert "<img src=x" not in rendered
    assert html.escape("<script>alert(1)</script>") in rendered
    assert report.content_type.startswith("text/html")


def test_pdf_uses_simplified_chinese_cid_font():
    report = _renderer().render(_snapshot(branch="发布分支"), format="pdf")

    assert report.data.startswith(b"%PDF-")
    assert b"STSong-Light" in report.data
    assert report.content_type == "application/pdf"


def test_unknown_format_is_rejected():
    with pytest.raises(ValueError, match="format"):
        _renderer().render(_snapshot(), format="exe")
