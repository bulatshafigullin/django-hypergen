"""Settings used only by the Selenium browser tests.

Runs the examples under the gevent websocket backend with plain (non-manifest)
static storage so static files are served straight from the app dirs without a
collectstatic step.
"""
from settings import *  # noqa: F401,F403

DEBUG = True

# No hashed-name manifest needed; serve original filenames via staticfiles.
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"

# Single worker + in-process group bus (no Redis required for the tests).
HYPERGEN_WS_BACKEND = "gevent"
HYPERGEN_WS_REDIS_URL = None
