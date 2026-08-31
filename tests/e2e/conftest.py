"""Self-contained environment for the E2E suite: a temp SQLite DB, real
backend/frontend subprocesses, auto-provisioned accounts, and a reshaped
model catalog so the wizard's automatic model selection resolves to
fake-architect: -- see
docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md."""
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest

from ._env import API_URL, BASE_URL, DEMO, FAKE_ARCHITECT_SPEC, OP

REPO_ROOT = Path(__file__).resolve().parents[2]

# On Windows, getpass.getpass() reads directly from the console (msvcrt),
# ignoring a piped stdin entirely -- it only falls back to reading stdin
# when `sys.stdin is not sys.__stdin__`, which a bare `-m` invocation never
# triggers (both are bound to the same object at interpreter startup). This
# shim runs before admin.main() and rebinds sys.stdin to a fresh wrapper
# around the same underlying pipe, which *is* a different object, so
# getpass's fallback path (a plain stdin read) engages instead of hanging
# forever waiting for real keystrokes. Only needed for the create-user
# subcommand below, which is the only one that prompts for a password.
_ADMIN_CLI_STDIN_SAFE = (
    "import sys, io; "
    "sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding=sys.stdin.encoding); "
    "from ui.backend.admin import main; sys.exit(main())"
)

# Every subprocess started below pays the full `import bestteam` cost before it
# does any work -- ~5s in the documented dev install, but ~26s in a venv that
# also has the `tools-rerank` extra, because sentence-transformers puts
# transformers and torch on the path and langchain_core.language_models.base
# imports transformers eagerly at module scope. At the old 30s budget such a
# venv had ~2s of headroom, so provisioning died under any load with a bare
# TimeoutExpired that named neither the import nor the extra. These guard
# against a hung getpass prompt, not against a slow interpreter startup.
_IMPORT_HEAVY_TIMEOUT = 120


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate proc and any processes it spawned.

    On Windows, `npm run dev` runs via npm.CMD, a wrapper batch script --
    Popen.terminate() only kills that wrapper, orphaning the actual
    vite/node process it launches (confirmed by hand: it's left running and
    still bound to the port after teardown). taskkill's /T flag walks the
    OS-tracked parent-child tree instead of relying on the immediate child
    to propagate the signal."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.terminate()


def _assert_port_free(host: str, port: int) -> None:
    """Raise a clear error if something is already listening on host:port.

    A successful connect means a real process -- possibly a developer's own
    dev stack, started per the local workflow documented in the root
    CLAUDE.md, which uses these same ports -- is already bound there.
    Spawning our own server on top of it wouldn't fail: `_wait_healthy`
    would silently attach to that pre-existing process instead of ours,
    and this fixture would go on to destructively reshape its real model
    catalog and mutate its real database."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        in_use = sock.connect_ex((host, port)) == 0
    finally:
        sock.close()
    if in_use:
        raise RuntimeError(
            f"Port {port} on {host} is already in use -- refusing to start "
            "the E2E backend/frontend on top of a possibly-running dev "
            "stack. Stop whatever is listening on "
            f"{host}:{port} (e.g. your own `uvicorn`/`npm run dev`) and "
            "re-run the E2E suite."
        )


def _wait_healthy(url: str, proc: subprocess.Popen, log_path: Path, timeout: float = _IMPORT_HEAVY_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_text = log_path.read_text(errors="replace") if log_path.exists() else "<no log captured>"
            raise RuntimeError(
                f"process for {url} exited early (code {proc.returncode}) "
                f"before becoming healthy -- output:\n{log_text}"
            )
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except (urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"{url} did not become healthy in {timeout}s: {last_error}")


def _provision_user(db_path: str, username: str, password: str, *, org: str | None, platform: bool) -> None:
    args = [sys.executable, "-c", _ADMIN_CLI_STDIN_SAFE, "create-user", username]
    args += ["--platform"] if platform else ["--org", org or "default"]
    env = {**os.environ, "BESTTEAM_DB_PATH": db_path}
    result = subprocess.run(
        args, cwd=str(REPO_ROOT), env=env, input=f"{password}\n{password}\n",
        capture_output=True, text=True, timeout=_IMPORT_HEAVY_TIMEOUT,
    )
    assert result.returncode == 0, f"provisioning {username} failed:\n{result.stdout}\n{result.stderr}"


def _promote_to_admin(db_path: str, username: str) -> None:
    env = {**os.environ, "BESTTEAM_DB_PATH": db_path}
    result = subprocess.run(
        [sys.executable, "-m", "ui.backend.admin", "promote", username],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=_IMPORT_HEAVY_TIMEOUT,
    )
    assert result.returncode == 0, f"promoting {username} failed:\n{result.stdout}\n{result.stderr}"


def _reshape_model_catalog() -> None:
    """Delete every auto-seeded non-fake: catalog entry and add
    fake-architect:e2e, so the wizard's pickDefaultModel() resolves to it
    automatically (see the design doc's "Fake-architect mechanism")."""
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        login = client.post("/api/auth/login", json={"username": OP[0], "password": OP[1]})
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        entries = client.get("/api/config/model-catalog", headers=headers).json()
        for entry in entries:
            if not entry["spec"].startswith("fake:"):
                resp = client.delete(f"/api/config/model-catalog/{entry['spec']}", headers=headers)
                assert resp.status_code == 204, resp.text

        resp = client.put(
            f"/api/config/model-catalog/{FAKE_ARCHITECT_SPEC}",
            headers=headers,
            json={
                "display_name": "E2E Test Architect (fake, $0)",
                "description": "Deterministic fake architect for automated E2E tests only.",
                "tier": "fast",
                "input_price_per_1k": 0.0,
                "output_price_per_1k": 0.0,
            },
        )
        assert resp.status_code == 200, resp.text


@pytest.fixture(scope="session", autouse=True)
def e2e_backend():
    _assert_port_free("127.0.0.1", 8000)
    _assert_port_free("localhost", 5173)

    tmp_dir = tempfile.mkdtemp(prefix="bestteam_e2e_")
    db_path = str(Path(tmp_dir) / "e2e.db")
    secret = "e2e-test-secret-" + secrets.token_hex(16)

    _provision_user(db_path, DEMO[0], DEMO[1], org="default", platform=False)
    _provision_user(db_path, OP[0], OP[1], org=None, platform=True)
    _promote_to_admin(db_path, OP[0])

    env = {
        **os.environ,
        "BESTTEAM_DB_PATH": db_path,
        "BESTTEAM_SECRET_KEY": secret,
        "BESTTEAM_DEMO_PIPELINES": "1",
        # Neutralize every BESTTEAM_* var that could otherwise route this
        # supposedly $0, side-effect-free E2E run at a real external
        # service or a real persistent store outside tmp_dir, regardless
        # of what's already set in the invoking developer's shell.
        "BESTTEAM_MEMORY_DB": "",
        "BESTTEAM_MEMORY_MODEL": "",
        "BESTTEAM_MEMORY_EMBEDDING_MODEL": "",
        "BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL": "",
        "BESTTEAM_MEMORY_RERANK_MODEL": "",
        "BESTTEAM_EMAIL_BACKEND": "",
    }

    npm = shutil.which("npm")
    assert npm is not None, "npm not found on PATH -- required to start the frontend dev server"

    backend_log = Path(tmp_dir) / "backend.log"
    frontend_log = Path(tmp_dir) / "frontend.log"
    backend = None
    frontend = None

    try:
        with open(backend_log, "w") as f:
            backend = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "ui.backend.main:app", "--port", "8000", "--host", "127.0.0.1"],
                cwd=str(REPO_ROOT), env=env, stdout=f, stderr=subprocess.STDOUT,
            )
        with open(frontend_log, "w") as f:
            frontend = subprocess.Popen(
                # --strictPort: fail instead of silently auto-incrementing
                # to 5174 if 5173 is taken, which would leave BASE_URL
                # pointing at the wrong server.
                [npm, "run", "dev", "--", "--port", "5173", "--strictPort"],
                cwd=str(REPO_ROOT / "ui" / "frontend"), env=env, stdout=f, stderr=subprocess.STDOUT,
            )

        _wait_healthy(f"{API_URL}/api/health", backend, backend_log)
        _wait_healthy(BASE_URL, frontend, frontend_log)
        _reshape_model_catalog()
        yield
    finally:
        if backend is not None:
            _kill_process_tree(backend)
            backend.wait(timeout=10)
        if frontend is not None:
            _kill_process_tree(frontend)
            frontend.wait(timeout=10)
        shutil.rmtree(tmp_dir, ignore_errors=True)
