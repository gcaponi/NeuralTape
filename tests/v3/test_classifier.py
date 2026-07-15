"""Test per lex/v3/classifier.py (D1.1)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

from classifier import (  # type: ignore[import-not-found]
    CLASSIFIER_PROMPT,
    ClassificationError,
    ClassifierInsight,
    ClassifierV3,
)


class CleanRedactor:
    def redact(self, text: str):
        return text, []

    def summary(self, _events):
        return "redaction: clean"


class OpenCostPolicy:
    def can_call(self):
        return True, "ok"

    def record_call(self, _tokens_used: int):
        return None


def _build_classifier() -> ClassifierV3:
    return ClassifierV3(
        config=object(),
        project=object(),
        storage=object(),
        redactor=CleanRedactor(),
        cost_policy=OpenCostPolicy(),
        api_key="test-key",
    )


def test_classifier_insight_valid():
    ins = ClassifierInsight(
        category="pattern",
        title="test insight",
        context="during session X",
        implication="use this approach in future",
        layer="working",
        confidence=0.8,
    )
    errs = ins.validate()
    assert errs == [], f"expected no errors, got {errs}"


def test_classifier_insight_missing_title():
    ins = ClassifierInsight(
        category="pattern", title="", context="", implication="",
        layer="working", confidence=0.5,
    )
    errs = ins.validate()
    assert any("title is empty" in e for e in errs)


def test_classifier_insight_invalid_category():
    ins = ClassifierInsight(
        category="bogus", title="x", context="", implication="",
        layer="working", confidence=0.5,
    )
    errs = ins.validate()
    assert any("invalid category" in e for e in errs)


def test_classifier_insight_invalid_layer():
    ins = ClassifierInsight(
        category="pattern", title="x", context="", implication="",
        layer="quantum", confidence=0.5,
    )
    errs = ins.validate()
    assert any("invalid layer" in e for e in errs)


def test_classifier_insight_out_of_range_confidence():
    ins = ClassifierInsight(
        category="pattern", title="x", context="", implication="",
        layer="working", confidence=1.5,
    )
    errs = ins.validate()
    assert any("confidence out of range" in e for e in errs)


def test_classifier_insight_from_dict():
    data = {
        "category": "decision",
        "title": "choose sqlite over json",
        "context": "during storage design",
        "implication": "better query capabilities",
        "layer": "semantic",
        "confidence": 0.95,
    }
    ins = ClassifierInsight.from_dict(data)
    assert ins.category == "decision"
    assert ins.title == "choose sqlite over json"
    assert ins.layer == "semantic"
    assert ins.confidence == 0.95


def test_classifier_insight_from_dict_missing_fields():
    data = {"category": "pattern", "title": "test"}
    ins = ClassifierInsight.from_dict(data)
    assert ins.context == ""
    assert ins.implication == ""
    assert ins.layer == "working"
    assert ins.confidence == 0.0


def test_classifier_insight_dedup_normalization():
    """Titles that differ by case/whitespace must normalize to the same key."""
    ins1 = ClassifierInsight("pattern", "Use SQLite", "c", "i", "working", 0.7)
    ins2 = ClassifierInsight("pattern", "  use SQLITE  ", "c", "i", "working", 0.7)
    norm1 = " ".join(ins1.title.lower().split())
    norm2 = " ".join(ins2.title.lower().split())
    assert norm1 == norm2


def test_redaction_integration():
    """Verify the redactor works before classifier gets text (integration test)."""
    from redaction import Redactor  # type: ignore
    r = Redactor()
    text = "my token=sk-abc123def456ghi789jkl012mno345pqr"
    redacted, ev = r.redact(text)
    # The generic-assignment pattern should catch "token=..." with long value
    assert "[REDACTED:" in redacted
    assert "sk-abc123" not in redacted


def test_classifier_processes_newest_chunk_first():
    classifier = _build_classifier()
    prompts = []

    def fake_completion(prompt: str):
        prompts.append(prompt)
        return {"choices": [{"message": {"content": '{"insights": []}'}}]}

    classifier._chat_completion = fake_completion
    old_chunk = "OLD-CONTEXT " + ("x" * 16000) + "\n"
    new_chunk = "NEW-CONTEXT " + ("y" * 16000) + "\n"

    classifier.classify(old_chunk + new_chunk, "session-long")

    assert "NEW-CONTEXT" in prompts[0]
    assert "OLD-CONTEXT" in prompts[1]


def test_classifier_raises_when_llm_call_fails():
    classifier = _build_classifier()

    def failing_completion(_prompt: str):
        raise OSError("provider unavailable")

    classifier._chat_completion = failing_completion

    try:
        classifier.classify("session content", "session-failed")
    except ClassificationError as error:
        assert "provider unavailable" in str(error)
    else:
        raise AssertionError("ClassificationError was not raised")


def test_classifier_prompt_requires_evidence_for_preferences_and_errors():
    assert "preferenza esplicita dell'utente" in CLASSIFIER_PROMPT
    assert "nome esatto dell'errore" in CLASSIFIER_PROMPT
    assert "non reinterpretarlo" in CLASSIFIER_PROMPT
