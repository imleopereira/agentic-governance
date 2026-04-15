"""Enforce ``extra="forbid"`` + no-``Any`` lint on console response models.

This file is the single source of truth for the class-of-bugs that the v0.6
Pydantic response-model prereq is designed to eliminate. If any new model is
added to ``codeatelier_governance.console.models.responses`` without the
strict config, one of these tests will fail. The lint runs at collection time
via the ``conftest`` hook AND as explicit tests so CI catches it even when
nothing imports the models module.
"""
from __future__ import annotations

import inspect
import typing as _t

import pytest
from pydantic import BaseModel, ValidationError

from codeatelier_governance.console.models import responses as _responses


def _model_classes() -> list[type[BaseModel]]:
    return [
        obj
        for _, obj in inspect.getmembers(_responses, inspect.isclass)
        if issubclass(obj, BaseModel) and obj is not BaseModel
    ]


@pytest.mark.parametrize("model_cls", _model_classes())
def test_every_response_model_forbids_extra(model_cls: type[BaseModel]) -> None:
    """Every exported response model must declare ``extra='forbid'``."""
    cfg = model_cls.model_config
    assert cfg.get("extra") == "forbid", (
        f"{model_cls.__name__} must set model_config=ConfigDict(extra='forbid'); "
        f"got extra={cfg.get('extra')!r}"
    )
    assert cfg.get("strict") is True, (
        f"{model_cls.__name__} must set strict=True in model_config"
    )


def _annotation_contains_any(annotation: object) -> bool:
    """Recursively detect ``Any`` or untyped/parameterless ``dict``/``list``."""
    if annotation is _t.Any:
        return True
    # Bare ``dict`` / ``list`` (no type params) counts as "untyped".
    if annotation is dict or annotation is list:
        return True
    origin = _t.get_origin(annotation)
    if origin is None:
        return False
    args = _t.get_args(annotation)
    if not args:
        return True
    return any(_annotation_contains_any(a) for a in args)


@pytest.mark.parametrize("model_cls", _model_classes())
def test_every_field_is_typed(model_cls: type[BaseModel]) -> None:
    """No response-model field may use ``Any`` or an untyped ``dict``/``list``."""
    for field_name, field_info in model_cls.model_fields.items():
        ann = field_info.annotation
        assert not _annotation_contains_any(ann), (
            f"{model_cls.__name__}.{field_name} uses Any or an untyped "
            f"container ({ann!r}); declare explicit field types."
        )


def test_unexpected_field_raises_validation_error() -> None:
    """Constructing a model with an unexpected field must raise."""
    with pytest.raises(ValidationError):
        _responses.PolicyListResponse(
            policies=[],
            __unexpected_leak__="db_pool=5",  # type: ignore[call-arg]
        )


def test_subclass_with_extra_allow_raises_at_class_creation() -> None:
    """``StrictResponse.__init_subclass__`` fires at ``class`` statement time.

    This is the belt half of the belt-and-suspenders: even if a model is
    defined in a module the conftest lint doesn't scan (e.g. a dynamically
    imported plugin), the base class refuses to let ``extra='allow'``
    land. The error fires at import time, not at first-use time.
    """
    from pydantic import ConfigDict

    from codeatelier_governance.console.models.responses import PolicyRow

    with pytest.raises(TypeError, match="extra='forbid'"):

        class Evil(PolicyRow):  # type: ignore[misc]
            model_config = ConfigDict(extra="allow")


def test_nested_unexpected_field_raises_validation_error() -> None:
    """Nested shapes must also reject unknown keys."""
    with pytest.raises(ValidationError):
        _responses.GateClaimResponse(
            ok=True,
            request_id="rid",
            reviewer_id="rev",
            reviewing_since="2026-04-13T00:00:00+00:00",  # type: ignore[arg-type]
            internal_pool_size=5,  # type: ignore[call-arg]
        )
