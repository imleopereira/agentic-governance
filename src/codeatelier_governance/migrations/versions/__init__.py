"""Alembic revision scripts packaged into the wheel.

Each ``*.py`` file here is an alembic revision. Alembic discovers them
by scanning the directory referenced via ``script_location`` in
``alembic.ini`` — it does NOT import them as Python modules, so the
usual caveat about sibling-module imports from a revision script does
not apply.

This ``__init__.py`` exists purely so setuptools treats ``versions/``
as a subpackage and ships the revision files inside the wheel.
"""
