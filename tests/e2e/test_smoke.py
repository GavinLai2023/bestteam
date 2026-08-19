"""PR-gate E2E smoke suite: headless, self-contained (see conftest.py).
Ports docs/run_ui_tests.py's T1/T2/T3/T5 scenarios (T2-3 stays out of scope
-- it needs the backend stopped mid-run) plus a new wizard smoke scenario
using the fake architect. Full wizard journey scenarios (T4) live in
test_wizard_full.py, gated to slow/main-only."""
import json
import time

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect as pw_expect

from ._env import BASE_URL, DEMO, OP, ORG_LABEL

pytestmark = pytest.mark.e2e


def login_ui(page, account):
    username, password = account
    page.goto(BASE_URL + "/login")
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("button[type=submit]")
    # Post-login landing route depends on account type and org state
    # (LandingPage/RequireOrgMember redirect logic) -- wait for the
    # authenticated app shell rather than a specific path.
    page.wait_for_selector(".top-nav", timeout=8000)


def goto_expecting_login_redirect(page, path, timeout=5000):
    """Navigate to a guarded route and wait for the bounce to /login.

    `page.goto()` waits for `load` by default, but the app's client-side auth
    guard can redirect before that fires -- Playwright then raises
    "Navigation to X is interrupted by another navigation to /login" rather
    than returning. Whether it raises is a pure race between the redirect and
    the load event, which is why the same assertion failed in `e2e-smoke` and
    passed in `e2e-full` within a single CI run.

    `wait_until="commit"` narrows that window but does not close it: on a fast
    runner the guard can abort the navigation before it even commits, which
    surfaces as `net::ERR_ABORTED` instead. There is no `wait_until` value that
    reliably survives a navigation the app is deliberately cancelling, so the
    navigation error is tolerated outright.

    That is not a weakened assertion. `wait_for_url` below is the assertion,
    and it still fails if the redirect does not happen -- including if the page
    failed to load for some unrelated reason, since then no /login URL ever
    arrives either.
    """
    try:
        page.goto(BASE_URL + path, wait_until="commit")
    except PlaywrightError:
        pass
    page.wait_for_url("**/login", timeout=timeout)


def logout(page):
    page.click("button.logout-button")
    page.wait_for_url("**/login", timeout=5000)


def open_advanced_tab(page, label):
    page.click(f".advanced-kinds button:has-text('{label}')")
    page.wait_for_selector(".advanced-list", timeout=3000)
    time.sleep(0.3)


def test_smoke_journey(page):
    # AdvancedPage's Delete button gates on window.confirm(); Playwright
    # auto-dismisses native dialogs (confirm() -> false) unless a handler
    # accepts them, which would otherwise make every Delete click a silent
    # no-op.
    page.on("dialog", lambda dialog: dialog.accept())

    # -- T1. Authentication (as demo) --
    goto_expecting_login_redirect(page, "/", timeout=6000)
    login_ui(page, DEMO)
    assert "/login" not in page.url

    logout(page)
    login_ui(page, DEMO)
    assert "/login" not in page.url

    page.goto(BASE_URL + "/login")
    page.fill("#username", DEMO[0])
    page.fill("#password", "wrongpassword!")
    page.click("button[type=submit]")
    time.sleep(1.5)
    assert "/login" in page.url
    page.wait_for_selector(".banner-error", timeout=4000)

    login_ui(page, DEMO)  # re-login after the bad-password check

    ctx2 = page.context.browser.new_context()
    p2 = ctx2.new_page()
    goto_expecting_login_redirect(p2, "/advanced")
    ctx2.close()

    # -- T2. Monitor page (as demo) --
    js_errors = []
    page.on("pageerror", lambda e: js_errors.append(str(e)))
    page.goto(BASE_URL + "/run")
    page.wait_for_selector("select", timeout=8000)
    options = page.locator("select option").all_inner_texts()
    assert len(options) > 0, "Pipeline dropdown is empty -- was BESTTEAM_DEMO_PIPELINES=1 set?"
    bad = [e for e in js_errors if "Cannot read properties of undefined" in e]
    assert not bad, f"TypeError still present: {bad[0]}"

    page.goto(BASE_URL + "/run")
    page.wait_for_selector("select", timeout=8000)
    opts = page.locator("select option").all_inner_texts()
    target = "code_review" if "code_review" in opts else opts[0]
    page.select_option("select", label=target)
    page.fill("textarea", "def add(a, b): return a + b")
    page.click("button:has-text('Run')")
    # The live trace list hides itself once a terminal event arrives (see
    # MonitorPage's traceExpanded toggle) -- the result banner is the
    # reliable signal that the run finished.
    page.wait_for_selector(".result-run_completed", timeout=30000)

    # -- T3. Advanced page (as op) --
    logout(page)
    login_ui(page, OP)
    page.goto(BASE_URL + "/advanced")
    page.wait_for_selector(".advanced-kinds", timeout=6000)

    for label in ["Pipelines", "Skills", "Knowledge bases", "Tools", "Model catalog"]:
        page.click(f".advanced-kinds button:has-text('{label}')")
        page.wait_for_selector(".advanced-list", timeout=3000)
        time.sleep(0.3)
    labels = page.locator(".advanced-kinds button").all_inner_texts()
    assert "Agents" not in labels and "Teams" not in labels

    open_advanced_tab(page, "Skills")
    # Org state persists across tabs (AdvancedPage only forces a default when
    # switching *into* an org-required tab) and the initial mount-time
    # default resolves to the first real org, not the platform tier -- so
    # explicitly select the platform tier before seeding a built-in skill,
    # rather than relying on the org state still being unset at this point.
    page.select_option(".advanced-org select", value="__platform__")
    SEED = f"seed_{int(time.time())}"
    page.fill(".advanced-new input", SEED)
    page.click(".advanced-new button:has-text('New')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{SEED}')", timeout=4000)
    page.fill(".advanced-editor textarea", json.dumps({"instructions": "seed", "description": "seed"}, indent=2))
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success", timeout=6000)
    page.click(f".advanced-list button:has-text('{SEED}')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{SEED}')", timeout=4000)
    page.select_option(".advanced-org select", label=ORG_LABEL)
    pw_expect(page.locator(".advanced-editor .hint")).to_be_visible(timeout=4000)
    pw_expect(page.locator(".advanced-editor textarea")).to_have_count(0)
    page.select_option(".advanced-org select", value="__platform__")

    open_advanced_tab(page, "Skills")
    SKILL = f"skill_{int(time.time())}"
    SKILL_BODY = json.dumps({
        "instructions": "Always reply professionally. End with a polite sign-off.",
        "description": "Professional email style",
    }, indent=2)

    page.fill(".advanced-new input", SKILL)
    page.click(".advanced-new button:has-text('New')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{SKILL}')", timeout=4000)
    page.fill(".advanced-editor textarea", SKILL_BODY)
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success", timeout=6000)
    page.wait_for_selector(f".advanced-list button:has-text('{SKILL}')", timeout=4000)

    page.click(f".advanced-list button:has-text('{SKILL}')")
    raw = json.loads(page.locator(".advanced-editor textarea").input_value())
    raw["description"] = "Formal email writing style"
    page.fill(".advanced-editor textarea", json.dumps(raw, indent=2))
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success", timeout=5000)
    open_advanced_tab(page, "Knowledge bases")
    # Knowledge bases requires a real org, so switching to it while on the
    # platform tier force-substitutes the last real org selected (see
    # AdvancedPage's selectKind) -- and that substitution persists after
    # switching back to Skills, since Skills (org-optional) doesn't force it
    # back. Re-select the platform tier explicitly so the still-platform-tier
    # SKILL is visible again.
    open_advanced_tab(page, "Skills")
    page.select_option(".advanced-org select", value="__platform__")
    page.click(f".advanced-list button:has-text('{SKILL}')")
    val = json.loads(page.locator(".advanced-editor textarea").input_value())
    assert val.get("description") == "Formal email writing style"

    page.click(f".advanced-list button:has-text('{SKILL}')")
    page.fill(".advanced-editor textarea", '{"instructions": "test"')  # missing }
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-error:has-text('Not valid JSON')", timeout=4000)

    page.click(f".advanced-list button:has-text('{SKILL}')")
    page.fill(".advanced-editor textarea", "{}")
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-error", timeout=6000)
    err = page.locator(".banner-error").inner_text()
    assert "instructions" in err.lower() or "validation" in err.lower()

    page.click(f".advanced-list button:has-text('{SKILL}')")
    page.fill(".advanced-editor textarea", SKILL_BODY)
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success", timeout=5000)
    page.click(".advanced-editor button:has-text('Delete')")
    pw_expect(page.locator(f".advanced-list button:has-text('{SKILL}')")).to_have_count(0, timeout=5000)

    CATALOG_SPEC = f"fake:model_{int(time.time())}"
    open_advanced_tab(page, "Model catalog")
    page.fill(".advanced-new input", CATALOG_SPEC)
    page.click(".advanced-new button:has-text('New')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{CATALOG_SPEC}')", timeout=4000)
    page.fill(".advanced-editor textarea", json.dumps({
        "display_name": "Test model", "description": "Smoke-test entry",
        "tier": "economy", "input_price_per_1k": 0, "output_price_per_1k": 0,
    }, indent=2))
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success", timeout=5000)
    page.wait_for_selector(f".advanced-list button:has-text('{CATALOG_SPEC}')", timeout=4000)

    open_advanced_tab(page, "Tools")
    page.click(".advanced-list button:has-text('web_search')")
    pw_expect(page.locator(".advanced-readonly-text")).to_be_visible(timeout=4000)
    pw_expect(page.locator(".advanced-editor textarea")).to_have_count(0)

    PL = f"pl_{int(time.time())}"
    PL_BODY = json.dumps({
        "name": PL,
        "teams": [{"name": "t", "mode": "sequential", "agents": ["a"]}],
        "agents": [{"name": "a", "role": "Asst", "goal": "Help",
                    "backstory": "Friendly AI assistant.", "model": "fake:Hello! How can I help?"}],
        "pipeline": {"steps": ["t"]},
    }, indent=2)

    open_advanced_tab(page, "Pipelines")
    page.select_option(".advanced-org select", label=ORG_LABEL)
    page.fill(".advanced-new input", PL)
    page.click(".advanced-new button:has-text('New')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{PL}')", timeout=4000)
    page.fill(".advanced-editor textarea", PL_BODY)
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success, .banner-error", timeout=8000)
    assert not page.locator(".banner-error").is_visible()

    logout(page)
    login_ui(page, DEMO)
    page.goto(BASE_URL + "/run")
    page.wait_for_selector("select", timeout=8000)
    opts = page.locator("select option").all_inner_texts()
    assert PL in opts, f"{PL} not found in Monitor dropdown for demo"

    # -- T5. Edge cases --
    page.goto(BASE_URL + "/")
    page.evaluate("localStorage.removeItem('bestteam_token')")
    goto_expecting_login_redirect(page, "/advanced")

    login_ui(page, OP)
    page.goto(BASE_URL + "/advanced")
    page.wait_for_selector(".advanced-kinds", timeout=5000)
    open_advanced_tab(page, "Pipelines")
    page.select_option(".advanced-org select", label=ORG_LABEL)
    page.fill(".advanced-new input", PL)
    page.click(".advanced-new button:has-text('New')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{PL}')", timeout=4000)
    page.fill(".advanced-editor textarea", PL_BODY)
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success, .banner-error", timeout=8000)
    assert not page.locator(".banner-error").is_visible()
    count = page.locator(f".advanced-list button:has-text('{PL}')").count()
    assert count == 1, f"Expected 1 entry (upsert), got {count}"

    login_ui(page, DEMO)
    page.goto(BASE_URL + "/run")
    page.wait_for_selector("select", timeout=8000)
    page.fill("textarea", "")
    assert page.locator("button:has-text('Run')").is_disabled()


def test_wizard_smoke(page):
    """New PR-gate scenario: intent -> generate -> Preview -> Deploy ->
    confirm the team shows up in Monitor. Uses the fake architect (reshaped
    into the catalog by the e2e_backend fixture) so no real LLM key is
    needed. Stops at Deploy -- never opens the Confirm-page's ModelPicker
    dropdown (that's covered by test_wizard_full.py)."""
    login_ui(page, DEMO)
    page.goto(BASE_URL + "/wizard")
    page.wait_for_selector("#intent", timeout=8000)

    team_name_hint = f"e2e_wizard_team_{int(time.time())}"
    page.fill(
        "#intent",
        f"We get customer support emails and need quick replies. "
        f"(test marker: {team_name_hint})",
    )
    page.click("button:has-text('Start building my team')")
    page.wait_for_url("**/documents", timeout=15000)

    page.wait_for_selector("button:has-text('Skip for now')", timeout=8000)
    page.click("button:has-text('Skip for now')")
    page.wait_for_url("**/preview", timeout=20000)

    page.wait_for_selector(".team-flow, .employee-card", timeout=8000)

    page.click("button:has-text('Continue')")
    page.wait_for_url("**/confirm", timeout=8000)

    page.click("button:has-text('Continue to deploy')")
    page.wait_for_url("**/deploy", timeout=8000)

    page.click("button:has-text('Launch my team')")
    page.wait_for_selector("text=Your team is live", timeout=20000)

    page.click("button:has-text('Run a team')")
    page.wait_for_url("**/run**", timeout=8000)
    page.wait_for_selector("select", timeout=8000)
    opts = page.locator("select option").all_inner_texts()
    assert any("e2e_support_team" in o or "Support Team (E2E)" in o for o in opts), (
        f"deployed fake-architect team not found in Monitor dropdown: {opts}"
    )
