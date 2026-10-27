"""Strict v2 case DSL candidate frozen by the owner for Phase 2."""

from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class RoleLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: Literal["role"]
    value: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, min_length=1, max_length=512)
    exact: bool = False


class ValueLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: Literal["label", "placeholder", "text", "test_id", "css"]
    value: str = Field(min_length=1, max_length=1024)


Locator = Annotated[Union[RoleLocator, ValueLocator], Field(discriminator="by")]


class NavigateStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["navigate"]
    url: str = Field(min_length=1, max_length=2048)


class ClickStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["click"]
    locator: Locator


class FillStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["fill"]
    locator: Locator
    text: str = Field(max_length=12000)


Step = Annotated[
    Union[NavigateStep, ClickStep, FillStep], Field(discriminator="action")
]


class TextAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    locator: Locator
    expected: str = Field(min_length=1, max_length=4000)
    match: Literal["exact", "contains"]


class UrlAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["url"]
    expected: str = Field(min_length=1, max_length=2048)
    match: Literal["exact", "contains"]


Assertion = Annotated[
    Union[TextAssertion, UrlAssertion], Field(discriminator="type")
]


class TestCaseDslV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=2, le=2)
    case_id: UUID
    title: str = Field(min_length=1, max_length=250)
    steps: list[Step] = Field(min_length=1)
    assertions: list[Assertion] = Field(min_length=1)


class DslValidationFailed(ValueError):
    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("DSL validation failed")
        self.errors = errors


def validate_dsl(payload: dict[str, Any], *, expected_case_id: UUID) -> TestCaseDslV2:
    try:
        dsl = TestCaseDslV2.model_validate(payload)
    except ValidationError as exc:
        raise DslValidationFailed(exc.errors(include_input=False)) from exc
    if dsl.case_id != expected_case_id:
        raise DslValidationFailed([
            {"loc": ["case_id"], "msg": "case_id must match the target case", "type": "value_error"}
        ])
    return dsl
