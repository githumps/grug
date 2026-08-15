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
import os
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


def test_every_language_the_fleet_uses_has_at_least_one_rule():
    """Ansible was named in the ticket and silently missing from the first draft.

    Asserting "not python-only" was too weak - it passed the moment ONE yaml
    rule existed, which is exactly how Ansible got dropped while the file's own
    header still claimed to cover it.
    """
    langs = {lang for r in _rules() for lang in r.get("languages", [])}
    assert "yaml" in langs, "the YAML-heavy repos have no coverage"
    ids = " ".join(r["id"] for r in _rules())
    # k8s deliberately absent: iac_scan.py owns those and dispatch.py
    # concatenates both scanners without dedup, so a rule here would
    # double-report. See the header of yaml_ci_k8s.yml.
    for shape in ("gha", "ansible"):
        assert shape in ids, f"no rule targets {shape}-shaped YAML"


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
    "ansible-tls-disabled": (
        "ansible/roles/x/tasks/main.yml",
        "- name: fetch\n"
        "  ansible.builtin.uri:\n"
        "    url: https://example.invalid\n"
        "    validate_certs: no\n",
    ),
    "ansible-shell-interpolation": (
        "ansible/roles/x/tasks/run.yml",
        "- name: run\n"
        "  ansible.builtin.shell: rm -rf {{ target_dir }}\n",
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
            # LAYER onto os.environ rather than replacing it, matching
            # sast.scan_opengrep. A hardcoded PATH here would fail to find an
            # engine installed anywhere else - and the failure mode would be
            # "tests skip", i.e. silently green.
            env={**os.environ, "HOME": tmp, "XDG_CACHE_HOME": f"{tmp}/.cache",
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
    """Precision guard. A ruleset that flags everything gets switched off.

    The safe form of each rule, which must NOT fire: a workflow that routes the
    untrusted value through env: and references it as a quoted shell variable,
    and an Ansible task that leaves TLS verification on and uses an argv list
    rather than interpolating into a shell string.
    """
    clean = {
        ".github/workflows/ok.yml":
            "name: ok\non: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - env:\n          T: ${{ github.event.issue.title }}\n"
            "        run: echo \"$T\"\n",
        "ansible/roles/x/tasks/ok.yml":
            "- name: fetch\n  ansible.builtin.uri:\n"
            "    url: https://example.invalid\n    validate_certs: yes\n"
            "- name: run\n  ansible.builtin.command:\n    argv: [rm, -rf, /tmp/x]\n",
    }
    assert _scan(clean) == []
