"""Guard: no `secure:` values in committed stack config (infra#2358).

`grug/prod` is on `awskms://alias/pulumi-secrets`, so a `secure:`
ciphertext in `Pulumi.prod.yaml` is currently inert to anyone without an
IAM-gated `kms:Decrypt` call. **This guard is not fixing a live
exposure.** It exists because that property depends entirely on which
secrets provider the stack happens to be using, and that is a one-line
change.

Under the passphrase provider the `encryptionsalt` lives in this same
file, in a PUBLIC repo. A committed ciphertext would then hand an
attacker both halves at once, and the attack moves offline, where no IAM
policy, CloudTrail alarm or key disable can reach it. That is not
hypothetical: this stack spent part of 2026-08-08 on the passphrase
provider before being moved back, and any `secure:` value committed
during a future window like that would stay attackable afterwards -
rotating the provider does not un-publish a ciphertext someone already
cloned.

So the rule is provider-independent even though the threat is not:
secrets belong in SSM under `/grug/*`, read at program-eval time, as
this stack already does everywhere else. Nothing goes in this file.

There are zero such values today. This test is here so that stays a
property of the repo rather than a property of nobody having done it
yet - the class of guarantee that stops being true silently.
"""

from __future__ import annotations

import re
from pathlib import Path

_STACK_CONFIG_GLOB = "Pulumi.*.yaml"

# Pulumi writes encrypted config as a `secure:` mapping key, e.g.
#     grug:someToken:
#       secure: v1:abc...
# Match it as a YAML key only, so prose or a config value that merely
# contains the word does not trip the guard.
_SECURE_KEY = re.compile(r"^\s*secure:\s*\S", re.MULTILINE)


def _stack_configs() -> list[Path]:
    pulumi_dir = Path(__file__).resolve().parent.parent
    # Exclude the checked-in example, which is documentation and carries no
    # real state.
    return [
        p
        for p in sorted(pulumi_dir.glob(_STACK_CONFIG_GLOB))
        if not p.name.endswith(".example")
    ]


def test_the_guard_actually_finds_a_secure_value():
    """Negative control.

    A guard that never matches anything passes identically whether it
    works or the pattern is wrong. Pin the pattern against a known-bad
    sample so a silently broken regex is a red test, not a quiet pass.
    """
    sample = "config:\n  grug:token:\n    secure: v1:abcdef==\n"
    assert _SECURE_KEY.search(sample) is not None

    # And that it does NOT fire on the benign lookalikes it must tolerate.
    assert _SECURE_KEY.search("config:\n  grug:note: secure things\n") is None
    assert _SECURE_KEY.search("# secure: handled via SSM\n") is None


def test_no_stack_config_contains_a_secure_value():
    offenders = []
    for path in _stack_configs():
        if _SECURE_KEY.search(path.read_text()):
            offenders.append(path.name)

    assert not offenders, (
        f"{offenders} contain a `secure:` value. The passphrase provider "
        "publishes `encryptionsalt` in this repo, so committed ciphertext is "
        "attackable offline. Store the secret in SSM under /grug/* and read "
        "it at program-eval time instead."
    )


def test_there_is_a_stack_config_to_check():
    """The guard is worthless if the glob silently matches nothing."""
    assert _stack_configs(), "no Pulumi.<stack>.yaml found - guard is inert"
