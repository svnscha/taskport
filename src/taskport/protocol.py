"""Wire models and validation shared by the server, SDK, and worker."""

import json
import re
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$"
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
TERMINAL = frozenset({"succeeded", "failed", "lost", "cancelled"})
RESERVED = frozenset({"python", "output", "inputs", "result"})


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def guid(value: str) -> str:
    return str(UUID(value))


def _check_refs(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                raise ValueError("Input schemas may only reference their own document (#...)")
            _check_refs(item)
    elif isinstance(value, list):
        for item in value:
            _check_refs(item)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Manifest(WireModel):
    name: str = Field(pattern=NAME)
    version: str = Field(pattern=NAME)
    description: str = Field(default="", max_length=4096)
    command: list[str] = Field(min_length=1, max_length=256)
    inputs: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "additionalProperties": False}
    )
    timeout_seconds: float = Field(default=3600, gt=0, le=604800, allow_inf_nan=False)

    @field_validator("inputs")
    @classmethod
    def check_schema(cls, value: dict) -> dict:
        canonical(value)
        _check_refs(value)
        try:
            Draft202012Validator.check_schema(value)
        except Exception as exc:
            raise ValueError(f"Invalid input JSON Schema: {exc}") from exc
        if value.get("type") != "object":
            raise ValueError("The input schema must have type: object")
        if RESERVED.intersection(value.get("properties", {})):
            raise ValueError(f"Reserved input names: {', '.join(sorted(RESERVED))}")
        return value

    @model_validator(mode="after")
    def check_command(self):
        known = set(self.inputs.get("properties", {})) | RESERVED
        for argument in self.command:
            if "\0" in argument:
                raise ValueError("Commands cannot contain NUL")
            for name in PLACEHOLDER.findall(argument):
                if name not in known:
                    raise ValueError(f"Unknown command placeholder: {{{name}}}")
        if not self.command[0]:
            raise ValueError("Command executable cannot be empty")
        return self

    def validate_arguments(self, arguments: dict) -> None:
        canonical(arguments)
        Draft202012Validator(self.inputs).validate(arguments)
        missing = {
            name
            for argument in self.command
            for name in PLACEHOLDER.findall(argument)
            if name not in arguments and name not in RESERVED
        }
        if missing:
            raise ValueError(f"Missing command arguments: {', '.join(sorted(missing))}")


class Submission(WireModel):
    function: str = Field(pattern=NAME)
    version: str | None = Field(default=None, pattern=NAME)
    inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("inputs")
    @classmethod
    def finite_json(cls, value: dict) -> dict:
        canonical(value)
        return value


class Claim(WireModel):
    request_id: UUID
    function: str = Field(pattern=NAME)


class Completion(WireModel):
    exit_code: int
    result: Any = None
    error: str | None = Field(default=None, max_length=8192)

    @field_validator("result")
    @classmethod
    def finite_json(cls, value: Any) -> Any:
        if len(canonical(value).encode("utf-8")) > 1024 * 1024:
            raise ValueError("Result JSON must be at most 1 MiB; use an artifact for larger data")
        return value


class Problem(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)
