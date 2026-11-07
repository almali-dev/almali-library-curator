import io
import logging
from almali_library_curator.logging_config import configure_logging
def test_existing_root_handler_redacts_child_logger_records():
    root = logging.getLogger()
    existing_stream = io.StringIO()
    existing = logging.StreamHandler(existing_stream)
    existing.setFormatter(logging.Formatter('%(message)s %(api_key)s'))
    root.addHandler(existing)
    try:
        configure_logging('INFO')
        logging.getLogger('almali_library_curator.child').info('child_event', extra={'api_key': 'do-not-log'})
    finally:
        root.removeHandler(existing)
        configure_logging('INFO')
    assert 'do-not-log' not in existing_stream.getvalue() and '***' in existing_stream.getvalue()
