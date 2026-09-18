# Tianji

Tianji is a host-neutral local orchestration system for multiple hosts and model sources.

It provides one shared workflow for planning, decomposition, routing, role execution, real-model proof, failure compensation, mechanical acceptance, and the runtime ledger. Kimi, Codex, and future hosts only provide adapters; they do not contain separate Tianji implementations.

## User workflow

After one-time installation and host reload, the user simply describes a normal task:

~~~text
Tianji, inspect the login flow in this project, fix any issue you find, add or update tests, run the relevant tests, and report the changes and remaining risks.
~~~

Tianji handles health checks, route proof, decomposition, worker execution, independent verification, bounded rework, compensation, ledger records, and final delivery internally. Users do not need to run probe, ledger, or acceptance scripts.

## Installation

### Kimi

Run once from the repository root:

~~~bash
python install.py install --host kimi
python install.py status --host kimi
~~~

`--host` names the current host. When omitted, only a signal the host process injects counts (Command Code injects one; kimi and codex do not, so pass `--host` explicitly); the script refuses instead of defaulting to kimi. A configuration directory (`CODEX_HOME`/`KIMI_CODE_HOME`) says what is installed, not which host is running, so it is not treated as identity.

Reload the Kimi host and say Tianji. The first-use flow guides model-pool setup, role assignment, and route verification.

### Codex

Run from the repository root:

~~~bash
python install.py install --host codex
python install.py status --host codex
~~~

Restart Codex, confirm the Tianji hooks in /hooks, then say Tianji. The Codex adapter uses native named subagents, hooks, and the Responses provider. It does not silently guess credentials, role bindings, model changes, or route switches.

## Architecture

~~~text
Shared Tianji core
├── orchestration and decomposition
├── RoleContract
├── model pool and cross-vendor routing
├── real-model proof
├── runtime ledger
├── mechanical acceptance
├── failure compensation
└── user-facing black-box workflow

HostAdapter
├── Kimi: Markdown roles and Kimi host configuration
└── Codex: TOML roles, native subagents, hooks, Responses provider
~~~

Role definitions have one shared source:

~~~text
agents/tianji-*.md
        ↓
skills/tianji/scripts/role_contract.py
        ├── Kimi Markdown
        └── Codex TOML
~~~

## Roles

Three roles, one per tier. A role is a station, not a job title: exploration, research, reproduction and implementation are modes of one worker dispatch, separated by the task book's read-only flag rather than by more role names.

- tianji-worker: economy tier. Exploration, research and reproduction (read-only), and implementation (writable)
- tianji-verifier: quality tier. Three axes of review -- Spec (missing, extra or misdirected work, judged against the original request), Standards (the repository's written conventions plus a code-smell baseline), and correctness and risk (test authenticity, diff review, independent findings). The axes are written up separately and never merged into one list; one axis failing sends the work back with that axis's rework pointer
- tianji-referee: escalation tier. Clearing false findings is a standing duty (only findings independently reproduced are accepted, and the reviewer who raised one never confirms it); arbitration of conflicting verdicts comes on top of that

Mechanical acceptance is the controller's gate -- deterministic commands do not need to occupy a review seat -- while the reviewer still reruns those commands rather than trusting a report of them. A change touching authentication, authorization, session handling, cryptography, the permission model or credentials requires the strongest tier on the review seat.

Route proof is not a role: after install or a rebind, the controller dispatches one read-only worker self-check and collects the evidence. Any completed paired dispatch of a bound role counts.

## Tiers and budgets

Whether to dispatch, and how hard to review, follows the **reversibility of the artifact**, not the size of the task:

| Tier | Criterion | Process | Budget written at dispatch |
|---|---|---|---|
| T0 | Settled at a glance (wiring, one-line fixes) | Done directly, not dispatched, not ledgered | -- |
| T1 | Read-only output (a report, research, reproduction) | One worker; review reads two axes | 500K tokens |
| T2 | Files changed outside the shared core | Worker, three axes, gate | 1M |
| T3 | Core mechanisms, config writes, deletions, security surfaces | Worker plus two reviewers of different models, each running every axis, and the referee | 2M |

The budget is written into the ledger at dispatch (`open --token-budget`) and read back at close: an overrun is reported per dispatch and **never acted on** -- killing a nearly finished dispatch costs more than the overrun. A dispatch that was given no ceiling is listed separately, because "no ceiling" and "inside the ceiling" are two different facts. The `tok spent/ceiling` in the terminal footer is the same account: a dispatch that is **still running** past its ceiling turns it red, shows that dispatch's own ratio, and moves it to the front of the line where it cannot be truncated. A finished overrun is not marked in the footer -- that is a fact about the past, and the close report and the board are where it is told.

The main session owns decomposition, routing, retry decisions, acceptance, and integration. Workers do not spawn nested workers. Delivery requires verifier PASS.

## User-visible acceptance

A normal task must demonstrate real workspace changes, real test execution, identifiable model and route evidence, bounded failure recovery, and a final report containing changed files, test results, and remaining risks.

See skills/tianji/USER-VALIDATION.md for the complete black-box acceptance rules.

## Project layout

~~~text
install.py                         # multi-host install, status, uninstall, sync
agents/                            # shared role source packages
skills/tianji/                     # main Skill and runtime protocol
skills/tianji/scripts/role_contract.py
                                   # shared role contract and renderers
skills/tianji-proxy/               # local model proxy and routing
docs/                              # host capability notes
tests/                             # shared protocol and host regression tests
~~~

## Development checks

~~~bash
python -m py_compile install.py
python -m unittest discover -s tests -p "test_*.py"
~~~

## Documentation

- Host capability and integration evidence: docs/CMDC-CAPABILITY.md, docs/CODEX-CAPABILITY.md
- Role topology: skills/tianji/ROLE-TOPOLOGY.md
- Runtime protocol: skills/tianji/RUNTIME-PROTOCOL.md
- User black-box validation: skills/tianji/USER-VALIDATION.md

## Dependencies

- Kimi Code or Codex host
- Python 3.11+
- An available model pool, proxy, or direct model source

## License

MIT License; see [LICENSE](LICENSE). The repository contains no third-party code.

MIT
