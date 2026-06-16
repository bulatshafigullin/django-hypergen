from hypergen.template import *
from hypergen.liveview import *
from hypergen.context import *

# Select the websocket backend. "channels" (default) uses django-channels/ASGI;
# "gevent" uses gevent-websocket served by gunicorn. Both expose the same
# public names (HypergenWebsocketConsumer, ws_url, group_send). Read from the
# env (not settings) so this works at import time before Django is configured;
# it is a deploy-time choice that wsgi.py reads from the same variable.
import os

if os.environ.get("HYPERGEN_WS_BACKEND", "channels") == "gevent":
    from hypergen.websocket_gevent import *
else:
    from hypergen.websocket import *
