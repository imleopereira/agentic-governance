"""Enforce ``extra="forbid"`` + no-``Any`` lint on console response models.

This file is the single source of truth for the class-of-bugs that the v0.6
Pydantic response-model prereq is designed to eliminate. If any new model is
added to the ``codeatelier_governance.console`` package — anywhere, not just
``responses.py`` — without the strict config, one of these tests will fail.

The lint walks EVERY submodule of ``codeatelier_governance.console`` (so
subclasses defined in ``app.py``, plugin modules, fixtures etc. land in
``StrictResponse.__subclasses__()``) and then enumerates every subclass of
``StrictResponse`` recursively. The earlier revision only scanned
``responses.py`` and silently skipped subclasses elsewhere — a
false-confidence hole flagged by Devil's Advocate on 2026-04-15.
"""
from __future__ import annotations

import importlib
import pkgutil
import typing as _t

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from codeatelier_governance.console.models import responses as _responses
from codeatelier_governance.console.models.responses import StrictResponse


def _walk_console_package() -> None:
    """Import every submodule of ``codeatelier_governance.console``.

    Submodules must be imported so their ``StrictResponse`` subclasses
    register in ``StrictResponse.__subclasses__()``. Any import failure is
    re-raised — a broken module in the console package is a real bug we
    want surfaced at collection time, not silently swallowed.
    """
    import codeatelier_governance.console as _console

    for mod in pkgutil.walk_packages(_console.__path__, _console.__name__ + "."):
        # Some submodules (``__main__``) execute server startup on import;
        # skip those to keep the lint pure. The lint still exercises
        # ``app``/``auth``/``models.*`` which is where every shipped
        # response model lives.
        if mod.name.endswith(".__main__"):
            continue
        importlib.import_module(mod.name)


def _all_subclasses(cls: type) -> list[type]:
    """Return ``cls`` subclasses transitively (depth-first)."""
    seen: list[type] = []
    stack: list[type] = list(cls.__subclasses__())
    while stack:
        sub = stack.pop()
        if sub in seen:
            continue
        seen.append(sub)
        stack.extend(sub.__subclasses__())
    return seen


# Walk at import time so parametrize decorators below see every subclass.
_walk_console_package()


def _strict_response_subclasses() -> list[type[BaseModel]]:
    subs = _all_subclasses(StrictResponse)
    # Keep only concrete Pydantic response models.
    return [c for c in subs if issubclass(c, BaseModel) and c is not BaseModel]


@pytest.mark.parametrize("model_cls", _strict_response_subclasses())
def test_every_strict_response_subclass_forbids_extra(
    model_cls: type[BaseModel],
) -> None:
    """Every StrictResponse subclass ANYWHERE must declare ``extra='forbid'``.

    Scope is the whole ``codeatelier_governance.console`` package, not just
    ``responses.py``. A rogue subclass defined in ``app.py`` or a plugin
    module must fail the same lint as one defined next to the base class.
    """
    cfg = model_cls.model_config
    assert cfg.get("extra") == "forbid", (
        f"{model_cls.__module__}.{model_cls.__name__} must set "
        f"model_config=ConfigDict(extra='forbid'); got extra={cfg.get('extra')!r}"
    )
    assert cfg.get("strict") is True, (
        f"{model_cls.__module__}.{model_cls.__name__} must set strict=True "
        f"in model_config; got strict={cfg.get('strict')!r}"
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


@pytest.mark.parametrize("model_cls", _strict_response_subclasses())
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

    Uses ``exec`` so the ``class`` statement executes inside a fresh
    namespace at the moment ``exec`` runs — proving the raise happens at
    class-creation time and not at first instantiation. An accidental move
    of the guard to ``model_post_init`` would therefore no longer pass.
    """
    src = (
        "from pydantic import ConfigDict\n"
        "from codeatelier_governance.console.models.responses import PolicyRow\n"
        "class Evil(PolicyRow):\n"
        "    model_config = ConfigDict(extra='allow', strict=True)\n"
    )
    with pytest.raises(TypeError, match="extra='forbid'"):
        exec(src, {})


def test_subclass_with_extra_ignore_raises_at_class_creation() -> None:
    """``extra='ignore'`` is the sneakiest regression — silently drops
    unknown fields without raising at serialization time. Must be rejected
    by ``__init_subclass__`` just like ``extra='allow'``.
    """
    from codeatelier_governance.console.models.responses import PolicyRow

    with pytest.raises(TypeError, match="extra='forbid'"):

        class Sneaky(PolicyRow):  # type: ignore[misc]
            model_config = ConfigDict(extra="ignore", strict=True)


def test_subclass_with_strict_false_raises_at_class_creation() -> None:
    """``strict=False`` relaxes Pydantic's type coercion and would let
    string-typed numbers etc. slip through. Must be rejected at class
    creation regardless of the ``extra`` setting.
    """
    from codeatelier_governance.console.models.responses import PolicyRow

    with pytest.raises(TypeError, match="strict"):

        class Loose(PolicyRow):  # type: ignore[misc]
            model_config = ConfigDict(extra="forbid", strict=False)


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
