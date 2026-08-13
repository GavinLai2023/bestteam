"""Self-contained environment for the E2E suite: a temp SQLite DB, real
backend/frontend subprocesses, auto-provisioned accounts, and a reshaped
model catalog so the wizard's automatic model selection resolves to
fake-architect: -- see
docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md."""
import os
import secrets
import shutil
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


def _wait_healthy(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
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
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"provisioning {username} failed:\n{result.stdout}\n{result.stderr}"


def _promote_to_admin(db_path: str, username: str) -> None:
    env = {**os.environ, "BESTTEAM_DB_PATH": db_path}
    result = subprocess.run(
        [sys.executable, "-m", "ui.backend.admin", "promote", username],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=30,
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
        "BESTTEAM_DEMO_WORKFLOWS": "1",
    }

    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ui.backend.main:app", "--port", "8000", "--host", "127.0.0.1"],
        cwd=str(REPO_ROOT), env=env,
    )
    npm = shutil.which("npm")
    assert npm is not None, "npm not found on PATH -- required to start the frontend dev server"
    frontend = subprocess.Popen(
        [npm, "run", "dev", "--", "--port", "5173"],
        cwd=str(REPO_ROOT / "ui" / "frontend"), env=env,
    )

    try:
        _wait_healthy(f"{API_URL}/api/health")
        _wait_healthy(BASE_URL)
        _reshape_model_catalog()
        yield
    finally:
        _kill_process_tree(backend)
        _kill_process_tree(frontend)
        backend.wait(timeout=10)
        frontend.wait(timeout=10)
        shutil.rmtree(tmp_dir, ignore_errors=True)
