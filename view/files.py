"""The quote's files, in the session: every dropped file (and every file in a
dropped ZIP) as an ``InputFile``, and what its reader made of it, a
``ReadResult`` (see ``core.model`` and ``core.readers``). Everything after
the drop reads files through here.

A DXF or DWG is read when dropped (the cards need its part at once), and so
is a PDF, under the drop's progress bar (the cards need its text); a STEP
model through ``view.bom``, which unfolds it with the K factor."""

from __future__ import annotations

from core.model import InputFile, ReadResult
from core.readers import read_file
from view.ui import st

INPUTS = "inputs"       # {file id: InputFile}, in drop order
RESULTS = "results"     # {file id: ReadResult}, each file read once
KEYS = (INPUTS, RESULTS)


def inputs() -> dict[str, InputFile]:
    return st.session_state.setdefault(INPUTS, {})


def get(file_id: str) -> InputFile | None:
    return inputs().get(file_id)


def add(f: InputFile) -> None:
    f.order = len(inputs())
    inputs()[f.id] = f


def of_kind(*kinds: str) -> dict[str, InputFile]:
    return {fid: f for fid, f in inputs().items() if f.kind in kinds}


def result(file_id: str) -> ReadResult | None:
    """The file's ``ReadResult``, read the first time it is asked for (None
    for a file not dropped)."""
    results = st.session_state.setdefault(RESULTS, {})
    if file_id not in results:
        f = get(file_id)
        if f is None or f.load is None:
            return None
        results[file_id] = read_file(f)
    return results[file_id]


def results() -> dict[str, ReadResult]:
    """Every file read so far, ``{file id: ReadResult}``, in reading order."""
    return st.session_state.get(RESULTS, {})


def is_read(file_id: str) -> bool:
    return file_id in st.session_state.get(RESULTS, {})


def keep(file_id: str, res: ReadResult) -> None:
    """A result read elsewhere (``view.bom`` reads STEP models with its K
    factor)."""
    st.session_state.setdefault(RESULTS, {})[file_id] = res


def forget(file_id: str) -> None:
    """Drop a file's result (it is read again when asked)."""
    st.session_state.get(RESULTS, {}).pop(file_id, None)


def remove(file_id: str) -> None:
    inputs().pop(file_id, None)
    forget(file_id)


def folder(file_id: str) -> str:
    f = get(file_id.split("|", 1)[0])
    return f.folder if f is not None else ""
