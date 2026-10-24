"""Change only the test process's timezone, restoring it even on failure."""
from contextlib import contextmanager
import os
import time
from unittest.mock import patch


@contextmanager
def process_timezone(name):
    try:
        with patch.dict(os.environ, {"TZ": name}):
            time.tzset()
            yield
    finally:
        time.tzset()
