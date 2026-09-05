"""Proof that the backend answering on the fixed E2E port is the one this
fixture started -- see the module docstring of tests/test_e2e_harness_guard.py
for the incident that motivated it.

`_assert_port_free` samples port 8000 once, more than a minute before
`_reshape_model_catalog` issues its DELETEs, and `_wait_healthy` returns on
the first successful /api/health without asking whose it is. So the reshape
needs its own answer to "is this server mine?", taken at the moment it
matters. The answer here is out-of-band: write a random spec through the API,
then look for it by reading the fixture's own temp database directly. Only a
backend serving that file can make the two agree.
"""

import sqlite3
from pathlib import Path

from ._env import API_URL


def assert_backend_is_ours(probe_spec: str, db_path: str) -> None:
    """Raise unless `probe_spec`, just written through the API, is visible in
    the fixture's own database at `db_path`.

    Fails closed: a database that cannot be read at all is not a database we
    have shown ownership of, so it is refused just like a mismatch.
    """
    def refuse(why: str) -> "RuntimeError":
        return RuntimeError(
            f"refusing to reshape the model catalog: {why}. Something other than "
            f"this fixture's backend is answering on {API_URL} -- most likely a "
            "dev stack that bound the port after the free-port check, or a "
            "leftover harness server from an earlier run. Its catalog would have "
            "been destroyed. Stop whatever is listening there and re-run."
        )

    path = Path(db_path)
    if not path.exists():
        raise refuse(f"the fixture's own database {path} does not exist")

    uri = "file:" + path.as_uri()[len("file:"):] + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            found = con.execute(
                "SELECT 1 FROM model_catalog WHERE spec = ?", (probe_spec,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise refuse(f"could not read the model catalog from {path}: {exc}") from exc

    if found is None:
        raise refuse(f"the probe entry {probe_spec!r} is not in {path}")
