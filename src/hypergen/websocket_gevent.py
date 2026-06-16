"""Gevent-websocket backend for hypergen, a drop-in replacement for the
django-channels based :mod:`hypergen.websocket`.

Why this exists
---------------
The original ``hypergen.websocket`` is built on django-channels / ASGI and
served by daphne.  This module re-implements the *same public API* on top of
``gevent`` + ``gevent-websocket`` served by gunicorn, so that downstream code
keeps working with only a server-command and routing change.

Public surface kept identical
-----------------------------
``HypergenWebsocketConsumer`` (with ``groups``, ``receive_hypergen_callback``,
``channel_send_hypergen_commands``, ``group_send_hypergen_commands``,
``encode_json``/``decode_json``), ``ws_url`` and module-level ``group_send``.

What channels did for us, and how it is replaced here
-----------------------------------------------------
* WS handshake / framing  -> ``environ['wsgi.websocket']`` (gevent-websocket).
* ws/ routing             -> plain Django ``urls.py`` + :func:`ws_view`
                             (``as_asgi`` is kept as a thin shim).
* auth                    -> a real ``WSGIRequest`` whose ``.user`` is already
                             populated by Django's auth middleware; no
                             ``ASGIRequest(scope)`` reconstruction needed.
* groups + cross-process  -> :class:`GroupRegistry` (in-process) plus a Redis
  fan-out (channel layer)    pub/sub bridge for delivery across gunicorn
                             workers / hosts.

Deployment notes
----------------
* ``wsgi.py`` MUST monkey-patch before Django is imported::

      from gevent import monkey; monkey.patch_all()
      # for Postgres also: from psycogreen.gevent import patch_psycopg; patch_psycopg()

* Run with::

      gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker \\
          -w 4 -b 0.0.0.0:8000 wsgi:application

* Configure the cross-worker bus with ``settings.HYPERGEN_WS_REDIS_URL``
  (e.g. ``"redis://127.0.0.1:6379/0"``).  If unset, the registry runs
  in-process only (fine for a single worker / development, equivalent to
  channels' ``InMemoryChannelLayer``).
"""
import json
from functools import wraps
from uuid import uuid4

d = dict

from hypergen.imports import *
from hypergen.hypergen import check_perms

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest

try:
    import gevent
    from gevent.queue import Queue
    from gevent.lock import RLock

    def assert_gevent():
        pass
except ImportError:

    Queue = None
    RLock = None
    gevent = None

    def assert_gevent():
        if getattr(settings, "HYPERGEN_INTERNAL_ONLY_ENFORCE_ASSERT_CHANNELS", True):
            raise Exception(
                "To use gevent websockets you must do 'pip install gevent gevent-websocket "
                "redis' and run gunicorn with the GeventWebSocketWorker.")


__all__ = ["HypergenWebsocketConsumer", "ws_url", "group_send", "GroupRegistry", "ws_view"]

# ----------------------------------------------------------------------------
# Group registry + Redis pub/sub bridge (replaces channels' channel layer)
# ----------------------------------------------------------------------------

REDIS_PREFIX = "hypergen:ws:"


class GroupRegistry(object):
    """Per-process registry of live connections, with optional Redis fan-out.

    Mirrors the two things channels' channel layer gave us: group membership
    and message delivery.  Local delivery is a dict lookup; cross-process
    delivery is a Redis publish that every worker's subscriber greenlet picks
    up and replays onto its local members.
    """

    def __init__(self):
        # group_name -> set(consumer)
        self._groups = {}
        # channel_name -> consumer  (for point-to-point sends)
        self._channels = {}
        self._lock = RLock() if RLock is not None else None
        self._redis = None
        self._pubsub = None
        self._subscriber = None

    # -- membership ---------------------------------------------------------
    def add(self, consumer):
        with self._guard():
            self._channels[consumer.channel_name] = consumer

    def remove(self, consumer):
        with self._guard():
            self._channels.pop(consumer.channel_name, None)
            for members in self._groups.values():
                members.discard(consumer)

    def join(self, group_name, consumer):
        with self._guard():
            self._groups.setdefault(group_name, set()).add(consumer)
        self._ensure_redis_subscribed(group_name)

    def leave(self, group_name, consumer):
        with self._guard():
            members = self._groups.get(group_name)
            if members:
                members.discard(consumer)

    def _guard(self):
        # Context-manager that is a no-op if gevent is missing (assert_gevent
        # will already have raised in that case for real use).
        return self._lock if self._lock is not None else _NullCtx()

    # -- delivery -----------------------------------------------------------
    def channel_send(self, channel_name, event):
        """Point-to-point send. Tries local first, else publishes to Redis."""
        consumer = self._channels.get(channel_name)
        if consumer is not None:
            consumer.dispatch(event)
        elif self._redis is not None:
            self._redis.publish(REDIS_PREFIX + "chan:" + channel_name, json.dumps(event))

    def group_send(self, group_name, event):
        """Fan-out to a group across all workers (or locally if no Redis)."""
        if self._redis is not None:
            self._redis.publish(REDIS_PREFIX + "grp:" + group_name, json.dumps(event))
        else:
            self.local_group_send(group_name, event)

    def local_group_send(self, group_name, event):
        for consumer in list(self._groups.get(group_name, ())):
            consumer.dispatch(event)

    # -- redis bridge -------------------------------------------------------
    def _connect_redis(self):
        url = getattr(settings, "HYPERGEN_WS_REDIS_URL", None)
        if not url or self._redis is not None:
            return
        import redis
        self._redis = redis.Redis.from_url(url)
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        # We always listen for point-to-point sends targeting *any* of our
        # local channels via a single pattern subscription.
        self._pubsub.psubscribe(REDIS_PREFIX + "chan:*")
        self._subscriber = gevent.spawn(self._listen)

    def _ensure_redis_subscribed(self, group_name):
        self._connect_redis()
        if self._pubsub is not None:
            self._pubsub.subscribe(REDIS_PREFIX + "grp:" + group_name)

    def _listen(self):
        for message in self._pubsub.listen():
            try:
                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()
                event = json.loads(message["data"])
                if channel.startswith(REDIS_PREFIX + "grp:"):
                    self.local_group_send(channel[len(REDIS_PREFIX) + 4:], event)
                elif channel.startswith(REDIS_PREFIX + "chan:"):
                    consumer = self._channels.get(channel[len(REDIS_PREFIX) + 5:])
                    if consumer is not None:
                        consumer.dispatch(event)
            except Exception as e:  # never let the bus greenlet die
                print("hypergen ws bridge error:", e)


class _NullCtx(object):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# Process-global registry (one per gunicorn worker).
REGISTRY = GroupRegistry()


# ----------------------------------------------------------------------------
# Consumer
# ----------------------------------------------------------------------------

class HypergenWebsocketConsumer(object):
    """Gevent equivalent of the channels ``HypergenWebsocketConsumer``.

    One instance per connection.  ``serve()`` is the greenlet entrypoint and
    runs the receive loop; a second greenlet drains ``self.outbox`` and is the
    *only* writer to the socket (sockets are not safe for concurrent writes).
    """

    groups = None

    def __init__(self, request, ws, perm=None, any_perm=False):
        assert_gevent()
        self.request = request          # real WSGIRequest, .user already set
        self.ws = ws
        self.perm = perm
        self.any_perm = any_perm
        self.channel_name = uuid4().hex
        self.outbox = Queue()

    # -- lifecycle ----------------------------------------------------------
    def serve(self):
        """Blocking receive loop. Called by :func:`ws_view`."""
        REGISTRY.add(self)
        for group_name in (self.groups or []):
            REGISTRY.join(group_name, self)

        writer = gevent.spawn(self._writer)
        try:
            self.connect()
            while not self.ws.closed:
                raw = self.ws.receive()
                if raw is None:
                    break               # client closed
                self.receive_json(self.decode_json(raw))
        finally:
            writer.kill()
            REGISTRY.remove(self)

    def _writer(self):
        # Single writer greenlet: serializes all outbound frames.
        for payload in self.outbox:     # blocks until a frame is queued
            if self.ws.closed:
                break
            self.ws.send(payload)

    def connect(self):
        # Channels called super().connect() to accept; the gevent handshake is
        # already complete by the time we get here, so this is a hook only.
        pass

    # -- inbound ------------------------------------------------------------
    def receive_json(self, content):
        perms_ok, _, matched_perms = check_perms(self.get_request(), self.perm, any_perm=self.any_perm)
        if perms_ok is not True:
            self.send_permission_denied()
            return

        if type(content) is dict and "args" in content and "meta" in content:
            with context(request=self.get_request()), context(at="hypergen", matched_perms=matched_perms):
                self.receive_hypergen_callback(*content["args"])

    def receive_hypergen_callback(self, *args, **kwargs):
        raise NotImplementedError("Please implement your own receive_hypergen_callback() method.")

    def get_request(self):
        # Under WSGI we already have a fully-formed request with an
        # authenticated user; just mark the pseudo-method like channels did.
        self.request.method = "WS"
        return self.request

    def send_permission_denied(self):
        self.channel_send_hypergen_commands([["console.error", "Permission denied"]])

    # -- outbound -----------------------------------------------------------
    def channel_send(self, event):
        """Deliver an event to this connection (by channel name)."""
        REGISTRY.channel_send(self.channel_name, event)

    def group_send(self, group_name, event):
        REGISTRY.group_send(group_name, event)

    def group_send_hypergen_commands(self, group_name, commands):
        self.group_send(group_name,
            {'type': 'hypergen__send_hypergen_commands', 'commands': json.loads(self.encode_json(commands))})

    def channel_send_hypergen_commands(self, commands):
        self.channel_send({
            'type': 'hypergen__send_hypergen_commands', 'commands': json.loads(self.encode_json(commands))})

    def hypergen__send_hypergen_commands(self, event):
        # Enqueue for the writer greenlet; never touch self.ws directly here.
        self.outbox.put(self.encode_json(event['commands']))

    # -- dispatch -----------------------------------------------------------
    def dispatch(self, message):
        """Route a backend event to its type-named handler.

        Replaces channels' async ``dispatch`` + ``database_sync_to_async``.
        Called from the registry, possibly from the Redis-subscriber greenlet,
        so it runs the handler inside a hypergen ``context``.
        """
        handler = getattr(self, message["type"].replace(".", "__"), None)
        if handler is None:
            raise ValueError("No handler for message type %s" % message["type"])
        with context(request=self.get_request()), context(at="hypergen", matched_perms=[]):
            handler(message)

    # -- json ---------------------------------------------------------------
    @classmethod
    def decode_json(cls, text_data):
        return loads(text_data)

    @classmethod
    def encode_json(cls, content):
        return dumps(content)

    # -- back-compat routing shim ------------------------------------------
    @classmethod
    def as_asgi(cls, **initkwargs):
        """Drop-in for the channels ``as_asgi``; returns a Django ws view so
        existing ``path(..., Consumer.as_asgi(perm=...))`` lines keep working
        once they point at a normal ``urlpatterns`` list."""
        return ws_view(cls, **initkwargs)


# ----------------------------------------------------------------------------
# Routing helpers
# ----------------------------------------------------------------------------

def ws_view(consumer_cls, **initkwargs):
    """Wrap a consumer class as a normal Django view.

    Usage in urls.py::

        path('ws/chat/<slug:room_name>/', ws_view(ChatConsumer, perm=NO_PERM_REQUIRED))
    """

    @wraps(consumer_cls)
    def view(request, *args, **kwargs):
        ws = request.environ.get("wsgi.websocket")
        if ws is None:
            return HttpResponseBadRequest("Expected a WebSocket request.")
        consumer_cls(request, ws, **initkwargs).serve()
        # The handshake response was already hijacked by gevent-websocket.
        return HttpResponse()

    return view


# ----------------------------------------------------------------------------
# Module-level helpers (unchanged public API)
# ----------------------------------------------------------------------------

def ws_url(url):
    absolute_url = context.request.build_absolute_uri(url)
    if settings.DEBUG:
        return absolute_url.replace("http://", "ws://").replace("https://", "wss://")
    else:
        return context.request.build_absolute_uri(url).replace("http://", "wss://").replace("https://", "wss://")


def group_send(group_name, event):
    """Send an event to a group from anywhere (e.g. an HTTP view), the gevent
    analogue of the channels ``get_channel_layer().group_send``."""
    REGISTRY.group_send(group_name, event)
