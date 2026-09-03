"""Packaging / entry-point contract tests.

These guard the fixes for CR-006 (Alembic in the image), CR-007 (provider
extras installed in the official image), CR-014 (httpx in the aggregate tools
extra) and CR-015 (`python -m bestteam`). They assert declarations rather than
building the container, so they run in the deterministic suite; the container
`alembic current` / import smoke checks remain CI concerns.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent


def _pyproject():
    tomllib = pytest.importorskip("tomllib")
    return tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _extras():
    return _pyproject()["project"]["optional-dependencies"]


def test_tools_extra_declares_httpx():
    # CR-014: http_get imports httpx at call time; the aggregate tools extra
    # must therefore declare it so `bestteam[tools]` can run every built-in.
    assert any(dep.startswith("httpx") for dep in _extras()["tools"])


def test_tools_extra_declares_lxml_for_html_extraction():
    # Same reasoning as httpx above: `_html_to_text` imports lxml at call time
    # and degrades to raw markup without it, so `bestteam[tools]` must declare
    # it or every fetched page silently comes back as tags.
    assert any(dep.startswith("lxml") for dep in _extras()["tools"])
    assert any(dep.startswith("lxml") for dep in _extras()["tools-http"])


def test_providers_openai_extra_declares_langchain_and_openai():
    # CR-007: real-model string resolution needs langchain + langchain-openai,
    # and interview transcription needs openai.
    deps = " ".join(_extras()["providers-openai"])
    assert "langchain" in deps
    assert "langchain-openai" in deps
    assert "openai" in deps


def test_dockerfile_ships_alembic_and_providers():
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    # CR-006: Alembic assets copied so the documented upgrade command works.
    assert "alembic.ini" in dockerfile
    assert "COPY alembic" in dockerfile
    # CR-007: official image installs the provider extra.
    assert "providers-openai" in dockerfile


def test_readme_quickstart_installs_provider_extra():
    # CR-007/CR-017: the quick start uses openai: models, so it must install a
    # provider extra, not just [tools]/[ui].
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "providers-openai" in readme


def test_python_dash_m_bestteam_entry_point():
    # CR-015: the documented `python -m bestteam` invocation must work.
    result = subprocess.run(
        [sys.executable, "-m", "bestteam", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage" in result.stdout


# --- G1: reproducible installs -------------------------------------------
#
# `requirements.lock` is a `uv pip compile --universal` constraints file over
# every extra CI and the Dockerfile install. These three tests are the drift
# guard: they run in the deterministic suite with no `uv` on the machine, and
# fail when someone edits `pyproject.toml` without regenerating the lock, or
# installs somewhere without it.

_LOCKED_EXTRAS = (
    "ui",
    "dev",
    "tools",
    "test",
    "interview",
    "providers-openai",
    "providers-deepseek",
    "providers-google",
)


def _lock_text():
    return (_ROOT / "requirements.lock").read_text(encoding="utf-8")


def _pinned_versions():
    """`{normalised name: {version, ...}}` -- a name can pin more than one
    version under different environment markers (`--universal`)."""
    from packaging.utils import canonicalize_name

    pins = {}
    for line in _lock_text().splitlines():
        if not line or line.startswith(("#", " ", "-")):
            continue
        name, _, rest = line.partition("==")
        version = rest.split(";", 1)[0].strip()
        pins.setdefault(canonicalize_name(name), set()).add(version)
    return pins


def test_lockfile_was_compiled_over_the_extras_ci_and_docker_install():
    header = _lock_text().splitlines()[1]
    for extra in _LOCKED_EXTRAS:
        assert f"--extra {extra}" in header, header
    assert "--universal" in header, header


def test_lockfile_pins_every_declared_dependency_within_its_specifier():
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    project = _pyproject()["project"]
    declared = list(project["dependencies"])
    for extra in _LOCKED_EXTRAS:
        declared += project["optional-dependencies"][extra]
    pins = _pinned_versions()
    for spec in declared:
        req = Requirement(spec)
        versions = pins.get(canonicalize_name(req.name))
        assert versions, f"{req.name} is declared but not in requirements.lock"
        for version in versions:
            assert req.specifier.contains(Version(version), prereleases=True), (
                f"{req.name}=={version} in requirements.lock does not satisfy {spec}"
            )


def test_langgraph_and_langchain_have_upper_bounds():
    from packaging.requirements import Requirement

    project = _pyproject()["project"]
    specs = {
        Requirement(s).name: Requirement(s)
        for s in project["dependencies"] + project["optional-dependencies"]["providers-openai"]
    }
    for name in ("langgraph", "langchain-core", "langchain", "langchain-openai"):
        assert any(op.operator in ("<", "<=", "==", "~=") for op in specs[name].specifier), (
            f"{name} has no upper bound: {specs[name]}"
        )


def test_dockerfile_and_ci_install_under_the_lockfile():
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY pyproject.toml requirements.lock" in dockerfile
    assert "-c requirements.lock" in dockerfile
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installs = [line for line in ci.splitlines() if "pip install" in line]
    assert installs
    for line in installs:
        assert "-c requirements.lock" in line, line
    assert "cache-dependency-path: pyproject.toml" not in ci


# --- G3: container / ops baseline ------------------------------------------


def test_image_runs_unprivileged_with_a_healthcheck_and_migrating_entrypoint():
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "useradd" in dockerfile
    assert "\nUSER app\n" in dockerfile
    # Only the data directory is handed to the unprivileged user: the volume
    # mount point must exist in the image with that owner, or a fresh named
    # volume is created root-owned and the first write fails.
    assert "chown -R app:app ui/backend/data" in dockerfile
    assert "HEALTHCHECK" in dockerfile and "/api/health" in dockerfile
    assert 'ENTRYPOINT ["./docker-entrypoint.sh"]' in dockerfile
    # The default command must stay `uvicorn ...`: that is the literal the
    # entrypoint keys its migration step on.
    assert 'CMD ["uvicorn"' in dockerfile


def test_entrypoint_migrates_only_when_starting_the_server():
    script = (_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "\r" not in script, "CRLF would break the shebang inside the container"
    assert 'if [ "$1" = "uvicorn" ]' in script
    assert "alembic upgrade head" in script
    assert 'exec "$@"' in script
    attrs = (_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attrs
    # Both files are checked by this test, so a change to either has to run
    # it: the CI path filter is an allowlist.
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "- 'docker-entrypoint.sh'" in ci
    assert "- '.gitattributes'" in ci


def test_compose_restarts_and_bounds_the_services():
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count("restart: unless-stopped") == 2
    assert "memory: 2g" in compose
    assert compose.count("max-size:") == 2
    # The backend image declares a HEALTHCHECK; the frontend should wait on it
    # rather than on the container merely having started, so an operator who
    # opens port 80 right after `up -d` finds a backend that answers.
    assert "condition: service_healthy" in compose


def test_backup_covers_the_data_volume_not_only_the_database():
    # docs/BETA_NOTES.md promises a nightly backup of "everything on the
    # server"; the database script alone leaves knowledge-base uploads (the
    # originals behind every collection) out. A second script tars the rest
    # of the data volume, and the docs pair the two.
    files = (_ROOT / "scripts" / "backup-files.sh").read_text(encoding="utf-8")
    assert "\r" not in files
    assert "/app/ui/backend/data" in files
    # The live SQLite file (and its WAL/journal siblings) belong to the
    # online-backup script, never to a raw tar of a database in use.
    assert "--exclude='bestteam.db'" in files or "--exclude=bestteam.db" in files
    assert "bestteam.db-*" in files
    # GNU tar exits 1 ("file changed as we read it") when an upload is staged
    # while the archive streams; the archive is still complete. Only exit 2
    # (a real error) may fail the backup, or cron reports a spurious failure.
    assert "--warning=no-file-changed" in files
    assert "-gt 1" in files
    doc = (_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    assert "backup-files.sh" in doc


def test_backup_db_also_takes_the_per_user_memory_database():
    # Per-user memory (BESTTEAM_MEMORY_DB) is a SECOND SQLite database. Left to
    # backup-files.sh it would be tarred while in use -- the half-written page
    # the two-script split exists to avoid -- so the online-backup script takes
    # it too, beside the main file, with nothing extra in the operator's cron.
    db = (_ROOT / "scripts" / "backup-db.sh").read_text(encoding="utf-8")
    assert "\r" not in db
    assert "BESTTEAM_MEMORY_DB" in db
    assert "-memory.db" in db
    # Both databases go through sqlite3's backup API, not a file copy.
    assert db.count("src.backup(dst)") == 2
    # Enabled but never written to: connecting to a missing file would CREATE an
    # empty database and hand it over as if it were a backup.
    assert "test -f" in db
    doc = (_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    assert "-memory.db" in doc


def test_restore_script_follows_the_documented_procedure():
    restore = (_ROOT / "scripts" / "restore.sh").read_text(encoding="utf-8")
    assert "\r" not in restore
    # Stop -> copy -> hand the file back to uid 1000 -> start -> verify: the
    # same steps docs/deployment.md spells out, so the two cannot drift.
    for step in (
        "docker compose stop backend",
        "docker compose cp",
        "chown -R 1000:1000",
        "docker compose start backend",
        "/api/health",
    ):
        assert step in restore, step
    # `docker compose cp` lands in the stopped backend container's own
    # filesystem; a later `docker compose run` is a fresh container that
    # shares only the data volume -- so the archive must be staged inside the
    # data directory, never /tmp, or the extraction finds nothing.
    assert "/tmp/" not in restore
    # An optional third argument puts back the per-user memory database. It must
    # be resolved from BESTTEAM_MEMORY_DB *before* the backend is stopped -- an
    # unset variable that surfaced mid-restore would leave the deployment down
    # with the database already overwritten -- and copied AFTER the files
    # archive, whose raw-tar copy of the same file it exists to overwrite.
    assert "[memory.db]" in restore
    assert "BESTTEAM_MEMORY_DB" in restore
    assert restore.index("MEM_PATH=$(") < restore.index("docker compose stop backend")
    assert restore.index('cp "$FILES_BACKUP"') < restore.index('cp "$MEM_BACKUP"')
    doc = (_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    assert "restore.sh" in doc
