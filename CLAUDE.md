# Agentic Operator — working notes

An always-on OS-level daemon: a conductor model orchestrating a tiered pool of
execution workers behind a deterministic verifier gate. Standalone product, not a
Claude Code extension.

- **[agentic-operator-spec.md](agentic-operator-spec.md)** — the design. Source of truth for *what* the system is.
- **[BUILD-PLAN.md](BUILD-PLAN.md)** — the slot-by-slot build order. Source of truth for *what to do next*.

---

## Start here

**Live on DeepSeek. $0.081 spent to date. 679 tests green.**

```powershell
$env:PYTHONPATH = "d:\orchestrator\src"
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY","User")
```

Both are needed in any shell that runs `aop`. VS Code terminals inherit the
editor's environment, so a "new" terminal there may still lack the key — either
pull it as above or restart VS Code. `pip install -e .` removes the PYTHONPATH
line permanently.

### Two things are unfinished

**1. The eval baseline was interrupted** (laptop shut down mid-run). It left
**2 tasks stuck in `running`** and wrote no report. The scheduler reclaims
orphans on start, but the baseline is not usable — re-run it from a clean slate:

```powershell
Remove-Item -Recurse -Force workspace, state.db, state.db-wal, state.db-shm -ErrorAction SilentlyContinue
python -m aop eval --label deepseek --out evals\runs\deepseek.json
```

Budget it at **~$0.12**, not the $0.06 originally estimated — the partial run
measured ~$0.0104/task on real suite tasks, against $0.0055 for the trivial one.

**2. The Claude Code execution plane — decided, and started.**
Block J2 in [NEXT-PLAN.md](NEXT-PLAN.md) carries the whole design. The answer is
**prefer and fall back**, not replace: Claude Code becomes the preferred
execution plane, ours stays as the fallback when the subscription is exhausted,
and the ladder survives because `ClaudeAgentOptions.model` gives three tiers on
one subscription. **Slot 48a is done** — `execution/plane.py` is the seam.

### Pick up here

| Slot | State | Needs |
|---|---|---|
| 48b `ClaudeCodePlane` | designed, not built | **which Claude plan tier** — Max gives three rungs, lower tiers give one. Changes `registry.toml`, nothing else |
| 48c failover chain | designed, not built | **nothing — fully unblocked** |
| 48d run the comparison | — | 48b + the baseline above |

**Start with 48c unless the plan tier is known.** It is registry + ladder
mechanics (role becomes a list, `TRANSPORT` moves sideways, `VERIFIER` moves up)
and is testable end to end against `tests/config/` with two mock roles. No key,
no subscription, no baseline.

Both remaining slots are one slot each. Do not do both.

---

## Original start-here

**The goal is a Jarvis**, not a batch execution engine. See
**[NEXT-PLAN.md](NEXT-PLAN.md)** for the re-scoped remaining work (Slots 41–73)
and why the order changed. **Slots 41–42 are done** — there is a running daemon
with an overlay:

```powershell
.\.venv\Scripts\python.exe -m aop serve      # daemon + overlay on :8765
.\.venv\Scripts\python.exe -m aop run "..."  # one directive, headless
.\.venv\Scripts\python.exe -m aop status     # what it believes
```

**Block I is complete (Slots 41–46b).** Everything buildable before the buy point
is done: service, overlay, scheduler, frameless window, tray, hotkey, autostart.

```powershell
.\.venv\Scripts\python.exe -m aop app        # daemon + tray + hotkey + overlay
.\.venv\Scripts\python.exe -m aop autostart on
```

**Next: Slot 47 — the buy point.** [PRICING.md](PRICING.md) carries the phased
plan and a buy-day runbook. The **eval harness is already built** (`aop eval`),
so the model-allocation questions are answered by measurement rather than by
argument — run the suite, swap a role, run it again, compare.

Do not buy anything or add a real `api_key_ref` yourself; that is the user's call.

**The shell is a client and must never import the Operator** — a test asserts it
by reading the source. That separation is what lets the window crash, the tray
die, or the hotkey be refused without disturbing a task mid-flight.

`src/aop/operator.py` is the composition root: the one place that knows the order
things happen in. Nothing there should contain logic that is not *sequencing* — if
a rule appears in it, it belongs in the plane it came from.

**Slots 01–40 are complete and green. Everything buildable without spending money
is done.**

**Next slot: 41 — the buy point.** The *research* is done and written up in
**[PRICING.md](PRICING.md)**: verified prices, Artificial Analysis intelligence
and speed figures, benchmark comparisons, endpoints, and a ready-to-paste
registry block.

**The decision is open and belongs to the user.** Three questions are listed at
the top of PRICING.md — which model fills `high`, whether the conductor stays
Kimi K3 or moves to something cheaper and faster, and which model sits at `max`.
Do not pick for them, and do not buy anything.

Do not add a real `api_key_ref` or `base_url` to `config/registry.toml` as a
convenience while working on something else. The loader rejects pasted secrets,
but the buy-point is a decision, not a side effect.

Worth knowing before reading PRICING.md: the spec's prices were close to right
(Kimi and Qwen-Max exact), and its original choice of Qwen-Max for `high` looks
correct — it is the only affordable multimodal tier, and without one the visual
escalation ladder has a single rung.

The whole pipeline runs today on mocks at zero cost: directive → checkpoint →
spec → router → test authorship → ladder → verdict → logbook → journal.

---

## One slot per session

A slot is one component plus its tests, sized to finish in a single session
ending on green. This is deliberate: a session that ends mid-component leaves the
next one guessing.

**Rules:**

1. **Do one slot.** Do not start the next one because it looks small. If a slot
   turns out to be two slots, say so and split it in the plan rather than
   silently doing both.
2. **Finish green.** `pytest` passes before the session ends, with tests for the
   slot's stated "done when" condition.
3. **Update [BUILD-PLAN.md](BUILD-PLAN.md)** — mark the slot ✅, and if the build
   revealed that something belongs in a different slot, record the change and the
   reason. Block A has three such notes; that is the expected shape.
4. **Do not commit or push** unless asked.

Deviating from a slot's plan is fine and sometimes correct — the plan is a
hypothesis. Write down what changed and why.

---

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q          # all tests
.\.venv\Scripts\python.exe -m pytest tests/test_x.py    # one file
.\.venv\Scripts\python.exe -m pip install <pkg>         # deps go in pyproject.toml too
```

Python 3.13 in `.venv`. No Node, no uv. Tests run from `pyproject.toml`
(`pythonpath = ["src"]`, `asyncio_mode = "auto"` — async tests need no decorator).

---

## Rules that must not be broken

These are cheap to enforce and expensive to get wrong. Most have a named test.

| Rule | Why it bites |
|---|---|
| Code references **roles** (`conductor`/`low`/`high`/`max`), never model names | A model literal outside `config/` breaks the swap-in-config premise the whole registry exists for |
| Guards are **deterministic and zero-token** | No model call in a guard path, ever. A guard that costs tokens is not a guard |
| A **guard trip is not a verifier failure** | It must not escalate a tier and must not become a router training label. `FailureClass` carries this on the type — consult it, don't re-derive it |
| Escalation advances **only** on `Verdict.FAIL` | A worker's own claim about how it did carries no weight |
| Nothing writes outside the **jail root** | |
| Pruning **stores before it drops** | |
| The `<Directive>` is **immutable**, hashed at creation | Recovery must reproduce the hash, never recompute it |

Tunable values (`append_on_retry`, `suspend_threshold_seconds`, `max_attempts`,
budgets, effort defaults…) live in `config/policy.toml`, not in code. Their right
setting is an empirical question.

---

## Conventions

- **Money is `Decimal`, stored as TEXT.** Never float, never a REAL column.
  Budget ceilings compare against these values.
- **Datetimes are timezone-aware UTC**, stored ISO-8601. Naive values are
  rejected at the schema and at the store boundary.
- **Clock and ids are injected** (`core/ids.py`). Never call `datetime.now()` or
  `uuid4()` inline — determinism tests and transcript replay both depend on it.
- **Schemas subclass `Strict`** (`extra="forbid"`). A typo in a field name should
  fail at construction, not vanish into a dict nothing reads.
- **Every durable record pins `schema_version`.** The task spec is the
  conductor↔worker contract; an unversioned format change silently corrupts the
  router's training set.
- **Docstrings explain the non-obvious *why*.** The existing modules state what
  would break if the rule were violated — match that, don't narrate the code.
- **Tests assert the consequence, not the implementation.** See
  `test_events.py::test_slow_subscriber_cannot_stall_the_bus`.

---

## What exists (Blocks A–I + 48a, Slots 01–48a, 679 tests)

`src/aop/execution/` · `src/aop/conductor/` · `src/aop/router/`

| Module | Role |
|---|---|
| `execution/plane.py` | `ExecutionPlane` / `PlaneOutcome` — the four facts the ladder consumes. The seam a non-`Worker` plane plugs into |
| `execution/worker.py` | `render_spec` (deterministic, field by field) + one dispatch |
| `execution/tools.py` | read / write / edit / list / run, all guard-wrapped |
| `execution/ladder.py` | Executes the Slot 16 decision table. Escalation calls no conductor |
| `memory/logbook.py` | One row per attempt; `tier_stats` for "is `low` earning its place" |
| `conductor/directive.py` | Hash re-checked at every checkpoint |
| `conductor/taskspec.py` | Structured emission with a repair loop; invalid never goes downstream |
| `conductor/checkpoints.py` | Four checkpoints as data; anything else raises |
| `conductor/authorship.py` | Separate test-author call, then freeze |
| `conductor/rationale.py` | Deterministic `check_plan` enforces; model prose only audits |
| `router/features.py` | One extractor for rules and classifier alike. `FEATURE_NAMES` is append-only |
| `router/rules.py` | Scores difficulty, then the registry applies modality |

`src/aop/guards/` · `src/aop/backends/` · `src/aop/verify/` · `src/aop/context/` · `src/aop/memory/`

| Module | Role |
|---|---|
| `guards/pathjail.py` | Resolve-then-contain. Traversal, symlinks, UNC, device names, drive-relative, ADS |
| `guards/commands.py` | Allowlist, deny-by-default. argv lists only — no shell anywhere, ever |
| `guards/budget.py` | Per-task and per-day ceilings, checked *before* dispatch |
| `core/failures.py` | The decision table: (failure class, attempts, policy) → action |
| `backends/` | `RunBackend` + Windows and WSL impls, guards wired in by construction |
| `verify/` | Gate + registry, static/stateful split, pytest gate, mechanical poller |
| `context/assembler.py` | `[prefix ‖ tail]`, `append_failure`, explicit `rebuild_prefix` |
| `context/pruner.py` | Store-before-drop, deterministic summary |
| `memory/store.py` | SQLite FTS5 lexical store. Pruned context only — not a document library |

`src/aop/registry/`

| Module | Role |
|---|---|
| `registry.py` | `Registry` — the only path from a role slot to a model identity, prices, and capability tags. Modality-aware tier selection (`tier_for`, `escalate`), credentials from env by name |
| `cost.py` | `Usage` + `CostModel`. Prices from the registry, Decimal throughout. **Missing usage raises** |
| `adapter.py` | One HTTP client for every provider. `complete`, `stream`, `complete_streaming` |
| `shims/` | Per-provider quirk seam. Baseline OpenAI + mock only; real vendor shims wait for Slot 41 |
| `providers/mock.py` | `MockProvider` — scripted answers over a real `httpx` transport |
| `providers/replay.py` | Record/replay with strict request-hash matching |
| `toolcalls.py` | `ToolBox` + `run_tools`. Schema emission, dispatch, capped multi-turn loop |

`src/aop/core/`

| Module | Role |
|---|---|
| `ids.py` | `Clock` / `IdSource` protocols; `SystemClock`+`UuidIds` for real use, `FrozenClock`+`SequentialIds` for tests |
| `schemas.py` | `TaskSpec`, `Attempt`, `Verdict`, `Observation`, `Task`, `FailureClass`, `Role`, ladder helpers |
| `config.py` | Loads `config/registry.toml` + `policy.toml` into typed settings, validated eagerly with field paths |
| `events.py` | Async pub/sub. **Lossy by design** — publishers never block on a subscriber |
| `state.py` | SQLite: tasks, attempts, migrations, spend. `training_rows()` filters ineligible labels at the read |
| `lifecycle.py` | Legal transition map, suspend/resume, orphan recovery after a crash |
| `journal.py` | `OPERATOR.md` — deterministic, generated from state, never model-written |

**The journal** is the failsafe: prose for humans, a fenced `aop-state` JSON block
that is the authority. Delete the database and `Journal.recover()` rebuilds from
the markdown alone. Both halves come from one snapshot, so they cannot disagree.

**The event bus is lossy on purpose.** Blocking a publisher until the slowest
consumer catches up would let a wedged UI wedge the orchestrator. Durability is
SQLite's job — which is what lets the journal be a projection of state rather
than an accumulation of whatever events arrived.

**Modality overrides difficulty.** `Registry.tier_for(desired, needs_pixels=)`
routes around text-only tiers, preferring a capable tier at or above the desired
one so a visual task is not quietly downgraded. With the shipped ladder that
means visual work has only one rung — `escalate(high, needs_pixels=True)` is
`None` — until the conductor's pre-digest step exists.

`test_registry.py::test_no_model_name_appears_outside_config` scans `src/` for
hardcoded model ids. If it fails, route the decision through a capability tag
instead of adding an exception.

**The mock speaks HTTP, on purpose.** It is an `httpx` transport returning real
OpenAI-dialect JSON and SSE, so every mock-driven test exercises the adapter for
real. Do not add a bypass path "for speed" — the fast path would become the
default and the faithful one would rot, and you would find out at Slot 42. Two
faithful details to preserve: usage appears **only** when
`stream_options.include_usage` was sent, and streamed content arrives fragmented
with tool arguments cut mid-JSON.

**A tool problem is a message, not an exception.** Unknown tool, malformed JSON
arguments, a handler that raises — all come back as a structured tool result the
model can correct itself from. Guard denials will travel this same path in Slot
30: cheap, same tier, cache intact, never an escalation.

**Replay matches on a strict request hash.** A cassette miss means a prompt
changed. Re-record; do not loosen the match. A green test replaying the wrong
response is worse than a red one.

**A failed exit code is not automatically a verdict.** `python -m pytest` exits 1
both when tests fail and when pytest is not installed. Before classing anything
as a *verifier* failure, confirm the check actually ran — otherwise a broken
environment escalates tiers and poisons the router. Same principle for any future
gate: distinguish "the model was wrong" from "our tooling broke", because only
the first may escalate or train.

**On Windows, `env` does not choose the executable.** `CreateProcess` searches the
calling process's PATH, not the environment block you pass. `WindowsBackend`
resolves the program itself for this reason — do not "simplify" it back to
passing argv straight through.

**Guard and transport failures count toward the attempt cap but never toward the
escalation counter.** Drop the first half and a worker looping on jail escapes
never terminates; drop the second and it climbs the ladder while doing so.

**The conductor wakes at four checkpoints and nowhere else.** `WAKES_CONDUCTOR`
lists every event and whether it may; anything else raises `NotACheckpoint`. If
you find yourself wanting to add one, that is a cost decision, not a refactor —
this is the single biggest dial on the bill.

**Escalation does not call the conductor.** Re-dispatch the same spec one tier up
with the reason appended. `replan_on_escalation` exists and is off.

**The model's own rationale never grants approval.** `check_plan` is
deterministic and can refuse a plan; the conductor's stated reasoning is recorded
for audit and carries no weight. Same shape as the journal: the machine-readable
half is authoritative, the prose is for humans.

**`python` resolves to the operator's own interpreter**, not whatever is first on
PATH (`execution.python` overrides it). Bare `python` is usually a system install
with no pytest, so the gate reported "could not run the suite" on every attempt —
correctly classed as a broken tool rather than a weak model, and equally useless.
A test that relied on that ambient breakage now forces the condition instead.

**The test suite has its own config at `tests/config/`, always the mock
provider.** It used to load the project's `config/`, which was harmless while
that pointed at the mock and became a liability the moment a real key was bought:
the suite started making live, billable API calls and failed outright without the
credential. A suite whose behaviour depends on which model you happen to have
configured is not testing what it thinks. `test_config.py` still checks the
shipped config parses and contains no pasted secret — validity, not identity.

**A spec with no acceptance criteria is refused, not warned about.** On the first
live run a conductor emitted `acceptance: []`; the authorship step found nothing
to write, froze no test file, and the implementer wrote both the code and the
tests it was graded by. pytest passed and the task reported success. Empty
criteria do not make the gate vague — they disable it while still reporting a
pass.

**Every billable call goes in the `spend` ledger, not just attempts.** Cost used
to be summed from `attempts`, so it saw execution only — planning and test
authorship were unrecorded and outside the budget ceiling, which is exactly
backwards. `attempts` remains the router's training set; `spend` is the bill, and
the guard reads the bill. `record_spend` owns the task's cost rollup, so there is
one place that sees every call.

**Measured on real traffic: the conductor is ~60% of a task's cost** — 3,605
output tokens for planning against 338 for the work. The lever is how often the
conductor thinks, not which tier the router picks.

**Run the pipeline, not just the tests.** Every real bug found so far passed the
whole suite first: a cost of `0E-10`; an event stream claiming four attempts
against three logbook rows; a router demoting all ordinary work to the cheap tier;
and `provider = "mock"` being ignored outside the tests, because all 17 service
tests injected a transport while the daemon tried to resolve the mock's host over
the network. Run `python -m aop serve` and use it.

**A component that is green in isolation may still be wired to nothing.** The
audit after Slot 42 found `due_for_resume()`, `lifecycle.resume()` and the whole
Slot 25 suspension mechanism consumed by *nothing* — every part tested, no
scheduler to call them. When finishing a slot, check who calls it, not just that
it passes. `core/scheduler.py` (Slot 46) is now that loop.

**The scheduler owns execution. There is one way to start work.** `submit()`
queues, `run_directive()` submits and waits, and the loop claims. Calling
`Operator.run()` directly races the scheduler for the same task — if you need to
drive the pipeline by hand, use `start(run_scheduler=False)`.

**A resumed task re-runs the pipeline from the top.** There is no continuation
record, which is why mid-ladder suspension is still not wired in: suspending
inside a climb would lose the climb. Persisting ladder position is its own slot.

**The logbook records what served, not what was configured.** `served_model_id`
comes off the plane, never from `registry.model_id(role)`. They are the same
today and diverge the moment failover exists — at which point recording the
registry's opinion would label every attempt with a model that never ran, and
`training_rows()` hands exactly those rows to the router.

**A tier change and a vendor change are different moves.** `VERIFIER` escalates
*up* the ladder; `TRANSPORT` (quota, credit, transport death) moves *sideways* to
the next vendor and must never escalate or train. Confuse them and a Monday
subscription reset reads as "the cheap tier failed four tasks in a row".

**Authorship keeps the internal worker whichever plane is selected.** The test
author and the implementer must not be the same actor; putting them on different
planes entirely is a stronger separation than the frozen file alone.

**An unbuilt plane raises rather than falling back.** A run that quietly used the
internal plane while the report said `claude_code` is not an execution bug — it is
a wrong answer to the question the eval exists to settle.

**Anything durable that a worker could rewrite is a way to pass without working.**
The state database sits outside the jail; the journal sits inside it (so it is
readable) but is frozen. Apply the same test to anything new: if a worker can edit
it, could editing it substitute for doing the work?

---

## Cost posture

Every role in `config/registry.toml` points at the **mock provider**. Nothing in
Slots 01–40 spends money. Real keys are not needed until **Slot 41**, which
re-verifies prices and model ids before anything is bought.

Do not add a real `api_key_ref` or a real `base_url` to the registry as a
convenience. The loader rejects pasted secrets, but the buy-point is a decision,
not a side effect.
