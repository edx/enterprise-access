"""
Constants for API client
"""
import backoff
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeoutError

# Field for the backoff decorator representing
# the backoff strategy for retries
# https://github.com/litl/backoff/blob/master/backoff/_wait_gen.py
retry_backoff_strategy = backoff.expo

autoretry_for_exceptions = (
    RequestsConnectionError,
    RequestsTimeoutError,
)
