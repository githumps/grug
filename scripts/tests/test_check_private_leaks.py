"""Tests for the public-repo leak guard.

EVERY value below is INVENTED. A test for a leak guard is the last place a
real hostname or address should appear - the first draft of this file used
genuine ones and the hook correctly refused the commit.

The cases below are the ACTUAL leaks from 2026-08-15, generalised. Each one
reached a public commit, issue or PR body, and none was caught by secret
scanning, CodeQL, or human review - because none of them is secret-shaped.

The false-positive cases matter just as much. A guard that fires on ordinary
prose gets bypassed with --no-verify within a day, at which point it protects
nothing. 91 of the first 99 hits on the existing tree were prose like
"Post-#77", which is this repo's own issue after a hyphenated word.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_private_leaks import compiled, scan  # noqa: E402


def _hits(text: str, diff: bool = False) -> list[str]:
    return scan(text, compiled(), label="t", diff_mode=diff)


def _names(text: str, diff: bool = False) -> set[str]:
    out = set()
    for h in _hits(text, diff):
        out.add(h.split("[", 1)[1].split("]", 1)[0])
    return out


# --- the real leaks -------------------------------------------------------

def test_catches_cluster_node_name():
    assert "cluster-node" in _names("deployed to k8s-examplecluster-zone-worker-01 today")  # leak-guard-allow: invented fixture, not a real host


def test_catches_tailnet_hostname():
    assert "tailnet-host" in _names("POST https://sparks.ts.example.me/v1/models")  # leak-guard-allow: invented fixture, not a real host


def test_catches_tailscale_cgnat_ip():
    assert "tailscale-ip" in _names("the host answers on 100.90.11.22")  # leak-guard-allow: invented fixture, not a real host


def test_catches_rfc1918_ip():
    assert "rfc1918-ip" in _names("LAN node is 10.9.9.9")  # leak-guard-allow: invented fixture, not a real host
    assert "rfc1918-ip" in _names("router at 192.168.1.1")  # leak-guard-allow: invented fixture, not a real host


def test_catches_internal_server_hostname():
    assert "server-hostname" in _names("ssh root@srv-example-gpu failed")  # leak-guard-allow: invented fixture, not a real host
    assert "server-hostname" in _names("usr-someone-mbp reported installed=none")  # leak-guard-allow: invented fixture, not a real host


def test_catches_cross_repo_issue_reference():
    assert "private-issue-ref" in _names("tracked in someprivaterepo#4242")  # leak-guard-allow: invented fixture, not a real host


# --- the false positives that would get the guard disabled ----------------

def test_ignores_this_repos_own_issue_refs():
    assert _names("fixed in #101 and #102") == set()


def test_ignores_hyphenated_prose_before_an_issue_ref():
    """"Post-#77" is a hyphenated word then THIS repo's issue, not a repo ref.

    This exact shape was 91 of the first 99 hits against the existing tree.
    """
    assert _names("Post-#111 the loader changed") == set()
    assert _names("pre-#111 behaviour was different") == set()


def test_ignores_ordinary_public_addresses():
    assert _names("see https://github.com/opengrep/opengrep/releases") == set()
    assert _names("resolves via 8.8.8.8 upstream") == set()


def test_ignores_semver_and_ordinary_numbers():
    assert _names("bumped to 9.9.9 and 8.8.7") == set()


def test_allow_marker_suppresses_a_line():
    text = "host is srv-example-gpu  # leak-guard-allow: illustrative only"
    assert _names(text) == set()


# --- diff semantics -------------------------------------------------------

def test_only_added_lines_are_scanned_in_a_diff():
    """A REMOVED private identifier is a scrub, not a leak - never block it."""
    diff = (
        "--- a/x.md\n"
        "+++ b/x.md\n"
        "-old host was srv-example-gpu\n"  # leak-guard-allow: invented fixture, not a real host
        "+now describes it generically\n"
    )
    assert _hits(diff, diff=True) == []


def test_added_line_in_a_diff_is_caught():
    diff = "--- a/x.md\n+++ b/x.md\n+new host srv-example-gpu added\n"  # leak-guard-allow: invented fixture, not a real host
    assert "server-hostname" in _names(diff, diff=True)


def test_diff_headers_never_trigger():
    """+++ / --- lines carry paths and must not be read as content."""
    assert _hits("--- a/srv-thing/x.md\n+++ b/srv-thing/x.md\n", diff=True) == []
