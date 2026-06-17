"""Shared pytest setup. Ensures ui.backend.main can be imported during tests
without tripping the BESTTEAM_SECRET_KEY startup guard in ui/backend/main.py
-- that guard refuses to start with the public dev-default secret."""
import os

os.environ.setdefault("BESTTEAM_SECRET_KEY", "test-secret-key-not-for-production-use")
