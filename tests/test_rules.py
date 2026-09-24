"""Team rules: known failures answered from the repository's own .devinvestigator.yml."""

from app.analysis.rules import MAX_CONFIDENCE, load_rules, match_rule, rule_analysis
from app.integrations.github.client import JobLog
from tests.test_analysis import make_evidence

RULES_FILE = """
rules:
  - name: Test database was not ready
    match: "ECONNREFUSED"
    category: infrastructure_failure
    root_cause: The test database container was not ready.
    fix: Re-run the job, then add a health check.
    confidence: 0.85
  - name: npm peer dependency conflict
    match: "ERESOLVE unable to resolve dependency tree"
    category: dependency_failure
    root_cause: Two packages need incompatible versions.
    fix: Align the versions in package.json.
"""

NPM_LOG = (
    "2026-09-23T12:00:00.0Z npm error code ERESOLVE\n"
    "2026-09-23T12:00:00.0Z npm error ERESOLVE unable to resolve dependency tree\n"
    "2026-09-23T12:00:00.0Z ##[error]Process completed with exit code 1.\n"
)


def evidence_with(log_text: str):
    return make_evidence(JobLog(log_text, len(log_text), False))


def test_rules_are_parsed() -> None:
    rules = load_rules(RULES_FILE)
    assert [rule.name for rule in rules] == ["Test database was not ready", "npm peer dependency conflict"]
    assert rules[0].category == "infrastructure_failure" and rules[0].confidence == 0.85
    assert rules[1].confidence == 0.8  # default


def test_a_rule_matches_the_failing_log_and_keeps_the_matched_line() -> None:
    matched = match_rule(load_rules(RULES_FILE), evidence_with(NPM_LOG))
    assert matched is not None
    rule, line = matched
    assert rule.name == "npm peer dependency conflict"
    assert line == "npm error ERESOLVE unable to resolve dependency tree"  # timestamp stripped


def test_no_rule_matches_an_unrelated_failure() -> None:
    assert match_rule(load_rules(RULES_FILE), evidence_with("Segmentation fault\n")) is None


def test_matching_ignores_capitalisation() -> None:
    assert match_rule(load_rules(RULES_FILE), evidence_with("Error: connect econnrefused 10.0.0.5:5432\n")) is not None


def test_broken_yaml_and_broken_rules_are_skipped() -> None:
    assert load_rules("rules: [") == []
    assert load_rules("just a string") == []
    assert load_rules("rules:\n  - name: incomplete\n") == []  # missing match/root_cause/fix
    partial = load_rules("rules:\n  - name: bad\n  - name: ok\n    match: boom\n    root_cause: r\n    fix: f\n")
    assert [rule.name for rule in partial] == ["ok"]


def test_rule_confidence_is_capped() -> None:
    [rule] = load_rules("rules:\n  - name: n\n    match: boom\n    root_cause: r\n    fix: f\n    confidence: 1.0\n")
    assert rule.confidence == MAX_CONFIDENCE


def test_the_analysis_cites_the_matched_line() -> None:
    rule, line = match_rule(load_rules(RULES_FILE), evidence_with(NPM_LOG))
    analysis = rule_analysis(rule, line)

    assert analysis.category == "dependency_failure"
    assert analysis.root_cause == "Two packages need incompatible versions."
    assert analysis.recommendation == "Align the versions in package.json."
    assert [item.quote for item in analysis.evidence] == [line]
    assert "npm peer dependency conflict" in analysis.summary


def test_the_bundled_example_file_is_valid() -> None:
    from pathlib import Path

    rules = load_rules(Path("examples/devinvestigator.yml").read_text())
    assert len(rules) == 3
    assert match_rule(rules, evidence_with(NPM_LOG))[0].name == "npm peer dependency conflict"
