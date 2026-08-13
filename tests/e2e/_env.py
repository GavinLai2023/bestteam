"""Shared constants for the E2E suite. Ports are fixed -- safe in CI since
each job gets its own clean runner; see the design doc for local-dev notes."""
BASE_URL = "http://localhost:5173"
API_URL = "http://127.0.0.1:8000"

DEMO = ("demo", "demo-pass-123")  # org user (default org): Monitor, Wizard
OP = ("op", "op-pass-123")        # platform admin (no org): Advanced, Memory
ORG_LABEL = "Default Organization"

FAKE_ARCHITECT_SPEC = "fake-architect:e2e"
