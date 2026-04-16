"""Alembic migrations shipped inside the package.

This ``__init__.py`` exists so setuptools discovers ``migrations/`` as
a Python subpackage and ships its contents (``env.py``, the
``script.py.mako`` template, and every ``versions/*.py`` revision) in
the wheel. Without it, ``pip install code-atelier-governance[migrations]``
ends up with ``alembic.ini`` pointing at a non-existent ``migrations/``
directory and ``governance migrate`` silently skips the alembic step,
leaving fresh installs on the v0.5 schema — the exact P0 this file
prevents.

Alembic itself never imports this module: the migrations/env.py entry
point is resolved by the alembic runner via ``script_location``, not
via Python's package system. Adding ``__init__.py`` is purely a
packaging hint for setuptools.
"""
