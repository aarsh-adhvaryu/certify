# Agentic Operator — working notes

An always-on OS-level daemon: a conductor model orchestrating a tiered pool of
execution workers behind a deterministic verifier gate. Standalone product, not a
Claude Code extension.

- **[agentic-operator-spec.md](agentic-operator-spec.md)** — the design. Source of truth for *what* the system is.
- **[BUILD-PLAN.md](BUILD-PLAN.md)** — the slot-by-slot build order. Source of truth for *what to do next*.

---

## Start here

**Live on DeepSeek + Claude Code (Pro). ~$1.20 real spend to date. 757 tests green.**

```powershell
$env:PYTHONPATH = "d:\orchestrator\src"
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY","User")
```

Both are needed in any shell that runs `aop`. VS Code terminals inherit the
editor's environment, so a "new" terminal there may still lack the key — either
pull it as above or restart VS Code. `pip install -e .` removes the PYTHONPATH
line permanently.

### The baseline exists — `evals/runs/deepseek.json` (22 Aug 2026)

**9/11 correct, 11/11 coverage, $0.5179, 111 minutes.** The first usable
measurement after four abandoned attempts, and the incumbent side of Slot 48d.

Two failures, both real rather than artifacts:

- `topk-shortfall` — a genuine capability failure. Four attempts, full ladder
  climb, `awaiting_human`.
- `underspecified` — *"Make the retriever better."* was expected to be handed
  back and instead completed, twice in a row. The conductor accepts vague work
  and the gate certifies it: the `acceptance: []` failure mode in a new costume.
  **Worth its own slot.**

`impossible-offline` was correctly refused, but on the budget ceiling rather than
on judgement, which slightly flatters the 82%.

**Budget real runs at ~$0.05/task, not $0.01.** Four successive estimates
($0.06 → $0.12 → $0.29 → $0.45) all came in low. The refusal tasks are the
expensive ones: `impossible-offline` $0.115 and `underspecified` $0.079 are a
third of the bill between them.

**The Claude Code execution plane — decided, built.**
Block J2 in [NEXT-PLAN.md](NEXT-PLAN.md) carries the whole design. The answer is
**prefer and fall back**, not replace: Claude Code becomes the preferred
execution plane, ours stays as the fallback when the subscription is exhausted,
and the ladder survives because `ClaudeAgentOptions.model` gives three tiers on
one subscription. **Slot 48a is done** — `execution/plane.py` is the seam.

### Pick up here — Block J2 is CLOSED

| Slot | State |
|---|---|
| 48a `ExecutionPlane` seam · 48b `ClaudeCodePlane` · 48c failover · 48d comparison | ✅ **all built and run** |

**The answer (22 Aug 2026), on the 10 tasks both planes graded:**

| | DeepSeek | Claude Code (Pro, Sonnet ×3) |
|---|---|---|
| correct | 8/10 | **9/10** |
| real money | $0.5179 | **$0.3096** |
| list-equivalent | $0.5179 | $12.67 |
| wall clock | 110.8 min | 99.7 min |

Claude Code won one task and lost none. `topk-shortfall` is the separator:
DeepSeek exhausted all four attempts and handed to a human; Claude Code solved it
in two. Formal `Comparison` says **NO VERDICT** — the candidate lost
`unknown-intent` to a conductor-side `TransportError`, so coverage was 10 vs 11
and the guard refused to compare. `on_common_tasks()` is the narrowed answer.

**Next slot is not the plane.** `underspecified` — *"Make the retriever
better."* — was expected to be handed back and was **completed by both planes**,
with the gate certifying both. The executor was never the problem: no
implementation plane fixes a conductor that accepts vague directives. Fix spec
emission (refuse under-specified directives at the checkpoint) before anything in
Block K.

Full write-up in [NEXT-PLAN.md](NEXT-PLAN.md) Block J2.

**Running the claude_code plane** needs the CLI on PATH — resolve it, never pin a
version (a hardcoded `2.1.237` broke within hours when the extension updated):

```powershell
$cli = (Get-ChildItem "$env:USERPROFILE\.vscode\extensions\anthropic.claude-code-*-win32-x64\resources\native-binary\claude.exe" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1).DirectoryName
$env:PATH = "$cli;$env:PATH"
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY","User")
.\.venv\Scripts\python.exe -m aop --config config-claude eval --label claude_code --out evals\runs\claude_code.json
```

Note `--config` comes **before** the subcommand. If a run dies, re-run the
identical command: it resumes from `<out>.partial`.

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

## What exists (Blocks A–I + 48a/48b/48c, 743 tests)

`src/aop/execution/` · `src/aop/conductor/` · `src/aop/router/`

| Module | Role |
|---|---|
| `execution/plane.py` | `ExecutionPlane` / `PlaneOutcome` — the four facts the ladder consumes. The seam a non-`Worker` plane plugs into |
| `execution/claude_code.py` | Claude Agent SDK plane. `PreToolUse` hook calls `resolve_for_write`; quota → `AdapterError` → `TRANSPORT`. Optional extra, never a silent fallback |
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
| `registry.py` | `Registry` — the only path from a role slot to a model identity, prices, and capability tags. Modality-aware tier selection (`tier_for`, `escalate`), sideways failover (`chain`, `advance`, `reset`), credentials from env by name |
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

**An outage is not a result, and the measuring instrument must know it.** The
first real baseline reported 6/11 = 55%; four of the five "failures" were
`TransportError` and had never been graded at all. The true figure over what ran
was 6/7 = 86%. The orchestrator classed them correctly — no escalation, no
training rows — and `Harness` then threw the distinction away by scoring
`status is DONE`. `tasks.failure_class` (migration 3) carries it now,
`RunReport.pass_rate` divides by **graded** rather than total, and
`Comparison.comparable` refuses a verdict when two runs graded different task
sets. That last guard matters because Claude Code runs locally and *cannot*
suffer a DeepSeek DNS failure — every incumbent outage would otherwise read as
candidate skill.

**The wire is not evidence, so retry it — in the adapter, not per call site.**
Three eval runs died because one dropped connection killed a task outright: the
ladder had retried `TRANSPORT` since Slot 16, but the conductor and the
test-author had no equivalent, so a blip during *planning* was fatal. Retrying in
`Adapter` covers every caller by construction. Deliberately narrow — only
`httpx.HTTPError`; a 4xx is a `ProviderError` and is **not** retried, because
repeating a request the server actively refused just spends money to be told no
again. On exhaustion it still raises `TransportError`, so every downstream
classification is unchanged: rarer, never invisible.

**A long run must survive being interrupted.** `aop eval` writes
`<out>.partial` after **every** task, atomically, and resumes from it. A
65-minute run whose report only materialises on the final line throws away
everything it paid for the moment anything touches it — which happened three
times: twice to a reboot, once to wifi. Tasks killed by the wire are deliberately
**re-run rather than restored**, because restoring them would bake the outage
into the report permanently.

**`test_author_role` must name a role that stays on HTTP.** Authorship runs on
the internal worker whichever execution plane is selected, so pointing it at an
execution tier breaks the moment that tier moves to `claude_code`, which has no
`base_url`. It is `conductor` now — which is also a stronger author/implementer
separation than `low` ever was. Changing it mid-experiment would make two eval
runs non-comparable, so it is fixed before the baseline, not after.

**The logbook records what served, not what was configured.** `served_model_id`
comes off the plane, never from `registry.model_id(role)`. They are the same
today and diverge the moment failover exists — at which point recording the
registry's opinion would label every attempt with a model that never ran, and
`training_rows()` hands exactly those rows to the router.

**A tier change and a vendor change are different moves.** `VERIFIER` escalates
*up* the ladder; `TRANSPORT` (quota, credit, transport death) moves *sideways* to
the next vendor and must never escalate or train. Confuse them and a Monday
subscription reset reads as "the cheap tier failed four tasks in a row".

**The active-vendor pointer is process-wide, not per-task.** Running out of
credit is a property of the vendor and the key, not of the task that happened to
discover it. Per-task state looks tidier and makes every concurrent task pay its
own failed dispatch to learn the same fact — `test_the_vendor_pointer_is_process_wide`
exists because this is the obvious thing for a later refactor to "clean up".
Related: the fallback chain is **flat** (a fallback may not have its own), since
a tree has no answerable "which vendor is next".

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
