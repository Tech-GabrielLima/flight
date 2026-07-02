import pytest

import flight


@pytest.fixture(autouse=True)
def _clean_flight():
    """Guarantee no recording leaks across tests."""
    if flight.is_installed():
        flight.uninstall()
    yield
    if flight.is_installed():
        flight.uninstall()
