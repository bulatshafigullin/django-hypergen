#!/bin/bash

set -euo pipefail

python manage.py collectstatic --no-input

# channels/ASGI (daphne):
# exec daphne --bind 0.0.0.0 -p 8000 --access-log - --verbosity 1 asgi:application

# gevent-websocket (gunicorn). HYPERGEN_WS_BACKEND=gevent makes wsgi.py monkey-patch.
export HYPERGEN_WS_BACKEND=gevent
exec gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
    -w 4 -b 0.0.0.0:8000 --access-logfile - --log-level warning wsgi:application
