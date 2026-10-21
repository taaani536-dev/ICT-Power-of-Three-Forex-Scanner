"""Static validation of the workflow + integration test of the git state-branch script."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows" / "forex_scanner.yml"


def wf():
    return yaml.safe_load(WF.read_text())


def test_workflow_yaml_parses_with_required_triggers():
    d = wf()
    on = d.get("on", d.get(True))
    assert on["schedule"][0]["cron"] == "7 * * * *"          # hourly
    assert "workflow_dispatch" in on
    assert "dry-run" in on["workflow_dispatch"]["inputs"]["mode"]["options"]


def test_workflow_concurrency_permissions_timeouts():
    d = wf()
    assert d["concurrency"] == {"group": "forex-scanner", "cancel-in-progress": False}
    assert d["permissions"] == {"contents": "write"}
    job = d["jobs"]["scan"]
    assert job["timeout-minutes"] > next(s for s in job["steps"] if s.get("id") == "run")["timeout-minutes"]


def test_workflow_secret_env_names_match_code():
    env = wf()["jobs"]["scan"]["env"]
    for name in ("TWELVE_DATA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        assert env[name] == "${{ secrets.%s }}" % name
    src = (ROOT / "src" / "config.py").read_text()
    for name in env:
        if name.isupper() and name not in ("PYTHONUNBUFFERED", "STATE_PATH"):
            assert name in src


def test_workflow_persist_step_always_runs_and_files_exist():
    steps = wf()["jobs"]["scan"]["steps"]
    persist = steps[-1]
    assert "always()" in persist["if"] and "state_branch.sh persist" in persist["run"]
    text = WF.read_text()
    for rel in re.findall(r"(scripts/\S+\.sh|main\.py|requirements\.txt)", text):
        assert (ROOT / rel).exists(), rel
    assert "pip install -r requirements.txt" in text


def test_requirements_are_pinned():
    for f in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / f).read_text().splitlines():
            if line.strip() and not line.startswith("-r"):
                assert re.fullmatch(r"[A-Za-z0-9_.\-]+==\d[\w.]*", line.strip()), line


def test_no_secrets_committed():
    pat = re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")
    for p in ROOT.rglob("*"):
        if p.is_file() and p.suffix in {".py", ".md", ".yml", ".txt", ".sh", ".example"} and ".git" not in p.parts:
            assert not pat.search(p.read_text(errors="ignore")), p
    assert not (ROOT / ".env").exists()


def test_excluded_instruments_not_present_in_source():
    for p in (ROOT / "src").glob("*.py"):
        t = p.read_text()
        if p.name == "symbol_mapping.py":
            continue
        assert "XAUUSD" not in t and "NAS100" not in t and "BTCUSD" not in t


# ------------------------------------------------ git state-branch script
git = shutil.which("git")


def sh(cmd, cwd, env=None, check=True):
    e = dict(os.environ, GIT_TERMINAL_PROMPT="0", PUSH_RETRY_SLEEP="0", **(env or {}))
    return subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, check=check, env=e)


def mk_state(signals):
    return {"schema_version": 1, "updated_at": None, "symbols": {}, "api_usage": {"date": None, "credits": 0},
            "signals": {s: {"id": s, "symbol": "EURUSD", "direction": "BUY", "candle_ts": "2026-03-05T10:00:00Z",
                            "detected_at": "2026-03-05T10:00:00Z", "status": st, "attempts": 0, "signal": {}}
                        for s, st in signals.items()}}


@pytest.mark.skipif(git is None, reason="git not installed")
def test_state_branch_prepare_persist_and_conflict_merge(tmp_path):
    remote = tmp_path / "remote.git"
    sh(f"git init -q --bare -b main {remote}", tmp_path)
    seed = tmp_path / "seed"
    shutil.copytree(ROOT, seed, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".state"))
    sh("git init -q -b main && git add -A && git -c user.name=t -c user.email=t@t commit -q -m seed "
       f"&& git remote add origin {remote} && git push -q origin main", seed)

    def clone(name):
        d = tmp_path / name
        sh(f"git clone -q {remote} {d}", tmp_path)
        return d

    a, b = clone("a"), clone("b")
    for d in (a, b):
        sh("bash scripts/state_branch.sh prepare", d)                    # both create the orphan branch (race)
        assert (d / ".state").is_dir()
    (a / ".state" / "state.json").write_text(json.dumps(mk_state({"sigA": "sent", "shared": "pending"})))
    (b / ".state" / "state.json").write_text(json.dumps(mk_state({"sigB": "pending", "shared": "sent"})))
    sh("bash scripts/state_branch.sh persist", a)
    r = sh("bash scripts/state_branch.sh persist", b)                    # rejected -> fetch -> merge -> push
    assert "merging" in r.stdout
    final = sh(f"git --git-dir={remote} show scanner-state:state.json", tmp_path).stdout
    sigs = json.loads(final)["signals"]
    assert set(sigs) == {"sigA", "sigB", "shared"} and sigs["shared"]["status"] == "sent"

    # sequential normal flow on an existing branch (fast-forward)
    c = clone("c")
    sh("bash scripts/state_branch.sh prepare", c)
    st = json.loads((c / ".state" / "state.json").read_text())
    assert set(st["signals"]) == {"sigA", "sigB", "shared"}
    st["signals"]["sigC"] = mk_state({"sigC": "pending"})["signals"]["sigC"]
    (c / ".state" / "state.json").write_text(json.dumps(st))
    sh("bash scripts/state_branch.sh persist", c)
    again = sh("bash scripts/state_branch.sh persist", c)                # unchanged -> no-op
    assert "unchanged" in again.stdout
    assert "sigC" in sh(f"git --git-dir={remote} show scanner-state:state.json", tmp_path).stdout
