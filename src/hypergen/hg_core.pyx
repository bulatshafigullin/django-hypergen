# cython: language_level=3, boundscheck=False, wraparound=False
"""
Experimental Cython hypergen core.

Architectural differences from django-hypergen:
  - No base_element Python objects: elements write directly to a thread-local buffer
  - No pyrsistent/pmap: replaced by a plain thread-local list stack
  - No contextlist: the buffer is just a cdef-accessible list
  - Block elements (tr, div, ...) are cdef classes used as context managers
  - Leaf elements (td, span, ...) are cdef functions — zero Python object creation
  - Escape and attribute rendering are cdef str functions
"""

import threading
from html import escape as _html_escape

_tl = threading.local()

# ---------------------------------------------------------------------------
# Thread-local buffer stack
# ---------------------------------------------------------------------------

cdef list _buf():
    return _tl._hg_buf

cdef list _push():
    cdef list b = []
    _tl._hg_buf = b
    _tl._hg_stack.append(b)
    return b

cdef list _pop():
    _tl._hg_stack.pop()
    cdef list prev = _tl._hg_stack[len(_tl._hg_stack) - 1] if _tl._hg_stack else None
    _tl._hg_buf = prev
    return prev

# ---------------------------------------------------------------------------
# Escape
# ---------------------------------------------------------------------------

cdef str _esc(str s):
    cdef Py_UCS4 ch
    for ch in s:
        if ch == '&' or ch == '<' or ch == '>' or ch == '"' or ch == '\'':
            return _html_escape(s, quote=True)
    return s

# ---------------------------------------------------------------------------
# Attribute rendering
# ---------------------------------------------------------------------------

cdef str _attrs(dict kw):
    if not kw:
        return ''
    cdef list parts = []
    cdef str k
    for k, v in kw.items():
        if len(k) > 0 and k[len(k) - 1] == '_':
            k = k[:len(k) - 1]
        parts.append(' ')
        parts.append(k)
        parts.append('="')
        parts.append(_esc(str(v)))
        parts.append('"')
    return ''.join(parts)

# ---------------------------------------------------------------------------
# Block element: writes <tag attrs> on enter, </tag> on exit
# ---------------------------------------------------------------------------

cdef class _Block:
    cdef str _close

    def __cinit__(self, str tag, object content, dict kw):
        self._close = '</' + tag + '>'
        cdef list buf = _buf()
        buf.append('<')
        buf.append(tag)
        if kw:
            buf.append(_attrs(kw))
        buf.append('>')
        if content is not None:
            buf.append(_esc(str(content)))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        _buf().append(self._close)

# ---------------------------------------------------------------------------
# Leaf element: writes <tag attrs>content</tag> immediately
# ---------------------------------------------------------------------------

cdef _leaf(str tag, object content, dict kw):
    cdef list buf = _buf()
    buf.append('<')
    buf.append(tag)
    if kw:
        buf.append(_attrs(kw))
    buf.append('>')
    if content is not None:
        buf.append(_esc(str(content)))
    buf.append('</' + tag + '>')

# ---------------------------------------------------------------------------
# Public element API  (block = context manager, leaf = immediate write)
# ---------------------------------------------------------------------------

# Elements are dual-mode:
#   td("value", class_="x")  → leaf: writes <td class="x">value</td> immediately
#   with td(class_="x"): ... → block: context manager, closes on exit
# Detected by whether content is provided (sentinel _NONE means "not passed").

cdef object _NONE = object()

cdef _el(str tag, object content, dict kw):
    if content is _NONE:
        return _Block(tag, None, kw)
    _leaf(tag, content, kw)

def table(content=_NONE, **kw): return _Block('table', None if content is _NONE else content, kw)
def tbody(content=_NONE, **kw): return _Block('tbody', None if content is _NONE else content, kw)
def tr(content=_NONE, **kw):    return _Block('tr',    None if content is _NONE else content, kw)
def div(content=_NONE, **kw):   return _Block('div',   None if content is _NONE else content, kw)
def ul(content=_NONE, **kw):    return _Block('ul',    None if content is _NONE else content, kw)
def td(content=_NONE, **kw):    return _el('td',   content, kw)
def th(content=_NONE, **kw):    return _el('th',   content, kw)
def li(content=_NONE, **kw):    return _el('li',   content, kw)
def span(content=_NONE, **kw):  return _el('span', content, kw)
def a(content=_NONE, **kw):     return _el('a',    content, kw)
def p(content=_NONE, **kw):     return _el('p',    content, kw)

def t(content):
    """Write escaped text node."""
    _buf().append(_esc(str(content)))

def raw(content):
    """Write raw unescaped HTML."""
    _buf().append(str(content))

# ---------------------------------------------------------------------------
# Render context
# ---------------------------------------------------------------------------

def render(fn, *args, **kwargs):
    """
    Call fn(*args, **kwargs) with a fresh buffer, return the captured HTML.
    Nestable: inner render() calls get their own buffer scope.
    """
    if not hasattr(_tl, '_hg_stack'):
        _tl._hg_stack = []
    buf = _push()
    try:
        fn(*args, **kwargs)
        return ''.join(buf)
    finally:
        _pop()
