# Tianji role topology

This is the host-neutral role contract: the duties and evidence requirements
below are the same no matter which host renders them. Host adapters may map or
collapse roles, but they must preserve those duties.

**One role per tier.** A role is a station (who may write, who may judge, who
decides disputes), not a job title. Exploration, research, reproduction and
implementation are *modes* of one dispatch, declared by the task book's
read-only flag — not four more rosters, four more bindings and four more
dispatch round-trips for the same model.

| Role | Tier | Responsibility | Evidence contract |
|---|---|---|---|
| worker | economy | Every bounded dispatch: exploration, research, reproduction, implementation. Read-only unless the task book says otherwise | Result, changed files, real checks, actual model, remaining risks |
| verifier | quality | Independent mechanical PASS/FAIL gate, diff review, correctness/security/regression/missing-test findings | Ranked findings with file/symbol, concrete fix, acceptance-gate verdict |
| referee | escalation | Resolve conflicting verifier findings independently | Disputed points, evidence, final ruling |
| *(controller)* | — | The parent session: decomposition, routing, acceptance, integration — and the route proof, which it earns by dispatching a role | Task books, receipts, verdicts |

The controller is not a tier and never a subagent: it is never rendered into a
host's agent format and never installed.

## Modes, not roles

The task book's required `只读模式:是|否` field carries what used to be separate
role names:

| Mode | Read-only | Was |
|---|---|---|
| exploration | yes | `tianji-explorer` |
| research | yes | `tianji-researcher` |
| reproduction | yes | `tianji-tester` |
| self-check | yes | `tianji-probe` |
| implementation | no | `tianji-worker` |

The read-only flag is a task-book requirement precisely *because* every role
now carries a shell: after convergence the tool grant cannot stop an analysis
dispatch from writing, so the contract has to say so explicitly.

## Route proof

Proving that the host really dispatches Tianji roles is the controller's job,
not a role's. Any completed, paired dispatch of a bound role is the evidence —
which role ran says what that role's binding was, not how strong the proof is.
There is no dedicated probe role to keep alive, and no extra round-trip to
spend before real work starts.

## Recommended flow for a large task

```text
worker (read-only: explore/research) -> worker (implement) -> verifier -> referee on conflict
```

## Hosts

Codex exposes these as native roles (`tianji-<role>.toml`). Kimi or another host
may run them as the equivalent shared agent templates. The core Tianji protocol
must not depend on Codex-only fields such as `agent_type`, `sandbox_mode`, or
`developer_instructions`.
