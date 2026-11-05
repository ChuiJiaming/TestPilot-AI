"""Production-observability contract: correlation IDs and a scrapeable /metrics."""

import json
import uuid

from fastapi.testclient import TestClient

from apps.api.app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_request_id_is_echoed_and_reused_in_problem_details():
    request_id = str(uuid.uuid4())
    response = _client().get("/api/v1/health/live", headers={"X-Request-ID": request_id})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == request_id


def test_invalid_request_id_is_replaced_with_a_generated_one():
    response = _client().get(
        "/api/v1/health/live",
        headers={"X-Request-ID": "spaces are not allowed in a header token"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "spaces are not allowed in a header token"
    uuid.UUID(response.headers["X-Request-ID"])


def test_error_trace_id_matches_the_request_id():
    request_id = str(uuid.uuid4())
    response = _client().get("/api/v1/does-not-exist", headers={"X-Request-ID": request_id})
    assert response.status_code == 404
    assert response.json()["trace_id"] == request_id


def test_liveness_does_not_depend_on_backing_services():
    response = _client().get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_metrics_expose_red_metrics_for_served_requests():
    client = _client()
    client.get("/api/v1/health/live")
    body = client.get("/metrics").text
    assert "# TYPE testpilot_http_requests_total counter" in body
    assert 'testpilot_http_requests_total{method="GET",route="/api/v1/health/live",status="200"}' in body
    assert "testpilot_http_request_duration_seconds_sum" in body
    assert "testpilot_build_info" in body


def test_metrics_can_be_gated_by_a_bearer_token(monkeypatch):
    monkeypatch.setenv("TESTPILOT_METRICS_TOKEN", "scrape-me")
    client = _client()
    assert client.get("/metrics").status_code == 403
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 403
    allowed = client.get("/metrics", headers={"Authorization": "Bearer scrape-me"})
    assert allowed.status_code == 200


def test_unmatched_requests_do_not_explode_metric_cardinality():
    client = _client()
    client.get("/api/v1/health/live")
    for suffix in ("one", "two", "three"):
        assert client.get(f"/nothing/{suffix}").status_code == 404
    body = client.get("/metrics").text
    assert 'route="unmatched"' in body
    assert "/nothing/one" not in body


def test_access_log_lines_are_json_and_carry_the_request_id(capsys):
    # configure_logging is what a real deployment runs; the test app does not
    # call it, so install it here and restore afterwards.
    import logging

    from apps.api.app.core.observability import configure_logging

    configure_logging(level="INFO", fmt="json")
    try:
        request_id = str(uuid.uuid4())
        _client().get("/api/v1/health/live", headers={"X-Request-ID": request_id})
    finally:
        logging.getLogger().handlers.clear()
        logging.getLogger().setLevel(logging.WARNING)

    payloads = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("{")
    ]
    matching = [item for item in payloads if item.get("request_id") == request_id]
    assert matching, payloads
    assert matching[0]["path"] == "/api/v1/health/live"
    assert matching[0]["status_code"] == 200
