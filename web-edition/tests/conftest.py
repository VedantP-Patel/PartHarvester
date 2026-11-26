"""Global Pytest Configuration and Fixtures for PartHarvester."""

import asyncio
import sys
import pytest


@pytest.fixture
def anyio_backend():
    """Run async tests on asyncio using modern Python 3.12+ loop_factory pattern without deprecated policies."""
    if sys.platform == "win32":
        return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})
    return "asyncio"
