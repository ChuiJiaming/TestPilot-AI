from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.operations.model_config import (
    MODEL_CONFIG_KEY,
    ModelConfigError,
    ModelConfigService,
    model_options,
    parse_model_config,
    resolve_model_config,
)
from apps.api.app.core.audit import AuditLogRecord
from apps.api.app.operations.persistence import (
    SystemSettingChangeRecord,
    SystemSettingRecord,
)
from apps.api.app.operations.settings import SettingInvalid, SystemSettingService

NOW = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)

VALID_CONFIG = {
    "base_url": "https://api.example.test/v1",
    "api_key_env": "TESTPILOT_LLM_API_KEY",
    "planner_model": "planner-large",
    "run_agent_model": "agent-small",
    "pricing": {
        "planner-large": {"input_usd_per_million": 1.5, "output_usd_per_million": 6},
        "agent-small": {"input_usd_per_million": 0.15, "output_usd_per_million": 0.6},
    },
}


@pytest.fixture
def model_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AuthBase.metadata.create_all(engine, tables=[
        UserRecord.__table__, SystemSettingRecord.__table__,
        SystemSettingChangeRecord.__table__, AuditLogRecord.__table__,
    ])
    sessions = sessionmaker(engine, expire_on_commit=False)
    admin_id, member_id = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add_all([
            UserRecord(id=admin_id, email="admin@example.test", password_hash="hash",
                       display_name="Admin", platform_role="platform_admin",
                       status="active", created_at=NOW, email_verified_at=NOW),
            UserRecord(id=member_id, email="member@example.test", password_hash="hash",
                       display_name="Member", platform_role="user",
                       status="active", created_at=NOW, email_verified_at=NOW),
        ])
    yield {"sessions": sessions, "admin_id": admin_id, "member_id": member_id}
    engine.dispose()


def _store(sessions, value):
    with sessions.begin() as session:
        session.add(SystemSettingRecord(
            id=uuid4(), key=MODEL_CONFIG_KEY, value=value, updated_at=NOW,
        ))


def test_config_rejects_a_secret_instead_of_an_environment_variable_name():
    with pytest.raises(ModelConfigError):
        parse_model_config({**VALID_CONFIG, "api_key_env": "sk-live-123456"})


def test_config_requires_pricing_for_every_configured_model():
    with pytest.raises(ModelConfigError):
        parse_model_config({
            **VALID_CONFIG,
            "pricing": {"planner-large": VALID_CONFIG["pricing"]["planner-large"]},
        })


def test_config_rejects_plain_http_off_loopback():
    with pytest.raises(ModelConfigError):
        parse_model_config({**VALID_CONFIG, "base_url": "http://api.example.test/v1"})
    loopback = parse_model_config({**VALID_CONFIG, "base_url": "http://127.0.0.1:8000/v1"})
    assert loopback["base_url"] == "http://127.0.0.1:8000/v1"


def test_config_rejects_unknown_or_missing_fields():
    with pytest.raises(ModelConfigError):
        parse_model_config({**VALID_CONFIG, "api_key": "leak"})
    with pytest.raises(ModelConfigError):
        parse_model_config({"base_url": VALID_CONFIG["base_url"]})


def test_negative_or_non_finite_rates_are_rejected():
    with pytest.raises(ModelConfigError):
        parse_model_config({
            **VALID_CONFIG,
            "pricing": {"agent-small": {"input_usd_per_million": -1,
                                        "output_usd_per_million": 0}},
        })


def test_setting_service_accepts_a_valid_model_config(model_db):
    service = SystemSettingService(model_db["sessions"])

    change = service.request_change(
        model_db["admin_id"], MODEL_CONFIG_KEY, value=VALID_CONFIG,
        reason="Configure the platform model provider", now=NOW,
    )

    assert change["status"] == "APPROVED"
    assert change["approved_by"] == model_db["admin_id"]
    assert change["proposed_value_hash"]


def test_setting_service_still_rejects_secret_settings(model_db):
    service = SystemSettingService(model_db["sessions"])

    with pytest.raises(SettingInvalid):
        service.request_change(
            model_db["admin_id"], "llm_api_key", value="sk-live-123456",
            reason="Rotate the key", now=NOW,
        )


def test_approved_config_wins_over_environment(model_db):
    _store(model_db["sessions"], VALID_CONFIG)

    config = resolve_model_config(model_db["sessions"], environ={
        "TESTPILOT_LLM_API_KEY": "env-key",
        "TESTPILOT_RUN_AGENT_MODEL": "env-model",
        "TESTPILOT_LLM_PRICING_JSON": '{"env-model": {"input_usd_per_million": 1, '
                                      '"output_usd_per_million": 1}}',
    })

    assert config.source == "database"
    assert config.base_url == "https://api.example.test/v1"
    assert config.run_agent_model == "agent-small"
    assert config.ready is True
    assert config.rates_for("agent-small")["input_usd_per_million"] == Decimal("0.15")


def test_invalid_stored_config_falls_back_to_environment(model_db):
    _store(model_db["sessions"], {"base_url": "not-a-url"})

    config = resolve_model_config(model_db["sessions"], environ={
        "TESTPILOT_LLM_API_KEY": "env-key",
        "TESTPILOT_LLM_BASE_URL": "https://env.example.test/v1",
        "TESTPILOT_PLANNER_MODEL": "env-planner",
        "TESTPILOT_RUN_AGENT_MODEL": "env-model",
        "TESTPILOT_LLM_PRICING_JSON": '{"env-model": {"input_usd_per_million": 1, '
                                      '"output_usd_per_million": 2}, '
                                      '"env-planner": {"input_usd_per_million": 1, '
                                      '"output_usd_per_million": 2}}',
    })

    assert config.source == "environment"
    assert config.base_url == "https://env.example.test/v1"
    assert config.planner_model == "env-planner"
    assert config.ready is True


def test_missing_key_and_unpriced_model_are_reported_as_issues(model_db):
    config = resolve_model_config(model_db["sessions"], environ={
        "TESTPILOT_RUN_AGENT_MODEL": "agent-small",
        "TESTPILOT_LLM_PRICING_JSON": '{"planner-large": {"input_usd_per_million": 1, '
                                      '"output_usd_per_million": 1}}',
    })
    codes = {issue["code"] for issue in config.issues}

    assert config.source == "environment"
    assert codes == {"API_KEY_MISSING", "RUN_AGENT_MODEL_UNPRICED"}
    assert config.ready is False


def test_model_options_never_expose_rates_or_endpoint(model_db):
    _store(model_db["sessions"], VALID_CONFIG)
    config = resolve_model_config(model_db["sessions"], environ={"TESTPILOT_LLM_API_KEY": "k"})

    options = model_options(config)

    assert options == {"items": ["agent-small", "planner-large"], "default": "planner-large"}


def test_model_config_service_requires_platform_administrator(model_db):
    _store(model_db["sessions"], VALID_CONFIG)
    service = ModelConfigService(model_db["sessions"], environ={"TESTPILOT_LLM_API_KEY": "k"})

    with pytest.raises(Exception) as caught:
        service.view(model_db["member_id"])
    assert type(caught.value).__name__ == "SettingForbidden"

    view = service.view(model_db["admin_id"])
    assert view["ready"] is True
    assert view["api_key_configured"] is True
    assert "api_key" not in view
    assert view["updated_at"] is not None
