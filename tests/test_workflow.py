from io import BytesIO
import json
from pathlib import Path
import urllib.request

import pytest
import yaml

WORKFLOW = Path("integrations/github/ai-review.yml")


def test_workflow_structure():
    data = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert set(data["on"]["pull_request"]["types"]) == {"opened", "synchronize", "reopened", "ready_for_review"}
    assert "pull_request_target" not in data["on"]
    gate, review = data["jobs"]["gate"], data["jobs"]["review"]
    assert gate["runs-on"] == "ubuntu-latest"
    assert "head.repo.full_name" in gate["if"] and "base.repo.full_name" in gate["if"] and "user.login" in gate["if"]
    assert review["runs-on"] == ["self-hosted", "ai-review"] and review["needs"] == "gate"
    assert "outputs.allowed" in review["if"]
    assert review["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert review["concurrency"]["cancel-in-progress"] == "true"
    text = WORKFLOW.read_text()
    assert "actions/checkout" not in text and "yarn " not in text and "pip install" not in text
    assert "github.event.pull_request.title" not in text
    assert '"$ASSISTANT_BIN"' in text and '"$EXPECTED_HEAD"' in text


@pytest.mark.parametrize("event_name,author,head_repo,draft,expected", [
    ("workflow_dispatch", "metalice80", "metalice80/outline_t", False, True),
    ("pull_request", "metalice80", "metalice80/outline_t", False, True),
    ("workflow_dispatch", "attacker", "metalice80/outline_t", False, False),
    ("pull_request", "metalice80", "attacker/outline", False, False),
    ("pull_request", "metalice80", "metalice80/outline_t", True, False),
])
def test_gate_without_pr_payload_and_untrusted_inputs(tmp_path, monkeypatch, event_name, author, head_repo, draft, expected):
    data = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    block = data["jobs"]["gate"]["steps"][0]["run"]
    script = "\n".join(block.splitlines()[1:-1])
    payload = {} if event_name == "workflow_dispatch" else {"pull_request": {"number": 7, "head": {"sha": "a" * 40}}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
    output, summary = tmp_path / "output", tmp_path / "summary"
    env = {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_EVENT_NAME": event_name, "INPUT_NUMBER": "7", "GH_TOKEN": "fake", "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
    for key, value in env.items(): monkeypatch.setenv(key, value)
    pr = {"state": "open", "draft": draft, "user": {"login": author}, "head": {"sha": "a" * 40, "repo": {"full_name": head_repo}}, "base": {"repo": {"full_name": "metalice80/outline_t"}}}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: BytesIO(json.dumps(pr).encode()))
    exec(compile(script, "workflow-gate", "exec"), {})
    assert f"allowed={str(expected).lower()}" in output.read_text()


def test_runner_context_is_resolved_inside_step():
    workflow = yaml.load(Path('integrations/github/ai-review.yml').read_text(), Loader=yaml.BaseLoader)
    job = workflow['jobs']['review']
    assert all('runner.' not in value for value in job['env'].values())
    prepare = job['steps'][0]['run']
    assert '$RUNNER_TEMP' in prepare and 'OUTPUT_DIR=%s' in prepare and '$GITHUB_ENV' in prepare
