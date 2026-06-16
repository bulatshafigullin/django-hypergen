# Replacing django-channels with gevent websockets

## 1. Where channels is used today

Only **one library file** touches channels: `src/hypergen/websocket.py`. Everything else is
example/infra code.

| Concern | Current (channels/ASGI) | File |
|---|---|---|
| Consumer base | `JsonWebsocketConsumer` | `src/hypergen/websocket.py:14,40` |
| Routing | `ProtocolTypeRouter` + `URLRouter`, `.as_asgi(perm=...)` | `examples/asgi.py`, `examples/*/routing.py` |
| Auth | `AuthMiddlewareStack`, `scope["user"]`, `ASGIRequest(scope)` | `websocket.py:68-73`, `examples/asgi.py:17` |
| Per-conn send | `channel_send` → `channel_layer.send` | `websocket.py:78-79` |
| Group send (pub/sub) | `channel_layer.group_send`, `get_channel_layer()` | `websocket.py:81-82,123-125` |
| Auto group join | `groups = [...]` class attr | `examples/websockets/consumers.py:7` |
| Cross-process bus | `channels_redis.RedisChannelLayer` | `examples/settings.py:130` |
| Async glue | `async_to_sync`, `database_sync_to_async`, `dispatch` | `websocket.py:12,16,95-106` |
| Server | `daphne ... asgi:application` | `Dockerfile:12`, `entrypoint.sh` |

**Client is protocol-agnostic.** `websocket.js` uses plain browser `WebSocket` (via Sockette):
sends `{args, meta}` JSON, receives a JSON command list → `hypergen.applyCommands`. **No client
change required** — gevent-websocket speaks the same RFC6455 wire protocol on the same `ws://`
URLs.

## 2. What channels actually gives us (and must be re-built)

1. **WS handshake + framing** → gevent-websocket provides (`environ['wsgi.websocket']`).
2. **URL routing for ws/** → replace ASGI URLRouter with normal Django `urls.py` + WSGI dispatch.
3. **Auth on the socket** → *easier* under WSGI: a real `WSGIRequest` with session/user already
   resolved by Django auth middleware. No `ASGIRequest(scope)` reconstruction.
4. **Groups + cross-process delivery (the hard part)** → channels' channel layer (Redis) handles
   group membership *and* fan-out across worker processes/hosts. gevent-websocket gives only the
   raw per-connection socket. We must build:
   - in-process registry: `group -> set(connection)`,
   - per-connection outbound `gevent.queue.Queue` + a single writer greenlet (sockets are not
     safe for concurrent writes),
   - a Redis pub/sub bridge so `group_send` reaches connections on *other* workers.

## 3. Target architecture (gunicorn + gevent-websocket)

```
gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker wsgi:application
```

- `wsgi.py`: `from gevent import monkey; monkey.patch_all()` **first**, then build Django WSGI app.
  (Postgres needs `psycogreen.gevent.patch_psycopg()`; sqlite in examples is fine.)
- Each WS connection runs in its own greenlet, blocking on `ws.receive()`.
- A second greenlet per connection drains the outbound queue → `ws.send()` (the only writer).
- One module-global `GroupRegistry` per process + one Redis-subscriber greenlet per process that
  republishes incoming group messages onto local connections' queues.

## 4. Concrete code changes

### 4.1 Rewrite `src/hypergen/websocket.py`
Keep the **public API identical** so example consumers barely change:
`HypergenWebsocketConsumer`, `receive_hypergen_callback`, `channel_send_hypergen_commands`,
`group_send_hypergen_commands`, `groups`, `ws_url`, module-level `group_send`, `encode_json`.

Replace the channels base with a plain class driven by a handler view:

```python
# gevent stack
from gevent.queue import Queue
import gevent, json
from geventwebsocket.websocket import WebSocket

class HypergenWebsocketConsumer:
    groups = None

    def __init__(self, request, ws, perm=None, any_perm=False):
        self.request = request          # real WSGIRequest, request.user already set
        self.ws = ws
        self.perm = perm
        self.any_perm = any_perm
        self.outbox = Queue()
        self.channel_name = uuid4().hex

    # ---- lifecycle (called by the dispatch view) ----
    def serve(self):
        REGISTRY.add(self.channel_name, self)
        for g in (self.groups or []):
            REGISTRY.join(g, self)
        writer = gevent.spawn(self._writer)
        try:
            while not self.ws.closed:
                msg = self.ws.receive()
                if msg is None:
                    break
                self.receive_json(self.decode_json(msg))
        finally:
            writer.kill()
            REGISTRY.remove(self)

    def _writer(self):
        for payload in self.outbox:        # blocks until something queued
            self.ws.send(payload)

    # ---- unchanged business logic ----
    def receive_json(self, content):
        perms_ok, _, matched = check_perms(self.get_request(), self.perm, any_perm=self.any_perm)
        if perms_ok is not True:
            return self.send_permission_denied()
        if isinstance(content, dict) and "args" in content and "meta" in content:
            with context(request=self.get_request()), context(at="hypergen", matched_perms=matched):
                self.receive_hypergen_callback(*content["args"])

    def get_request(self):
        self.request.method = "WS"
        return self.request                # no ASGIRequest dance needed

    # ---- send paths: enqueue locally, fan out via registry/redis ----
    def channel_send_hypergen_commands(self, commands):
        self.outbox.put(self.encode_json(commands))

    def group_send_hypergen_commands(self, group_name, commands):
        group_send(group_name, {"type": "hypergen__send_hypergen_commands",
                                "commands": json.loads(self.encode_json(commands))})

    def hypergen__send_hypergen_commands(self, event):
        self.outbox.put(self.encode_json(event["commands"]))
    ...
```

### 4.2 New `GroupRegistry` + Redis bridge (new file `src/hypergen/ws_groups.py`)
- `add/remove/join/leave`, `local_group_send(group, event)` → put on each member's `outbox`.
- `group_send(group, event)` → `redis.publish("hypergen:"+group, json.dumps(event))`.
- One subscriber greenlet per process subscribes to `hypergen:*`, and on message calls the
  named handler (`event["type"]`) on each local member — replicating channels' type-dispatch.
- Single-process / dev fallback: if no Redis configured, `group_send` calls `local_group_send`
  directly (drop-in for `InMemoryChannelLayer`).

### 4.3 Dispatch view + routing (replaces `asgi.py` + `*/routing.py`)
A tiny view reads the socket off the WSGI environ and hands to the consumer:

```python
def ws_view(consumer_cls, **initkwargs):
    def view(request, *args, **kw):
        ws = request.environ.get("wsgi.websocket")
        if ws is None:
            return HttpResponseBadRequest("Expected WebSocket")
        consumer_cls(request, ws, **initkwargs).serve()
        return HttpResponse()             # handshake already hijacked
    return view
```

Routing becomes ordinary Django urls:
```python
# was: path('ws/chat/<slug:room_name>/', ChatConsumer.as_asgi(perm=NO_PERM_REQUIRED))
path('ws/chat/<slug:room_name>/', ws_view(ChatConsumer, perm=NO_PERM_REQUIRED))
```
Provide a back-compat `classmethod as_asgi(cls, **kw)` that returns `ws_view(cls, **kw)` so
existing routing lines keep working.

### 4.4 Infra / config
- `wsgi.py`: monkey-patch at top (already exists at `examples/wsgi.py` — add patching).
- `Dockerfile:12` / `entrypoint.sh`: `daphne ... asgi:application`
  → `gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 4 -b 0.0.0.0:8000 wsgi:application`.
- `examples/settings.py`: drop `daphne` from `INSTALLED_APPS`, drop `ASGI_APPLICATION` &
  `CHANNEL_LAYERS`; add a `HYPERGEN_WS_REDIS_URL` setting for the bridge.
- requirements: drop `channels`, `channels-redis`, `daphne`, `Twisted`, `autobahn`, `asgiref`
  (if unused elsewhere); add `gevent`, `gevent-websocket`, `gunicorn` (already present),
  `redis`, `psycogreen` (if Postgres).

## 5. Risks / sharp edges

- **Concurrent writes**: enforce single writer greenlet per socket (the `outbox` pattern). Calling
  `ws.send` from both request handling and a group push without it will corrupt frames.
- **Blocking calls under gevent**: any C-extension that isn't greenlet-friendly (some DB drivers,
  `numpy`-heavy CPU work in `gameofcython`) will block the whole worker. CPU-bound consumers
  should stay on threads or separate workers.
- **Worker model**: gunicorn gevent workers are per-process; group fan-out across workers *requires*
  the Redis bridge — in-process registry alone silently drops cross-worker messages.
- **`database_sync_to_async`/`async_to_sync` removed**: all consumer code becomes plain sync — good,
  but audit for anything that relied on async behavior (none found in this repo).
- **Auth**: confirm session cookie is sent on the WS handshake (same-origin) so Django auth
  middleware populates `request.user`; channels' `AllowedHostsOriginValidator` → replace with a
  manual `Origin` check in `ws_view` if you need CSRF/origin protection.
- **Graceful shutdown / reconnect**: client already retries (Sockette `maxAttempts`), so worker
  restarts are tolerated.

## 6. Effort estimate

- Core library rewrite (`websocket.py` + `ws_groups.py`): ~1 day.
- Infra/settings/requirements + example routing edits: ~0.5 day.
- Test against `examples/websockets` (group chat + backend push) and `examples/features` (snake,
  per-connection state): ~0.5–1 day. These two examples exercise both the group/pub-sub path and
  the per-connection-state path, so they are the full acceptance test.

## 7. Recommendation

Feasible and arguably *simpler* than channels for this library: the consumer API is small, the
client needs no changes, and WSGI auth removes the `ASGIRequest`/scope plumbing. The only real
engineering is re-implementing the channel-layer group bus on Redis pub/sub. Keep the public
consumer API and an `as_asgi`-named shim so downstream users migrate by swapping the server command
and requirements, not their consumer code.
