"""
bestteam UI automated test runner.
Covers all scenarios in docs/ui-testing-guide.md except:
  T2-3 (requires stopping the backend mid-run)
  T4    (wizard AI steps require a real LLM API key)

Run: .venv\Scripts\python.exe docs\run_ui_tests.py
"""
import time, json, sys, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
API  = "http://127.0.0.1:8000"
USER = f"uitest_{int(time.time())}"
PASS = "TestPass123!"

PASSED, FAILED = [], []

def ok(name):
    PASSED.append(name)
    print(f"  [PASS] {name}")

def fail(name, reason=""):
    FAILED.append(name)
    print(f"  [FAIL] {name}" + (f" -- {reason}" if reason else ""))

def run(name, fn):
    try:
        fn()
        ok(name)
    except Exception as e:
        fail(name, str(e)[:140])

def api_post(path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        API + path, data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def login_ui(page, username, password):
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("button[type=submit]")
    page.wait_for_url(BASE + "/", timeout=8000)


def test_all():
    # Register test user via API (no registration UI exists)
    print(f"\nRegistering test user '{USER}' via API...")
    try:
        api_post("/api/auth/register", {"username": USER, "password": PASS})
        print("  OK")
    except urllib.error.HTTPError as e:
        print(f"  WARN: {e.code} {e.reason} (user may already exist)")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=150)
        ctx = browser.new_context()
        page = ctx.new_page()

        # ── T1 . Authentication ──────────────────────────────────────────────

        print("\n== T1 . Authentication ==")

        # T1-1: auth guard sends unauthenticated user to /login, then login works
        def t1_1():
            page.goto(BASE + "/")
            page.wait_for_url("**/login", timeout=6000)
            login_ui(page, USER, PASS)
            assert page.url == BASE + "/", f"Expected /, got {page.url}"
        run("T1-1 Auth guard + successful login", t1_1)

        # T1-2: logout then log back in
        def t1_2():
            page.click("button.logout-button")
            page.wait_for_url("**/login", timeout=5000)
            login_ui(page, USER, PASS)
            assert "/" in page.url
        run("T1-2 Logout and log back in", t1_2)

        # T1-3: wrong password shows error, stays on /login
        def t1_3():
            page.goto(BASE + "/login")
            page.fill("#username", USER)
            page.fill("#password", "wrongpassword!")
            page.click("button[type=submit]")
            time.sleep(1.5)
            assert "/login" in page.url, f"Expected /login, got {page.url}"
            page.wait_for_selector(".banner-error", timeout=4000)
        run("T1-3 Wrong password stays on login with error", t1_3)

        # Re-login after T1-3
        login_ui(page, USER, PASS)

        # T1-4: separate context without token -> redirect to login
        def t1_4():
            ctx2 = browser.new_context()
            p2 = ctx2.new_page()
            p2.goto(BASE + "/advanced")
            p2.wait_for_url("**/login", timeout=5000)
            ctx2.close()
        run("T1-4 Unauthenticated access redirects to /login", t1_4)

        # ── T2 . Monitor Page ────────────────────────────────────────────────

        print("\n== T2 . Monitor Page ==")

        # T2-1: workflow dropdown populated, no TypeError
        def t2_1():
            js_errors = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))
            page.goto(BASE + "/")
            page.wait_for_selector("select", timeout=8000)
            options = page.locator("select option").all_inner_texts()
            assert len(options) > 0, "Workflow dropdown is empty"
            bad = [e for e in js_errors if "Cannot read properties of undefined" in e]
            assert not bad, f"TypeError still present: {bad[0]}"
        run("T2-1 Workflow dropdown loads without TypeError", t2_1)

        # T2-2: run code_review_pipeline, see completed event and final output
        def t2_2():
            page.goto(BASE + "/")
            page.wait_for_selector("select", timeout=8000)
            opts = page.locator("select option").all_inner_texts()
            target = "code_review_pipeline" if "code_review_pipeline" in opts else opts[0]
            page.select_option("select", label=target)
            page.fill("textarea", "def add(a, b): return a + b")
            page.click("button:has-text('Run')")
            page.wait_for_selector(".event.event-run_completed", timeout=30000)
            page.wait_for_selector(".result", timeout=5000)
        run("T2-2 Run workflow and see live trace + final output", t2_2)

        print("  [SKIP] T2-3 Unreachable backend banner (would stop the server)")

        # ── T3 . Advanced Page ───────────────────────────────────────────────

        print("\n== T3 . Advanced Page ==")

        page.goto(BASE + "/advanced")
        page.wait_for_selector(".advanced-kinds", timeout=6000)

        # T3-1: navigate all tabs
        def t3_1():
            for label in ["Agents", "Teams", "Knowledge bases", "Workflows", "Skills", "Model catalog"]:
                page.click(f".advanced-kinds button:has-text('{label}')")
                page.wait_for_selector(".advanced-list", timeout=3000)
                time.sleep(0.3)
        run("T3-1 Navigate between all resource tabs", t3_1)

        # Select Skills tab
        page.click(".advanced-kinds button:has-text('Skills')")
        time.sleep(0.3)

        SKILL = f"skill_{int(time.time())}"
        SKILL_BODY = json.dumps({
            "instructions": "Always reply professionally. End with a polite sign-off.",
            "description": "Professional email style"
        }, indent=2)

        # T3-2: create a skill
        def t3_2():
            page.fill(".advanced-new input", SKILL)
            page.click(".advanced-new button:has-text('New')")
            page.wait_for_selector(f".advanced-editor h2:has-text('{SKILL}')", timeout=4000)
            page.fill(".advanced-editor textarea", SKILL_BODY)
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success", timeout=6000)
            page.wait_for_selector(f".advanced-list button:has-text('{SKILL}')", timeout=4000)
        run("T3-2 Create a Skill", t3_2)

        # T3-3: edit the skill and verify persistence
        def t3_3():
            page.click(f".advanced-list button:has-text('{SKILL}')")
            raw = json.loads(page.locator(".advanced-editor textarea").input_value())
            raw["description"] = "Formal email writing style"
            page.fill(".advanced-editor textarea", json.dumps(raw, indent=2))
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success", timeout=5000)
            # Navigate away and back
            page.click(".advanced-kinds button:has-text('Agents')")
            time.sleep(0.3)
            page.click(".advanced-kinds button:has-text('Skills')")
            time.sleep(0.5)
            page.click(f".advanced-list button:has-text('{SKILL}')")
            val = json.loads(page.locator(".advanced-editor textarea").input_value())
            assert val.get("description") == "Formal email writing style", \
                f"Got: {val.get('description')}"
        run("T3-3 Edit Skill and verify persistence", t3_3)

        # T3-4: malformed JSON rejected client-side
        def t3_4():
            page.click(f".advanced-list button:has-text('{SKILL}')")
            page.fill(".advanced-editor textarea", '{"instructions": "test"')  # missing closing }
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-error:has-text('Not valid JSON')", timeout=4000)
        run("T3-4 Malformed JSON rejected with 'Not valid JSON'", t3_4)

        # T3-5: missing required field rejected by backend
        def t3_5():
            page.click(f".advanced-list button:has-text('{SKILL}')")
            page.fill(".advanced-editor textarea", '{}')
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-error", timeout=6000)
            err = page.locator(".banner-error").inner_text()
            assert "instructions" in err.lower() or "validation" in err.lower(), \
                f"Unexpected error text: {err}"
        run("T3-5 Missing 'instructions' field rejected by backend", t3_5)

        # T3-6: delete the skill
        def t3_6():
            page.click(f".advanced-list button:has-text('{SKILL}')")
            # Restore valid JSON first
            page.fill(".advanced-editor textarea", SKILL_BODY)
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success", timeout=5000)
            page.click(".advanced-editor button:has-text('Delete')")
            # After delete: selectedId becomes null so the editor hides — no banner-success.
            # Verify success by waiting for the item to vanish from the list.
            from playwright.sync_api import expect as pw_expect
            pw_expect(page.locator(f".advanced-list button:has-text('{SKILL}')")).to_have_count(0, timeout=5000)
        run("T3-6 Delete Skill removes it from the list", t3_6)

        # T3-7: create a model catalog entry
        CATALOG_SPEC = f"fake:model_{int(time.time())}"
        def t3_7():
            page.click(".advanced-kinds button:has-text('Model catalog')")
            time.sleep(0.3)
            page.fill(".advanced-new input", CATALOG_SPEC)
            page.click(".advanced-new button:has-text('New')")
            page.wait_for_selector(f".advanced-editor h2:has-text('{CATALOG_SPEC}')", timeout=4000)
            page.fill(".advanced-editor textarea", json.dumps({
                "display_name": "Test model",
                "provider": "fake",
                "tier": "economy",
                "input_cost_per_1m": 0,
                "output_cost_per_1m": 0
            }, indent=2))
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success", timeout=5000)
            page.wait_for_selector(f".advanced-list button:has-text('{CATALOG_SPEC}')", timeout=4000)
        run("T3-7 Create Model Catalog entry", t3_7)

        # T3-8: create workflow via Advanced and verify it appears in Monitor
        WF = f"wf_{int(time.time())}"
        def t3_8():
            page.click(".advanced-kinds button:has-text('Workflows')")
            time.sleep(0.3)
            page.fill(".advanced-new input", WF)
            page.click(".advanced-new button:has-text('New')")
            page.wait_for_selector(f".advanced-editor h2:has-text('{WF}')", timeout=4000)
            page.fill(".advanced-editor textarea", json.dumps({
                "name": WF,
                "teams": [{"name": "t", "mode": "sequential", "agents": ["a"]}],
                "agents": [{"name": "a", "role": "Asst", "goal": "Help",
                            "backstory": "Friendly AI assistant.", "model": "fake:Hello! How can I help?"}],
                "workflow": {"steps": ["t"]}
            }, indent=2))
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success, .banner-error", timeout=8000)
            err = page.locator(".banner-error")
            if err.is_visible():
                raise AssertionError(f"Backend rejected save: {err.inner_text()}")
            page.goto(BASE + "/")
            page.wait_for_selector("select", timeout=8000)
            opts = page.locator("select option").all_inner_texts()
            assert WF in opts, f"{WF} not found in Monitor dropdown"
        run("T3-8 Workflow created in Advanced appears in Monitor dropdown", t3_8)

        # ── T4 . Wizard ──────────────────────────────────────────────────────

        print("\n== T4 . Wizard ==")
        print("  [SKIP] T4-1..T4-7 (AI generation requires real LLM API key)")

        # ── T5 . Edge Cases ──────────────────────────────────────────────────

        print("\n== T5 . Edge Cases ==")

        # T5-1: clear token -> redirect to /login
        def t5_1():
            page.goto(BASE + "/")
            page.evaluate("localStorage.removeItem('bestteam_token')")
            page.goto(BASE + "/advanced")
            page.wait_for_url("**/login", timeout=5000)
        run("T5-1 Clearing token redirects to /login", t5_1)

        # Re-login
        login_ui(page, USER, PASS)

        # T5-2: saving workflow with existing name is an upsert (no duplicate in list)
        def t5_2():
            page.goto(BASE + "/advanced")
            page.wait_for_selector(".advanced-kinds", timeout=5000)
            page.click(".advanced-kinds button:has-text('Workflows')")
            time.sleep(0.3)
            page.fill(".advanced-new input", WF)
            page.click(".advanced-new button:has-text('New')")
            page.wait_for_selector(f".advanced-editor h2:has-text('{WF}')", timeout=4000)
            page.fill(".advanced-editor textarea", json.dumps({
                "name": WF,
                "teams": [{"name": "t", "mode": "sequential", "agents": ["a"]}],
                "agents": [{"name": "a", "role": "Asst", "goal": "Help",
                            "backstory": "Friendly AI assistant.", "model": "fake:upsert test response"}],
                "workflow": {"steps": ["t"]}
            }, indent=2))
            page.click(".advanced-editor button:has-text('Save')")
            page.wait_for_selector(".banner-success, .banner-error", timeout=8000)
            err = page.locator(".banner-error")
            if err.is_visible():
                raise AssertionError(f"Backend rejected upsert: {err.inner_text()}")
            count = page.locator(f".advanced-list button:has-text('{WF}')").count()
            assert count == 1, f"Expected 1 entry, got {count}"
        run("T5-2 Saving same workflow name is an upsert (not a duplicate)", t5_2)

        # T5-3: Run button disabled when textarea is empty
        def t5_3():
            page.goto(BASE + "/")
            page.wait_for_selector("select", timeout=8000)
            page.fill("textarea", "")
            btn = page.locator("button:has-text('Run')")
            assert btn.is_disabled(), "Run button should be disabled with empty input"
        run("T5-3 Run button disabled with empty input", t5_3)

        browser.close()

    # ── Summary ─────────────────────────────────────────────────────────────

    total = len(PASSED) + len(FAILED)
    skipped = 2  # T2-3 and T4 block
    print(f"\n{'='*55}")
    print(f"Results: {len(PASSED)}/{total} passed, {len(FAILED)} failed, {skipped} skipped")
    print(f"{'='*55}")
    if FAILED:
        print("\nFailed:")
        for f in FAILED:
            print(f"  [FAIL] {f}")
    else:
        print("All tests passed!")
    return len(FAILED) == 0


if __name__ == "__main__":
    success = test_all()
    sys.exit(0 if success else 1)
