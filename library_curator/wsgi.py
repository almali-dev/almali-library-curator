"""WSGI entry point.

Run with ``gunicorn library_curator.wsgi:application``.
"""

from library_curator.app import create_app

application = create_app()
app = application
