"""The same failure must produce the same signature, a different one must not."""

from app.evidence.signature import error_lines, failure_signature, normalize_line
from app.integrations.github.client import JobLog
from tests.test_analysis import make_evidence

NPM = (
    "##[group]Run npm install\n"
    "npm error code ERESOLVE\n"
    "npm error ERESOLVE unable to resolve dependency tree\n"
    "npm error While resolving: web@1.0.0\n"
    "##[error]Process completed with exit code 1.\n"
)
# Same problem, different run: other commit, other durations, other temp paths.
NPM_LATER = (
    "##[group]Run npm install\n"
    "npm error code ERESOLVE\n"
    "npm error ERESOLVE unable to resolve dependency tree\n"
    "npm error While resolving: web@2.4.1\n"
    "##[error]Process completed with exit code 1.\n"
)
TEST_FAILURE = (
    "not ok 2 - 10% off 200 is 180\n"
    "  expected: 180\n"
    "  actual: 190\n"
    "##[error]Process completed with exit code 1.\n"
)


def signature_for(log_text: str) -> str:
    return failure_signature(make_evidence(JobLog(log_text, len(log_text), False)))


def test_the_same_failure_gives_the_same_signature() -> None:
    assert signature_for(NPM) == signature_for(NPM)
    assert signature_for(NPM) == signature_for(NPM_LATER)  # versions and numbers normalised away


def test_a_different_failure_gives_a_different_signature() -> None:
    assert signature_for(NPM) != signature_for(TEST_FAILURE)


def test_signature_covers_repository_workflow_and_stage() -> None:
    evidence = make_evidence(JobLog(NPM, len(NPM), False))
    other_repo = evidence.model_copy(update={"repository": evidence.repository.model_copy(update={"full_name": "other/repo"})})
    other_stage = evidence.model_copy(update={"failed_stage": "build / Compile"})

    assert failure_signature(evidence) != failure_signature(other_repo)
    assert failure_signature(evidence) != failure_signature(other_stage)


def test_markers_and_short_lines_are_ignored() -> None:
    assert error_lines(make_evidence(JobLog(NPM, len(NPM), False))) == [
        "npm error code eresolve",
        "npm error eresolve unable to resolve dependency tree",
        "npm error while resolving: web@#.#.#",
    ]


def test_normalize_line_removes_what_changes_between_runs() -> None:
    assert normalize_line("  Failed after 1234ms at /home/runner/work/abc123def/x.js  ") == "failed after #ms at #"
    assert normalize_line("Error: connect ECONNREFUSED 10.0.0.5:5432") == "error: connect econnrefused #.#.#.#:#"


def test_a_run_without_failed_jobs_still_has_a_signature() -> None:
    evidence = make_evidence().model_copy(update={"failed_jobs": []})
    assert len(failure_signature(evidence)) == 16
