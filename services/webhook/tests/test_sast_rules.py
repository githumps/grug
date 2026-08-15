"""Tests for the vendored SAST ruleset itself, not the runner around it.

WHY A SEPARATE FILE. test_sast.py mocks the engine subprocess, so it proves the
RUNNER handles output correctly and says nothing about whether the rules match
anything. Those are different failures: on 2026-08-15 the engine ran correctly,
exited 0, and found nothing outside Python, because the entire ruleset was one
file of ten Python rules. Every runner test passed throughout.

Two layers here:

  1. Structural checks that ALWAYS run - ids unique, `metadata.vuln_class`
     present (the runner DROPS any finding without one, so a rule missing it is
     silently dead), languages declared.
  2. Engine-backed checks against planted vulnerabilities, skipped when no
     engine is on PATH. These are the ones that would have caught the real gap,
     because a rule can be perfectly well-formed and match nothing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

RULES_DIR = Path(__file__).resolve().parents[1] / "sast_rules"
ENGINE = shutil.which("opengrep") or shutil.which("semgrep")


def _rules() -> list[dict]:
    out: list[dict] = []
    for f in sorted(RULES_DIR.glob("*.yml")) + sorted(RULES_DIR.glob("*.yaml")):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        out.extend(doc.get("rules", []))
    return out


# --- structural: always run ----------------------------------------------

def test_every_rule_declares_a_vuln_class():
    """The runner drops any finding whose rule has no metadata.vuln_class.

    A rule missing it is not a degraded rule, it is an invisible one - it
    matches, produces a result, and the mapper discards it without a log.
    """
    missing = [r["id"] for r in _rules()
               if not (r.get("metadata") or {}).get("vuln_class")]
    assert missing == [], f"rules with no vuln_class will be silently dropped: {missing}"


def test_rule_ids_are_unique():
    ids = [r["id"] for r in _rules()]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert dupes == set(), f"duplicate rule ids: {dupes}"


def test_every_rule_declares_languages():
    missing = [r["id"] for r in _rules() if not r.get("languages")]
    assert missing == [], f"rules with no languages: {missing}"


def test_ruleset_covers_more_than_python():
    """The gap that started #859.

    The infrastructure this reviews is mostly YAML - CI workflows, k8s
    manifests, Ansible. A Python-only ruleset reports a SAST capability while
    covering none of it.
    """
    langs = {lang for r in _rules() for lang in r.get("languages", [])}
    assert langs - {"python"}, (
        f"ruleset only covers {langs}; the YAML-heavy repos have no coverage"
    )


# --- engine-backed: the checks that would have caught the real gap --------

PLANTED = {
    "workflow-injection": (
        ".github/workflows/x.yml",
        "name: x\n"
        "on: [issues]\n"
        "jobs:\n"
        "  a:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo \"${{ github.event.issue.title }}\"\n",
    ),
    "k8s-privileged": (
        "k8s/x.yaml",
        "apiVersion: v1\n"
        "kind: Pod\n"
        "spec:\n"
        "  containers:\n"
        "    - name: c\n"
        "      image: nginx\n"
        "      securityContext:\n"
        "        privileged: true\n",
    ),
    "k8s-privilege-escalation": (
        "k8s/y.yaml",
        "apiVersion: v1\n"
        "kind: Pod\n"
        "spec:\n"
        "  containers:\n"
        "    - name: c\n"
        "      image: nginx\n"
        "      securityContext:\n"
        "        allowPrivilegeEscalation: true\n",
    ),
}


def _scan(files: dict[str, str]) -> list[str]:
    """Run the real engine over the real rules; return matched rule ids."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, body in files.items():
            p = Path(tmp) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        proc = subprocess.run(
            [ENGINE, "scan", "--config", str(RULES_DIR), "--json", "--quiet",
             "--disable-version-check", "--no-rewrite-rule-ids", tmp],
            capture_output=True, text=True, check=False,
            env={"HOME": tmp, "XDG_CACHE_HOME": f"{tmp}/.cache", "PATH": "/usr/bin:/bin:/usr/local/bin",
                 "PYTHONUTF8": "1", "LANG": "C.UTF-8"},
        )
        if proc.returncode != 0:
            pytest.fail(f"engine exited {proc.returncode}: {proc.stderr[-800:]}")
        data = json.loads(proc.stdout)
    return [r["check_id"].rsplit(".", 1)[-1] for r in data.get("results", [])]


@pytest.mark.skipif(ENGINE is None, reason="no opengrep/semgrep on PATH")
@pytest.mark.parametrize("name", sorted(PLANTED))
def test_planted_vulnerability_is_caught(name: str):
    rel, body = PLANTED[name]
    matched = _scan({rel: body})
    assert matched, f"planted {name} in {rel} matched NO rule"


@pytest.mark.skipif(ENGINE is None, reason="no opengrep/semgrep on PATH")
def test_clean_files_produce_no_findings():
    """Precision guard. A ruleset that flags everything gets switched off."""
    clean = {
        ".github/workflows/ok.yml":
            "name: ok\non: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - env:\n          T: ${{ github.event.issue.title }}\n"
            "        run: echo \"$T\"\n",
        "k8s/ok.yaml":
            "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n    - name: c\n"
            "      image: nginx\n      securityContext:\n"
            "        privileged: false\n        allowPrivilegeEscalation: false\n",
    }
    assert _scan(clean) == []
