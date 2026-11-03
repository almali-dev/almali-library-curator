"""WSGI entry point.

Run with ``gunicorn almali_library_curator.wsgi:application``.
"""

from almali_library_curator.app import create_app

application = create_app()
app = application
