"""Section validation and (de)serialisation for the blackboard.

Both write and read go through this module, so a value can never enter or leave
the store in a shape its section does not declare.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from athena.blackboard.schemas import (
    ALL_SECTIONS,
    LIST_ADAPTERS,
    LIST_ITEM_MODELS,
    SECTION_MODELS,
)
from athena.blackboard.store import SectionTypeError, UnknownSectionError


def check_section(section: str) -> None:
    """Reject section names that are not in the registry."""
    if section not in ALL_SECTIONS:
        raise UnknownSectionError(
            f"unknown blackboard section {section!r}; "
            f"expected one of {list(ALL_SECTIONS)}"
        )


def check_value(section: str, value: Any) -> None:
    """Reject a value whose type does not match its section."""
    model = SECTION_MODELS.get(section)
    if model is not None:
        if not isinstance(value, model):
            raise SectionTypeError(
                f"section {section!r} expects a {model.__name__}, "
                f"got {type(value).__name__}"
            )
        return
    item_model = LIST_ITEM_MODELS[section]
    ok = isinstance(value, list) and all(isinstance(i, item_model) for i in value)
    if not ok:
        raise SectionTypeError(
            f"section {section!r} expects a list[{item_model.__name__}], "
            f"got {type(value).__name__}"
        )


def check_appendable(section: str, item: Any) -> None:
    """Reject appends to non-list sections, or items of the wrong model."""
    if section not in LIST_ITEM_MODELS:
        raise SectionTypeError(
            f"section {section!r} is not appendable; "
            f"append works on {sorted(LIST_ITEM_MODELS)}"
        )
    model = LIST_ITEM_MODELS[section]
    if not isinstance(item, model):
        raise SectionTypeError(
            f"section {section!r} expects {model.__name__} items, "
            f"got {type(item).__name__}"
        )


def dump_value(value: BaseModel | list[BaseModel]) -> str:
    """Serialise a section value to JSON."""
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps([item.model_dump(mode="json") for item in value])


def dump_item(item: BaseModel) -> str:
    """Serialise one list item to JSON."""
    return json.dumps(item.model_dump(mode="json"), ensure_ascii=False)


def decode_value(section: str, payload: str) -> BaseModel | list[BaseModel]:
    """Parse and validate a stored value; a corrupted row fails loudly."""
    try:
        if section in LIST_ADAPTERS:
            return LIST_ADAPTERS[section].validate_json(payload)
        return SECTION_MODELS[section].model_validate_json(payload)
    except ValidationError as exc:
        raise SectionTypeError(
            f"stored value for section {section!r} is not valid: {exc}"
        ) from exc
