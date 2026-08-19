"""Compatibility shim so ``pip install -e .`` works on older pip that lacks
PEP 660 (editable) support for pyproject-only projects. All real metadata lives
in ``pyproject.toml``.
"""

from setuptools import setup


setup()
