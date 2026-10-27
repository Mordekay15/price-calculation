"""``st`` for code that can also run without drawing ("headless").

A big RFQ has hundreds of part cards. Only one page of them is drawn, but
every card is still priced, by running the same card code headless: inside
``headless()`` a widget draws nothing and returns the value it would have
(its value in the session, or its default), and every other element is
skipped. Modules whose code runs on a card import ``st`` from here instead of
``streamlit``; outside ``headless()`` it is plain Streamlit.

The switch is a context variable, so another session running at the same
time (another thread) is not affected."""

from __future__ import annotations

import contextlib
import contextvars
import re
from types import SimpleNamespace

import streamlit

_HEADLESS = contextvars.ContextVar("headless", default=False)

# Not drawing: these are used the same way headless or not.
_PASS = {"session_state", "cache_data", "cache_resource", "column_config", "secrets",
         "query_params", "context", "runtime"}


@contextlib.contextmanager
def headless(on: bool = True):
    """Run without drawing; ``on`` False: as before (headless or not)."""
    token = _HEADLESS.set(on or _HEADLESS.get())
    try:
        yield
    finally:
        _HEADLESS.reset(token)


def is_headless() -> bool:
    return _HEADLESS.get()


class _Null:
    """A container, column or element that draws nothing. Its widgets
    return what Streamlit would."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __bool__(self):
        return True

    def __iter__(self):
        return iter(())

    def __getattr__(self, name):
        if name in _PASS:
            return getattr(streamlit, name)
        return getattr(_WIDGETS, name, _nothing)

    # Containers.
    def columns(self, spec, *args, **kwargs):
        return [_NULL] * (spec if isinstance(spec, int) else len(spec))

    def tabs(self, labels, *args, **kwargs):
        return [_NULL] * len(labels)


def _nothing(*args, **kwargs):
    return _NULL


def _state(key, default):
    ss = streamlit.session_state
    return ss[key] if key is not None and key in ss else default


class _WIDGETS:
    """Each widget's value without drawing it, keyed like Streamlit's."""

    @staticmethod
    def selectbox(label, options, index=0, *args, key=None, **kwargs):
        options = list(options)
        default = options[index] if options and index is not None else None
        return _state(key, default)

    radio = selectbox

    @staticmethod
    def multiselect(label, options, default=None, *args, key=None, **kwargs):
        return _state(key, list(default or []))

    @staticmethod
    def number_input(label, min_value=None, max_value=None, value="min", *args, key=None,
                     **kwargs):
        if isinstance(value, str):          # "min": Streamlit starts at min_value or 0
            value = min_value if min_value is not None else 0.0
        return _state(key, value)

    @staticmethod
    def checkbox(label, value=False, *args, key=None, **kwargs):
        return _state(key, value)

    toggle = checkbox

    @staticmethod
    def text_input(label, value="", *args, key=None, **kwargs):
        return _state(key, value)

    text_area = text_input

    @staticmethod
    def segmented_control(label, options, *args, default=None, key=None, **kwargs):
        return _state(key, default)

    @staticmethod
    def dataframe(data, *args, key=None, **kwargs):
        rows = (_state(key, None) or {}).get("selection", {}).get("rows", [])
        return SimpleNamespace(selection=SimpleNamespace(rows=list(rows)))

    @staticmethod
    def data_editor(data, *args, **kwargs):
        return data

    @staticmethod
    def button(*args, **kwargs):
        return False

    download_button = button

    @staticmethod
    def file_uploader(*args, **kwargs):
        return None


_NULL = _Null()


class _St:
    """Streamlit, or ``_NULL`` inside ``headless()``."""

    def __getattr__(self, name):
        return getattr(_NULL if _HEADLESS.get() else streamlit, name)


st = _St()


# Buttons, downloads and editors: their value can't be set through the session.
_BUTTONS = ("dxf_del_", "dxf_fill_yes_", "dxf_fill_no_", "dxf_doc_dl_", "dxf_read_waiting",
            "dxf_step_del_", "dxf_doc_del_", "dxf_renest::", "dxf_sparrow_run",
            "dxf_sparrow_update", "dxf_uploader_", "asm_del_",
            "asm_bought_", "quote_pdf_dxf", "quote_xlsx_", "quote_dxf_zip_", "ui_")


def keep(owned) -> None:
    """Keep the values of widgets that may not be drawn, or be drawn anew:
    Streamlit forgets a widget's value when the widget isn't drawn, and the
    browser starts a widget drawn again from its default. Setting the value
    through the session, before the widget is drawn, keeps it for both.
    ``owned(key)`` picks them."""
    ss = streamlit.session_state
    for key in list(ss.keys()):
        if isinstance(key, str) and not key.startswith(_BUTTONS) and owned(key):
            ss[key] = ss[key]


def ids_in(ids) -> re.Pattern:
    """Matches a key with one of ``ids`` in it: a card's widget keys end in
    its id, or have it before "_"."""
    if not ids:
        return re.compile(r"(?!)")
    return re.compile("(?:" + "|".join(map(re.escape, sorted(ids, key=len, reverse=True)))
                      + ")(?:_|$)")
