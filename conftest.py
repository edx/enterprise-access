"""
Pytest fixtures for Enterprise Access tests.
"""
import pytest

@pytest.fixture(scope='session')
def celery_config():
    return {
        'broker_url': 'amqp://',
        'result_backend': 'redis://'
    }


@pytest.fixture(autouse=True)
def clear_cached_subsidy_clients():
    """
    Don't let a subsidy client cached by one test (see ``get_versioned_subsidy_client()``) reach the next, where it
    would bypass any patch of how clients are built.
    """
    # Imported here: importing it at module level would load Django models before Django is set up.
    # pylint: disable=import-outside-toplevel
    from enterprise_access.apps.subsidy_access_policy.utils import _subsidy_clients
    _subsidy_clients.by_version.clear()
    yield
    _subsidy_clients.by_version.clear()
