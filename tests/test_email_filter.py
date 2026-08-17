"""The pure pre-LLM filter evaluator (email automation Phase 4a).

No mailbox, no database, no clock -- the same shape as test_trigger_health.py,
so every rule and every ordering decision is pinned exhaustively and cheaply.
"""

import pytest

from ui.backend.email_filter import FilterSettings, describe, evaluate

pytestmark = pytest.mark.unit


def _headers(**kwargs) -> dict:
    base = {"from": "Alice <alice@example.com>", "subject": "Quote request"}
    base.update(kwargs)
    return base


def test_an_ordinary_message_is_processed():
    assert evaluate(_headers(), FilterSettings()) is None


def test_default_settings_filter_bulk_mail():
    # skip_bulk defaults on: the phase exists because customers are billed for
    # bulk mail, and a safety feature nobody switches on protects nobody.
    assert evaluate(_headers(**{"list-id": "<news.example.com>"}), FilterSettings()) == "bulk:list-id"


@pytest.mark.parametrize(
    "header,value",
    [
        ("auto-submitted", "auto-generated"),
        ("precedence", "bulk"),
        ("list-id", "<news.example.com>"),
        ("list-unsubscribe", "<https://example.com/u>"),
    ],
)
def test_each_bulk_header_is_recognised(header, value):
    assert evaluate(_headers(**{header: value}), FilterSettings()) == f"bulk:{header}"


def test_auto_submitted_none_is_not_bulk():
    # RFC 3834: "no" is spelled `none`, and every ordinary message that sets
    # the header at all sets it to that. Treating it as bulk would filter
    # normal mail.
    assert evaluate(_headers(**{"auto-submitted": "none"}), FilterSettings()) is None


def test_bulk_is_not_filtered_when_the_admin_switches_it_off():
    settings = FilterSettings(skip_bulk=False)
    assert evaluate(_headers(**{"precedence": "bulk"}), settings) is None


def test_a_blocked_sender_is_filtered():
    settings = FilterSettings(sender_blocklist=("alice@example.com",))
    assert evaluate(_headers(), settings) == "blocked_sender:alice@example.com"


def test_a_domain_wildcard_blocks_the_whole_domain():
    settings = FilterSettings(sender_blocklist=("*@example.com",))
    assert evaluate(_headers(), settings) == "blocked_sender:*@example.com"


def test_pattern_matching_ignores_case_on_both_sides():
    settings = FilterSettings(sender_blocklist=("*@EXAMPLE.com",))
    assert evaluate(_headers(**{"from": "ALICE@Example.COM"}), settings) is not None


def test_matching_uses_the_address_not_the_display_name():
    # The display name is attacker-chosen free text. Matching on it would let a
    # sender slip a blocklist, or forge past an allowlist, by typing anything.
    settings = FilterSettings(sender_blocklist=("*@spam.test",))
    headers = _headers(**{"from": '"noreply@spam.test" <real@example.com>'})
    assert evaluate(headers, settings) is None


def test_an_allowlist_filters_everyone_not_on_it():
    settings = FilterSettings(sender_allowlist=("*@client.test",))
    assert evaluate(_headers(), settings) == "not_allowlisted"


def test_an_allowlisted_sender_is_processed():
    settings = FilterSettings(sender_allowlist=("*@example.com",))
    assert evaluate(_headers(), settings) is None


def test_an_empty_allowlist_allows_everyone():
    assert evaluate(_headers(), FilterSettings(sender_allowlist=())) is None


def test_the_blocklist_outranks_the_allowlist():
    # "never this sender" must not be silently overridden by a broader
    # "anyone at this domain".
    settings = FilterSettings(
        sender_blocklist=("alice@example.com",), sender_allowlist=("*@example.com",)
    )
    assert evaluate(_headers(), settings) == "blocked_sender:alice@example.com"


def test_the_allowlist_does_not_exempt_a_sender_from_the_bulk_check():
    # An allowlisted domain that starts sending a newsletter is still sending a
    # newsletter. The admin who wants it anyway unticks skip_bulk.
    settings = FilterSettings(sender_allowlist=("*@example.com",))
    headers = _headers(**{"precedence": "bulk"})
    assert evaluate(headers, settings) == "bulk:precedence"


def test_a_blocked_subject_term_is_filtered():
    settings = FilterSettings(subject_blocklist=("unsubscribe",))
    headers = _headers(subject="Please UNSUBSCRIBE me")
    assert evaluate(headers, settings) == "blocked_subject:unsubscribe"


def test_the_subject_blocklist_matches_substrings_case_insensitively():
    settings = FilterSettings(subject_blocklist=("OUT OF OFFICE",))
    headers = _headers(subject="Re: out of office until Monday")
    assert evaluate(headers, settings) == "blocked_subject:OUT OF OFFICE"


def test_a_blocked_sender_outranks_a_blocked_subject():
    settings = FilterSettings(
        sender_blocklist=("alice@example.com",), subject_blocklist=("quote",)
    )
    assert evaluate(_headers(), settings) == "blocked_sender:alice@example.com"


def test_a_missing_from_header_is_processed():
    # Fail open: a malformed header is not evidence the message is junk.
    settings = FilterSettings(sender_blocklist=("*@example.com",))
    assert evaluate({"subject": "hi"}, settings) is None


def test_an_unparseable_from_header_is_processed():
    settings = FilterSettings(sender_allowlist=("*@client.test",))
    # No address at all to compare against -> allowlist cannot judge it.
    assert evaluate({"from": "not an address", "subject": "hi"}, settings) is None


def test_a_missing_subject_header_is_processed():
    settings = FilterSettings(subject_blocklist=("quote",))
    assert evaluate({"from": "alice@example.com"}, settings) is None


def test_header_lookup_is_case_insensitive():
    # IMAP hands headers back however the sender cased them.
    assert evaluate({"From": "a@b.test", "List-Id": "<x>"}, FilterSettings()) == "bulk:list-id"


def test_every_decision_has_a_sentence():
    decisions = [
        "bulk:list-id",
        "bulk:precedence",
        "blocked_sender:*@x.test",
        "blocked_subject:quote",
        "not_allowlisted",
    ]
    for decision in decisions:
        sentence = describe(decision)
        assert sentence and not sentence.startswith("Skipped: bulk:")


def test_an_unknown_decision_still_describes_itself():
    # A row written by a future version must not render as a blank cell.
    assert describe("something_new") == "Skipped: something_new"
