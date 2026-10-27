"""Undo for the "Poista" buttons: a removal is recorded as what it changed in
the session (each value and dict entry it dropped or replaced, each set
element it added or removed), and "Kumoa" puts exactly that back. What was
changed elsewhere since stays as it is. A new drop ends the undo: the cards
it made would no longer match."""

from __future__ import annotations

from view.ui import _BUTTONS, st

_UNDO = "ui_undo"           # {"label", "changes", "inbox"}
_INBOX = "dxf_inbox"        # bumped by every drop (see ``view.dxf_files``)
_MISSING = object()


def _copy(value):
    """A copy one level deep: what a removal pops from or adds to is kept."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (set, list)):
        return type(value)(value)
    return value


def _ours(key) -> bool:
    return isinstance(key, str) and key != _UNDO and not key.startswith(_BUTTONS)


def _diff(before: dict, ss) -> list[tuple]:
    """What changed since ``before``, as the steps that undo it."""
    steps = []
    for key, old in before.items():
        new = ss[key] if key in ss else _MISSING
        if new is _MISSING:
            steps.append(("value", key, old))
        elif isinstance(old, dict) and isinstance(new, dict):
            changed = [("entry", key, k, v) for k, v in old.items()
                       if k not in new or new[k] is not v]
            changed += [("drop_entry", key, k) for k in new if k not in old]
            if changed:     # undone last to first: the order is put back last
                steps += [("order", key, list(old)), *changed]
        elif isinstance(old, set) and isinstance(new, set):
            if old - new or new - old:
                steps.append(("elements", key, old - new, new - old))
        elif new is not old and not (isinstance(old, list) and old == new):
            steps.append(("value", key, old))
    steps += [("drop", key) for key in list(ss.keys()) if _ours(key) and key not in before]
    return steps


def undoable(fn, label: str):
    """``fn`` (a button's callback) recorded so that "Kumoa" undoes it."""
    def run(*args, **kwargs):
        ss = st.session_state
        before = {k: _copy(ss[k]) for k in list(ss.keys()) if _ours(k)}
        fn(*args, **kwargs)
        ss[_UNDO] = {"label": label, "changes": _diff(before, ss),
                     "inbox": ss.get(_INBOX, 0)}
    return run


def _undo() -> None:
    ss = st.session_state
    record = ss.pop(_UNDO, None)
    for step in reversed(record["changes"] if record else []):
        kind, key = step[0], step[1]
        if kind == "value":
            ss[key] = step[2]
        elif kind == "entry":
            ss.setdefault(key, {})[step[2]] = step[3]
        elif kind == "drop_entry":
            if isinstance(ss.get(key), dict):
                ss[key].pop(step[2], None)
        elif kind == "order":
            now = ss.get(key)
            if isinstance(now, dict):
                rank = {k: i for i, k in enumerate(step[2])}
                ss[key] = dict(sorted(now.items(), key=lambda kv: rank.get(kv[0], len(rank))))
        elif kind == "elements":
            now = ss.setdefault(key, set())
            now |= step[2]
            now -= step[3]
        elif kind == "drop":
            ss.pop(key, None)


def _dismiss() -> None:
    st.session_state.pop(_UNDO, None)


def render_undo() -> None:
    """The last removal, with "Kumoa", until it is dismissed or files are
    dropped again."""
    ss = st.session_state
    record = ss.get(_UNDO)
    if record is None:
        return
    if record["inbox"] != ss.get(_INBOX, 0):
        ss.pop(_UNDO)
        return
    with st.container(border=True):
        text, undo, close = st.columns([6, 1, 1], vertical_alignment="center")
        text.markdown(f"Poistettu: **{record['label']}**")
        undo.button("Kumoa", key="ui_undo_do", type="primary", on_click=_undo,
                    width="stretch")
        close.button("Sulje", key="ui_undo_close", on_click=_dismiss, width="stretch")
