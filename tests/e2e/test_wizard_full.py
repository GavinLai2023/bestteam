"""Full Team Builder wizard journey scenarios -- previously skipped in
docs/run_ui_tests.py ("T4-1..T4-6 (AI generation requires real LLM API
key)"). Un-skipped here against the fake architect (see conftest.py /
the design doc). Slow: only runs in the main-branch full-regression job."""
import pytest
from playwright.sync_api import expect as pw_expect

from ._env import BASE_URL, DEMO

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def _login(page):
    page.goto(BASE_URL + "/login")
    page.fill("#username", DEMO[0])
    page.fill("#password", DEMO[1])
    page.click("button[type=submit]")
    # LandingPage ("/") does an async, data-dependent redirect (to
    # /activity or /wizard) once logged in -- asserting an exact
    # post-login URL races that redirect (see test_smoke.py's login_ui).
    # Wait for the authenticated app shell instead.
    page.wait_for_selector(".top-nav", timeout=8000)


def _build_to_confirm(page, intent: str):
    page.goto(BASE_URL + "/wizard")
    page.wait_for_selector("#intent", timeout=8000)
    page.fill("#intent", intent)
    page.click("button:has-text('Start building my team')")
    page.wait_for_url("**/documents", timeout=15000)
    page.click("button:has-text('Skip for now')")
    page.wait_for_url("**/preview", timeout=20000)
    page.wait_for_selector(".team-flow, .employee-card", timeout=8000)
    page.click("button:has-text('Continue')")
    page.wait_for_url("**/confirm", timeout=8000)


def test_t4_1_apply_feedback_regenerates_team(page):
    """Regeneration loop via the Confirm page's "Which assistant should
    your team use?" ModelPicker + feedback box."""
    _login(page)
    _build_to_confirm(page, "We handle customer support emails.")

    # Only one ModelPicker instance is on the page at this point ("Show
    # what we understood about your business" hasn't been expanded, so the
    # second instance in that panel isn't rendered yet) -- #model-picker is
    # unambiguous here. Verified against ConfirmPage.tsx / ModelPicker.tsx.
    page.fill("#solution-feedback", "Make the team also draft a summary of each reply.")
    page.select_option("#model-picker", label="E2E Test Architect (fake, $0)")
    page.click("button:has-text('Apply this change')")
    page.wait_for_selector(".banner-info:has-text('Adjustments so far')", timeout=15000)
    assert "summary" in page.locator(".banner-info").inner_text().lower()


def test_t4_2_test_run_before_deploy(page):
    """Stage 5: run a real task through the sandboxed (not-yet-deployed)
    team on the Preview page before continuing to Confirm/Deploy."""
    _login(page)
    page.goto(BASE_URL + "/wizard")
    page.wait_for_selector("#intent", timeout=8000)
    page.fill("#intent", "We handle customer support emails.")
    page.click("button:has-text('Start building my team')")
    page.wait_for_url("**/documents", timeout=15000)
    page.click("button:has-text('Skip for now')")
    page.wait_for_url("**/preview", timeout=20000)

    page.fill("#test-input", "A customer wants to reset their password.")
    page.click("button:has-text('Run this through your team')")
    page.wait_for_selector(".activity-card.run_completed", timeout=30000)


def test_t4_3_apply_button_requires_a_model(page):
    """The Confirm page's "Apply this change" button is gated on a model
    being selected (ConfirmPage.tsx's applyFeedback: `if (!model || busy)
    return`), not on feedback text -- feedback itself is optional ("the
    customer may just be switching which assistant/model their team uses,
    with nothing else to describe", per the component's own comment).
    ModelPicker auto-selects a default the moment the model catalog loads
    (pickDefaultModel), so the button becomes enabled almost immediately
    after the Confirm page mounts -- confirmed by running this headed: the
    brief's original version of this test asserted the button stayed
    disabled after typing feedback, which does not hold in the real app.
    This exercises the actual guard instead: apply with blank feedback and
    the auto-selected model still succeeds (model, not feedback, is what's
    required). Also confirmed headed: the backend (builder.py's
    submit_solution_feedback) only appends a feedback_history entry `if
    req.feedback.strip()`, so a blank-feedback apply does NOT show the
    "Adjustments so far" banner -- that's expected, not a bug."""
    _login(page)
    _build_to_confirm(page, "We handle customer support emails.")

    apply_button = page.locator("button:has-text('Apply this change')")
    # The auto-selected default model makes the button enabled without any
    # feedback text typed at all -- pw_expect retries until the catalog
    # fetch + ModelPicker's auto-select effect have settled.
    pw_expect(apply_button).to_be_enabled(timeout=8000)
    assert page.locator("#solution-feedback").input_value() == ""

    apply_button.click()
    # No feedback text was submitted, so no history entry is recorded --
    # wait for the request round-trip (button leaves its busy "Updating…"
    # state) rather than a banner that isn't expected to appear.
    page.wait_for_selector("button:has-text('Updating…')", state="hidden", timeout=15000)
    assert not page.locator(".banner-error").is_visible()
    assert page.locator(".banner-info:has-text('Adjustments so far')").count() == 0
    # The team is still there -- the apply succeeded, it just had nothing
    # new to log.
    page.wait_for_selector(".team-flow, .employee-card", timeout=5000)


def test_t4_4_regenerate_requirements_summary(page):
    """The "Show what we understood about your business" panel's
    regenerate-with-feedback loop."""
    _login(page)
    _build_to_confirm(page, "We handle customer support emails.")

    page.click("button:has-text('Show what we understood about your business')")
    page.wait_for_selector("#req-feedback", timeout=5000)
    page.fill("#req-feedback", "We also handle billing questions, not just support.")
    # ModelPicker.tsx uses a fixed id="model-picker" for every instance --
    # ConfirmPage now renders two (the solution-feedback one above, plus
    # this panel's "redo this" one). Scope by the label's adjacent select
    # (unique per instance) rather than the ambiguous #model-picker id;
    # `.last` as a fallback since this panel's picker is the second one in
    # DOM order. Verified against ConfirmPage.tsx / ModelPicker.tsx.
    page.locator("label:has-text('Which assistant should redo this?') + select, select#model-picker").last.select_option(
        label="E2E Test Architect (fake, $0)"
    )
    page.click("button:has-text('Regenerate summary')")
    page.wait_for_selector("#summary", timeout=15000)
    assert page.locator("#summary").input_value()


def test_t4_5_deploy_then_run_for_real(page):
    """Full journey through to a real (non-sandbox) run of the deployed
    team via the Monitor page, confirming the fake architect's team
    executes cleanly end to end."""
    _login(page)
    _build_to_confirm(page, "We handle customer support emails.")
    page.click("button:has-text('Continue to deploy')")
    page.wait_for_url("**/deploy", timeout=8000)
    page.click("button:has-text('Launch my team')")
    page.wait_for_selector("text=Your team is live", timeout=20000)

    page.click("button:has-text('Run a team')")
    page.wait_for_selector("select", timeout=8000)
    page.fill("textarea", "A customer is asking about a refund.")
    page.click("button:has-text('Run')")
    # MonitorPage hides its live trace <ul> once a terminal event lands
    # unless "Show technical trace" is toggled on -- the result banner
    # (.result-<event.type>, e.g. .result-run_completed) is the reliable
    # signal a run finished, not the old .event.event-run_completed
    # list-item selector (confirmed against MonitorPage.tsx and by
    # test_smoke.py's T2 fix).
    page.wait_for_selector(".result-run_completed", timeout=30000)
    page.wait_for_selector(".result", timeout=5000)


def test_t4_6_revisit_documents_after_deploy_refines_not_regenerates(page):
    """Revisiting Documents after a specification already exists must
    refine the existing design (submitSolution, POST .../solution) rather
    than silently discarding prior feedback and regenerating from scratch
    (submitSpecification, POST .../specification) -- see DocumentsPage.tsx's
    comment on this exact regression (the `session.specification_json ?
    submitSolution(...) : submitSpecification(...)` branch)."""
    _login(page)
    _build_to_confirm(page, "We handle customer support emails.")
    page.fill("#solution-feedback", "Always sign off with 'Best, the Support Team'.")
    page.select_option("#model-picker", label="E2E Test Architect (fake, $0)")
    page.click("button:has-text('Apply this change')")
    page.wait_for_selector(".banner-info:has-text('Adjustments so far')", timeout=15000)

    page.click("text=Need to add or update a document? Upload it here")
    page.wait_for_url("**/documents", timeout=8000)
    # This is the assertion that actually guards the regression: checking a
    # UI side effect (e.g. the feedback banner) doesn't work here, because
    # feedback_history is untouched by both endpoints when there's no
    # kbHint (no files uploaded), and the fake architect's
    # generate_specification returns a fixed canned Specification
    # regardless of which endpoint/input produced it -- so nothing
    # user-visible would differ even if this "Skip for now" click
    # regressed to calling submitSpecification instead of submitSolution.
    # Assert on the network request itself instead.
    with page.expect_request(
        lambda r: r.method == "POST" and r.url.endswith("/solution"), timeout=8000
    ):
        page.click("button:has-text('Skip for now')")
    page.wait_for_url("**/preview", timeout=20000)
    page.click("button:has-text('Continue')")
    page.wait_for_url("**/confirm", timeout=8000)

    history_text = page.locator(".banner-info").inner_text()
    assert "sign off" in history_text.lower() or "Best, the Support Team" in history_text
