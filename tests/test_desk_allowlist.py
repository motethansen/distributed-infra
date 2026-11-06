import pytest
from pydantic import ValidationError

from shared.desk_allowlist import (ALLOWED_TIERS_THIS_SPRINT, ALLOWLIST, check_verb, is_valid_id, missing_required,
                                   tier_of)
from shared.desk_intent import CommandIntent, intent_missing, intent_verdict

TIERS = {
    "T1": ["refresh_proposal", "plan", "validate", "step_done"],
    "T2": ["approve", "go", "po_request", "po_revise", "accept"],
    "T3": ["merge", "deploy", "cancel", "close"],
}
BASE = dict(kind="command", project="vantage", sprint="2", actor="mike")


@pytest.mark.parametrize("tier,verb", [(t, v) for t, vs in TIERS.items() for v in vs])
def test_verb_tier(tier, verb):
    assert tier_of(verb) == tier
    status = {"T1": "ok", "T2": "confirm", "T3": "blocked"}[tier]
    assert check_verb(verb) == (status, tier)


def test_thirteen_verbs_and_sprint_tiers():
    assert len(ALLOWLIST) == 13
    assert ALLOWED_TIERS_THIS_SPRINT == {"T1"}


@pytest.mark.parametrize("verb", ["rm", "", "PLAN", None, 5])
def test_unknown_verb(verb):
    assert tier_of(verb) is None
    with pytest.raises(ValueError):
        check_verb(verb)
    assert missing_required(verb, {}) == []


def test_step_done_evidence():
    assert missing_required("step_done", {}) == ["evidence"]
    assert missing_required("step_done", {"evidence": None}) == ["evidence"]
    assert missing_required("step_done", {"evidence": ""}) == ["evidence"]
    assert missing_required("step_done", {"evidence": " \t\n"}) == ["evidence"]
    assert missing_required("step_done", {"evidence": "tests pass"}) == []
    assert missing_required("plan", {}) == []


@pytest.mark.parametrize("v", ["a", "S-8", "v1.2_x", "A" * 64])
def test_id_accept(v):
    assert is_valid_id(v)


@pytest.mark.parametrize("v", ["", "A" * 65, "; rm -rf", "../x", "a/b", "a b", "ok\n", None, 3])
def test_id_reject(v):
    assert not is_valid_id(v)


def test_intent_ok_and_helpers():
    i = CommandIntent(**BASE, verb="step_done", story_id="S-8")
    assert intent_verdict(i) == ("ok", "T1")
    assert intent_missing(i) == ["evidence"]
    assert intent_missing(i.model_copy(update={"evidence": "done"})) == []


@pytest.mark.parametrize("field", ["project", "sprint", "story_id", "actor"])
@pytest.mark.parametrize("bad", ["; rm -rf", "../x"])
def test_intent_rejects_bad_ids(field, bad):
    with pytest.raises(ValidationError):
        CommandIntent(**{**BASE, "verb": "plan", field: bad})


@pytest.mark.parametrize("extra", ["command", "args"])
def test_intent_rejects_extras(extra):
    with pytest.raises(ValidationError):
        CommandIntent(**BASE, verb="plan", **{extra: "ls"})


def test_intent_does_not_check_verb_membership():
    i = CommandIntent(**BASE, verb="nonsense")
    with pytest.raises(ValueError):
        intent_verdict(i)
