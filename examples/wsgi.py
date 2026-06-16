import os

# Gevent monkey-patching MUST happen before anything imports the stdlib socket,
# threading, etc. (i.e. before Django). Only patch when using the gevent ws
# backend so the channels/daphne setup is unaffected.
if os.environ.get("HYPERGEN_WS_BACKEND", "channels") == "gevent":
    from gevent import monkey
    monkey.patch_all()
    try:
        # Make psycopg2 cooperative under gevent (no-op if Postgres unused).
        from psycogreen.gevent import patch_psycopg
        patch_psycopg()
    except ImportError:
        pass

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
