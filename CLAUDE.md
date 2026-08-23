# Agentic Operator — working notes and plan of action

**A deterministic verification layer for coding agents.** Guards, a frozen-test
gate, a falsifiability check and a spend ledger — none of which call a model.

This document is the whole repo's memory. It replaced `BUILD-PLAN.md`,
`NEXT-PLAN.md`, `PRICING.md` and `agentic-operator-spec.md` on 23 Aug 2026; all
four are in git history if you need the reasoning behind an old decision.

---

## What this is now

It began as a personal always-on Jarvis: a conductor model orchestrating tiered
workers behind a verifier gate. Two things changed that.

**The audit (22 Aug).** The execution core held. The *measurement layer* did not,
and three findings recorded as settled were not supported by the code meant to
produce them. Fixing them changed what looked valuable.

**The measurement that reframed it.** Of the source, split by what needs an LLM
at runtime:

| | lines |
|---|---|
| **zero-token** — guards, gate, falsifiability, state, journal, ledger | **~10,500** |
| model-dependent — spec emission, authorship, worker, ladder, the Claude plane | ~2,200 |

The part that answers *"models lie confidently"* calls no model. That makes it
portable to any host, costs nothing to run, and is the actual product. The
conductor, ladder and router are **optional extras, not the spine** — the audit
showed the ladder never escalated and the router is a constant function.

**Target:** an open-source package that works wherever the agent lives — Claude
Code, Cursor, Codex, Antigravity — as a CLI first, with thin per-host wrappers.

**Status:** 786 tests green. ~$1.20 real spend to date, all of it on measurement.

---

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q     # all tests
.\.venv\Scripts\python.exe -m aop run "..."        # one directive
.\.venv\Scripts\python.exe -m aop status           # what it believes
.\.venv\Scripts\python.exe -m aop eval <suite>     # pass-rate vs cost
.\.venv\Scripts\python.exe -m aop compare a b      # which allocation won
.\.venv\Scripts\python.exe -m aop serve            # local HTTP+WS (needs aop[service])
```

Python 3.13 in `.venv`. No Node, no uv. `pythonpath = ["src"]` and
`asyncio_mode = "auto"` come from `pyproject.toml`, so async tests need no
decorator and `PYTHONPATH` is only needed outside pytest:

```powershell
$env:PYTHONPATH = "d:\orchestrator\src"
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY","User")
```

VS Code terminals inherit the editor's environment, so a "new" terminal there may
still lack the key. `pip install -e .` removes the PYTHONPATH line permanently.

**Running the `claude_code` plane** needs the CLI on PATH. Resolve it, never pin
a version — a hardcoded `2.1.237` broke within hours when the extension updated:

```powershell
$cli = (Get-ChildItem "$env:USERPROFILE\.vscode\extensions\anthropic.claude-code-*-win32-x64\resources\native-binary\claude.exe" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1).DirectoryName
$env:PATH = "$cli;$env:PATH"
```

`--config` comes **before** the subcommand. A dead eval run resumes from
`<out>.partial` on an identical re-run.

---

## Plan of action

Ordered. Each phase is shippable on its own, and nothing later is needed to make
something earlier useful.

### Phase 0 — repo ready ✅ (23 Aug)

Removed the desktop shell (`daemon/`, 844 lines + its suite): tray, hotkey,
frameless window, autostart. It was Windows-only personal-Jarvis furniture with
no future under the pivot, and **it held the only `pywin32`/`winreg` code in the
tree** — the source is now free of Windows-native bindings.

Also: four unreachable helpers deleted (`thaw`, `ensure_root`, `clear_tail`,
`supports_tools`); `fastapi`/`uvicorn`/`websockets` moved from hard dependencies
to an optional `service` extra, so the core installs as three packages; five
markdown files collapsed into this one.

### Phase 1 — `aop verify`, the product

One command: point it at a repo and a directive. It refuses an unfalsifiable
directive, freezes acceptance tests before implementation, runs the gate, and
returns a real verdict. **Zero tokens, no API key, no conductor.**

Everything it needs exists and is tested. This phase is a public surface over
`guards/`, `verify/`, `conductor/rationale.py` and `core/state.py` — not new
machinery. Getting the API right matters more than the code.

### Phase 2 — cross-platform

One missing `PosixBackend`; `backends/` is Windows + WSL only. `PathJail` uses
`PureWindowsPath` deliberately, which is *over*-strict on Linux rather than
unsafe — it would reject legal colons in filenames. Decide whether that becomes a
platform-aware mode or stays paranoid everywhere.

### Phase 3 — Graphify as an optional structural input

[Graphify](https://github.com/Graphify-Labs/graphify) is Apache 2.0, parses with
tree-sitter, **needs no LLM**, and emits `graph.json`. It is the same
architectural species as these guards: deterministic, local, zero-token. It
attacks token burn on the *input* side (stop re-grepping) where this attacks it
on the *output* side (don't certify wrong work).

It closes three open findings at once, because all three were blocked on the same
missing thing — an authoritative structural map of the repo:

- **F-04** — populate `spec.artifacts` / `inputs` deterministically, so the
  scope-drift check can finally fire.
- **48e's second failure mode** — answer *does this symbol exist?* rather than
  guessing with a regex.
- **The referent test** in `falsifiability()` currently extracts identifiers by
  regex over fixture files. A real symbol table replaces the guess.

**Only ever refuse work on an `EXTRACTED` edge.** Graphify tags edges `EXTRACTED`
(explicit in source) or `INFERRED` (its own resolution). Refusing real work on an
inference is over-refusal, which is the worse failure. Wire it as an **optional
extra with a fallback**, the way `claude-agent-sdk` already is — a fast-moving
upstream schema is not something to couple hard to.

### Phase 4 — the quality axis (the genuinely new half)

*"Models lie confidently"* is answered by the gate. *"The code they write is not
optimized"* is **not, and nothing built touches it.** The gate is pass/fail
against acceptance criteria; a passing implementation can be O(n²), allocate in a
loop, or be unreadable, and the gate certifies it happily.

This needs something that does not exist yet — complexity budgets, benchmark
assertions, a perf gate. It is the harder half and it has not been started. Do
not let Phase 1's success imply this is covered.

### Phase 5 — host wrappers

**A plugin is packaging, not enforcement.** Inside a Claude Code plugin, `hooks/`
fire deterministically and `.mcp.json` does not — an MCP tool the model chooses
whether to call is not a gate, which is the `acceptance: []` lesson one level up.
And "plugin" in that form is Claude Code's format; Cursor, Codex and Gemini each
have their own.

Graphify already solved this and it is the model to copy: **one deterministic
local core that runs standalone, plus a thin wrapper per host.** So the CLI is the
guarantee (it works in CI, where the agent cannot route around it) and the plugin
is the ergonomics. Ship the CLI first — it works in Antigravity without anyone
having verified Antigravity.

Claude Code's contract is verified. **Cursor's and Antigravity's are not** — check
before committing to a surface.

### Phase 6 — packaging

`README.md`, a licence, PyPI, CI. Deliberately last: a README written before
`aop verify` exists would describe something that does not. The package name is
still `aop`, which is a decision nobody has made on purpose.

### Explicitly dropped

**Voice** (audio capture, STT, TTS, barge-in) — it was for a personal desktop
Jarvis and has no place in a verification package. **Perception** (screen capture,
UIA, OCR) — nothing to perceive inside an editor. **System hygiene** — the
allowlist/denylist/quarantine cleaner; `guards/discovery.py` is its read-only
ancestor and is as far as this needs to go. **A learned router** — the audit
settled it on evidence: it would train on a constant.

---

## Open findings

Carried from the 22 Aug audit. Each is real, each is written down so it is not
re-derived, and none is fixed.

**F-03 — the router is a constant function.** The conductor emitted
`difficulty_hint = "medium"` for **13 specs out of 13**, and that field drives the
router's two largest weights (`+0.45` / `−0.25`). Neither ever fires, so `low` was
chosen once in 21 attempts. Slot 40's logic is fine; its input is degenerate. Fix
it at the conductor or not at all — and note this settles the learned router on
evidence rather than argument.

**F-04 — the scope-drift check cannot fire, on two counts.** `check_plan`'s
`allowed_paths` is never passed by any caller, and it compares against
`spec.artifacts`, which is `[]` in all 13 emitted specs — as are `inputs` and
`constraints`. Three fields of the conductor↔worker contract are declared,
versioned, validated, and never populated. A guard that cannot fire is worse than
an absent one, because the architecture says it is covered. **Phase 3 closes it.**

**F-06 — a dollar ceiling does not bound a flat-rate plane.** `per_task_usd` is
`"0.10"`; on the Claude Code plane `impossible-offline` ran **28 minutes and four
attempts** for $3.22 of list-equivalent work. The guard reads billable spend and
flat-rate attempts record `billable = 0`, so nothing intervened. Correct for
money, and it leaves *time* unguarded. If a guard's unit can go to zero, it needs
a second unit.

**48e's second failure mode — the unavailable premise.** `impossible-offline` is
refused today, but on the word *"Improve"*, not on the fact that a live server and
production logs do not exist. Reworded as *"Fine-tune the embedding model on the
production query logs so recall@10 rises 5%"* it would be accepted and burn a
full ladder. **Phase 3 closes it.**

**The escalation ladder has never escalated, and tiering is deferred by
decision.** `high` and `max` in `config/registry.toml` are byte-identical; all
three `config-claude` rungs are Sonnet. Every "full ladder climb" in the history
below was N attempts at *one model* with the failure appended. Treat the ladder as
a retry counter until a genuinely stronger `max` exists. **Do not reason from it.**

---

## Rules that must not be broken

These are cheap to enforce and expensive to get wrong. Most have a named test.

### Guards

| Rule | Why it bites |
|---|---|
| Code references **roles** (`conductor`/`low`/`high`/`max`), never model names | A model literal outside `config/` breaks the swap-in-config premise the registry exists for. `test_no_model_name_appears_outside_config` scans `src/` |
| Guards are **deterministic and zero-token** | No model call in a guard path, ever. A guard that costs tokens is not a guard — it is a second opinion, and it can be talked out of its answer |
| A **guard trip is not a verifier failure** | It must not escalate a tier and must not become a router training label. `FailureClass` carries this on the type — consult it, don't re-derive it |
| Nothing writes outside the **jail root** | |
| The `<Directive>` is **immutable**, hashed at creation | Recovery must reproduce the hash, never recompute it |

**A second guard reuses the first one's syntax check; only containment differs.**
`DiscoveryScope` has a completely different rule from `PathJail` — an allowlist of
roots instead of one root — but UNC, drive-relative, device names, ADS and NUL are
properties of Windows paths, not of any particular jail.
`reject_dangerous_syntax` is module-level and both call it, because two copies
drift and the one that drifts is the one nobody runs an escape suite against. Same
reasoning as the Claude Code hook calling `resolve_for_write` instead of restating
the rule as SDK deny globs.

**The denylist is checked before the allowlist, always.** Otherwise adding `~` as
a discovery root silently re-exposes `~/.ssh`. And it is applied to every
*result*, not just the search root — a walk that only checked where it started
follows a junction straight out of the allowlist and reports what it finds there.

**A result cap does not bound a search that finds nothing.** `locate` over a real
home directory had not returned after two minutes: `os.walk` keeps traversing
until something matches. Depth and a clock are what bound it, and
`LocateResult.truncated` reports which one was hit — *"no matches"* and *"gave up
after five seconds"* are different answers, and a model told the first stops
asking.

**Anything durable that a worker could rewrite is a way to pass without working.**
The state database sits outside the jail; the journal sits inside it (so it is
readable) but is frozen. Apply the same test to anything new.

**On Windows, `env` does not choose the executable.** `CreateProcess` searches the
calling process's PATH, not the environment block you pass. `WindowsBackend`
resolves the program itself for this reason — do not "simplify" it back.

**`python` resolves to the operator's own interpreter**, not whatever is first on
PATH (`execution.python` overrides it). Bare `python` is usually a system install
with no pytest, so the gate reported "could not run the suite" on every attempt.

### The gate

**A spec with no acceptance criteria is refused, not warned about.** On the first
live run a conductor emitted `acceptance: []`; authorship found nothing to write,
froze no test file, and the implementer wrote both the code and the tests it was
graded by. pytest passed and the task reported success. Empty criteria do not make
the gate vague — they disable it while still reporting a pass.

**A failed exit code is not automatically a verdict.** `python -m pytest` exits 1
both when tests fail and when pytest is not installed. Before classing anything as
a *verifier* failure, confirm the check actually ran — otherwise a broken
environment escalates tiers and poisons the router. Distinguish "the model was
wrong" from "our tooling broke", because only the first may escalate or train.

**The model's own rationale never grants approval.** `check_plan` is deterministic
and can refuse a plan; the conductor's stated reasoning is recorded for audit and
carries no weight. Same shape as the journal: the machine-readable half is
authoritative, the prose is for humans.

**When refusing work, over-refusing is the worse failure.** A gate that accepts a
vague directive at least produces something to argue with; one that rejects real
work fails silently and the user stops trusting it. The first 48e gate refused
*"delete the unused imports to clean up the module"* — an evaluative word used as
the *motivation* for a concrete deliverable. That is why the check triggers on an
evaluative term with **nothing anywhere to check it against**, never on the term
alone, and why `require_falsifiable_directive` is policy rather than a constant.

**Escalation advances only on `Verdict.FAIL`.** A worker's own claim about how it
did carries no weight.

**A tier change and a vendor change are different moves.** `VERIFIER` escalates
*up* the ladder; `TRANSPORT` (quota, credit, transport death) moves *sideways* to
the next vendor and must never escalate or train. Confuse them and a Monday
subscription reset reads as "the cheap tier failed four tasks in a row".

**Guard and transport failures count toward the attempt cap but never toward the
escalation counter.** Drop the first half and a worker looping on jail escapes
never terminates; drop the second and it climbs the ladder while doing so.

### Cost and measurement

**The conductor wakes at four checkpoints and nowhere else.** `WAKES_CONDUCTOR`
lists every event and whether it may; anything else raises `NotACheckpoint`. If
you want to add one, that is a cost decision, not a refactor — this is the single
biggest dial on the bill.

**Measured on real traffic: the conductor is ~60% of a task's cost** — 3,605
output tokens for planning against 338 for the work. The lever is how often the
conductor thinks, not which tier the router picks. It is also why a conversational
surface must never run on a coding harness.

**Escalation does not call the conductor.** Re-dispatch the same spec one tier up
with the reason appended. `replan_on_escalation` exists and is off.

**Every billable call goes in the `spend` ledger, not just attempts.** Cost used
to be summed from `attempts`, so it saw execution only — planning and authorship
were unrecorded and outside the budget ceiling, exactly backwards. `attempts`
remains the router's training set; `spend` is the bill, and the guard reads the
bill.

**Report real money and list price side by side, never one instead of the other.**
At flat rate the marginal price is zero and the list-equivalent is what the work
would cost to reproduce without the subscription. Collapsing them produced both
opposite errors: $10.81 read as money, and $0 read as "free, therefore unbounded".

**A ceiling that only counts dollars stops guarding a flat-rate plane.** See F-06.
If a guard's unit can go to zero, it needs a second unit.

**An outage is not a result, and the measuring instrument must know it.** The
first baseline reported 6/11 = 55%; four of the five "failures" were
`TransportError` and had never been graded. The true figure over what ran was 6/7.
`tasks.failure_class` carries it now, `RunReport.pass_rate` divides by **graded**,
and `Comparison.comparable` refuses a verdict when two runs graded different task
sets — which matters because Claude Code runs locally and *cannot* suffer a
DeepSeek DNS failure, so every incumbent outage would otherwise read as candidate
skill.

**A long run must survive being interrupted.** `aop eval` writes `<out>.partial`
after **every** task, atomically, and resumes from it. Tasks killed by the wire
are deliberately **re-run rather than restored**, because restoring them would
bake the outage into the report permanently.

**The wire is not evidence, so retry it — in the adapter, not per call site.**
Three eval runs died because one dropped connection killed a task: the ladder had
retried `TRANSPORT` since Slot 16, but the conductor and test-author had no
equivalent, so a blip during *planning* was fatal. Deliberately narrow — only
`httpx.HTTPError`; a 4xx is a `ProviderError` and is **not** retried, because
repeating a request the server actively refused just spends money to be told no
again.

### Execution planes

**The provider decides the plane, resolved per dispatch.** `ProviderRoutedPlane`
reads `Registry.provider(role)` through the active-vendor pointer, so a failover
from `claude_code` to an HTTP vendor swaps the *plane* too. Slot 48c moved only
the model id, which handed Claude Code a DeepSeek model to run.

**An unbuilt or unavailable plane raises rather than falling back.** A run that
quietly used the internal plane while the report said `claude_code` is not an
execution bug — it is a wrong answer to the question the eval exists to settle.

**A report must name the plane that ran it, not the plane in config.**
`RunReport.roles` names *models*, and two runs can share every model id while
measuring different implementation loops.

**The logbook records what served, not what was configured.** `served_model_id`
comes off the plane, never from `registry.model_id(role)`. They diverge the moment
failover happens, at which point recording the registry's opinion would label
every attempt with a model that never ran — and `training_rows()` hands exactly
those rows to the router.

**The active-vendor pointer is process-wide, not per-task.** Running out of credit
is a property of the vendor and the key, not of the task that discovered it.
`test_the_vendor_pointer_is_process_wide` exists because this is the obvious thing
for a later refactor to "clean up". Related: the fallback chain is **flat**, since
a tree has no answerable "which vendor is next".

**Authorship keeps the internal worker whichever plane is selected.** The test
author and the implementer must not be the same actor; putting them on different
planes entirely is a stronger separation than the frozen file alone.

**Do not add a real `api_key_ref` or `base_url` to a registry as a convenience.**
The loader rejects pasted secrets, but a model choice is a decision, not a side
effect.

### Testing discipline

**Run the pipeline, not just the tests.** Every real bug found so far passed the
whole suite first: a cost of `0E-10`; an event stream claiming four attempts
against three logbook rows; a router demoting all ordinary work to the cheap tier;
`provider = "mock"` ignored outside the tests; and a `locate` that never returned.

**A component that is green in isolation may still be wired to nothing.** The
audit after Slot 42 found `due_for_resume()`, `lifecycle.resume()` and the whole
suspension mechanism consumed by *nothing*. When finishing a slot, check who calls
it, not just that it passes.

**A test that builds the record itself cannot catch a missing producer.**
`billable_cost_usd` had a field, a consumer, and green unit tests that constructed
`TaskResult(...)` by hand — including one using the literal numbers `10.81` and
`0.31`. Nothing ever set it in `_run_one`, so every real run reported list price
as money and the comparison inverted. The tests were green because they *were* the
producer.

**A rule scored on the cases it was derived from is not measured, it is fitted.**
The first 48e lever separated the shipped suite **11/11** and scored **12/20** on
twenty directives written before it existed. The replacement scores 20/20 on the
same held-out set, and that number means something only because the first rule
died on it. `evals/holdout-directives.toml` keeps them; anything added afterwards
to fix a discovered bug is marked `heldout = false`, because it is a regression
test and not evidence.

**On Windows, test link escapes with a junction, not a symlink.** `symlink_to`
needs `SeCreateSymbolicLinkPrivilege`, so `skipif(os.name == "nt")` skips the
escape that matters most on the only platform this runs on. `mklink /J` needs no
privilege and `os.path.realpath` resolves it identically.

**The test suite has its own config at `tests/config/`, always the mock
provider.** It used to load the project's `config/`, which became a liability the
moment a real key was bought: the suite started making live, billable calls and
failed outright without the credential. A suite whose behaviour depends on which
model you happen to have configured is not testing what it thinks.

**Replay matches on a strict request hash.** A cassette miss means a prompt
changed. Re-record; do not loosen the match. A green test replaying the wrong
response is worse than a red one.

**Tests assert the consequence, not the implementation.** See
`test_events.py::test_slow_subscriber_cannot_stall_the_bus`.

---

## Conventions

- **Money is `Decimal`, stored as TEXT.** Never float, never a REAL column.
- **Datetimes are timezone-aware UTC**, stored ISO-8601. Naive values are rejected
  at the schema and at the store boundary.
- **Clock and ids are injected** (`core/ids.py`). Never call `datetime.now()` or
  `uuid4()` inline — determinism tests and transcript replay both depend on it.
- **Schemas subclass `Strict`** (`extra="forbid"`). A typo in a field name should
  fail at construction, not vanish into a dict nothing reads.
- **Every durable record pins `schema_version`.** The task spec is the
  conductor↔worker contract; an unversioned format change silently corrupts the
  router's training set.
- **Docstrings explain the non-obvious *why*.** The existing modules state what
  would break if the rule were violated — match that, don't narrate the code.
- **Tunable values live in `config/policy.toml`**, not in code. Their right
  setting is an empirical question.
- **`src/aop/operator.py` is the composition root** — the one place that knows the
  order things happen in. Nothing there should be logic that is not *sequencing*.

### Working rhythm

One slot per session: one component plus its tests, sized to finish on green.
`pytest` passes before the session ends. Update this file — mark the phase, and if
the build revealed something belongs elsewhere, record the change and the reason.
Deviating from the plan is fine and sometimes correct; write down what changed and
why. **Do not commit or push unless asked.**

---

## What exists

786 tests (829 before Phase 0 removed the shell and its 43). Three config directories, all meaningful:

| directory | what it is |
|---|---|
| `config/` | the daily driver — DeepSeek conductor, Claude Code execution, DeepSeek fallback |
| `config-deepseek/` | **the preserved incumbent.** What `evals/runs/deepseek.json` was measured on. Do not edit, or that report stops being reproducible |
| `config-claude/` | the preserved 48d candidate, no fallback |

### `src/aop/guards/` · `backends/` · `verify/`

| Module | Role |
|---|---|
| `guards/pathjail.py` | Resolve-then-contain. Traversal, symlinks, UNC, device names, drive-relative, ADS. `reject_dangerous_syntax` is shared with `discovery` |
| `guards/discovery.py` | Where a worker may **look**, not write. Allowlist of roots, denylist that always wins, **paths never contents**. Bounded by results, depth and a clock |
| `guards/commands.py` | Allowlist, deny-by-default. argv lists only — no shell anywhere, ever |
| `guards/budget.py` | Per-task and per-day ceilings, checked *before* dispatch |
| `backends/` | `RunBackend` + Windows and WSL impls, guards wired in by construction |
| `verify/` | Gate + registry, static/stateful split, pytest gate, mechanical poller |

### `src/aop/core/`

| Module | Role |
|---|---|
| `ids.py` | `Clock` / `IdSource` protocols; `FrozenClock`+`SequentialIds` for tests |
| `schemas.py` | `TaskSpec`, `Attempt`, `Verdict`, `Observation`, `Task`, `FailureClass`, `Role` |
| `config.py` | Loads `registry.toml` + `policy.toml` into typed settings, validated eagerly with field paths |
| `failures.py` | The decision table: (failure class, attempts, policy) → action |
| `events.py` | Async pub/sub. **Lossy by design** — publishers never block on a subscriber |
| `state.py` | SQLite: tasks, attempts, migrations, spend. `training_rows()` filters ineligible labels at the read |
| `lifecycle.py` | Legal transition map, suspend/resume, orphan recovery after a crash |
| `scheduler.py` | The loop that consumes pending and due work |
| `journal.py` | `OPERATOR.md` — deterministic, generated from state, never model-written |

**The journal is the failsafe:** prose for humans, a fenced `aop-state` JSON block
that is the authority. Delete the database and `Journal.recover()` rebuilds from
the markdown alone. Both halves come from one snapshot, so they cannot disagree.

**The event bus is lossy on purpose.** Blocking a publisher until the slowest
consumer catches up would let a wedged UI wedge the orchestrator. Durability is
SQLite's job.

**The scheduler owns execution. There is one way to start work.** `submit()`
queues, `run_directive()` submits and waits, and the loop claims. Calling
`Operator.run()` directly races the scheduler — use `start(run_scheduler=False)`
to drive the pipeline by hand. **A resumed task re-runs from the top**; there is
no continuation record, which is why mid-ladder suspension is not wired in.

### `src/aop/conductor/` · `router/` · `execution/`

| Module | Role |
|---|---|
| `conductor/rationale.py` | Deterministic `check_plan` enforces; model prose only audits. Holds `falsifiability()` |
| `conductor/directive.py` | Hash re-checked at every checkpoint |
| `conductor/taskspec.py` | Structured emission with a repair loop; invalid never goes downstream |
| `conductor/checkpoints.py` | Four checkpoints as data; anything else raises |
| `conductor/authorship.py` | Separate test-author call, then freeze |
| `router/features.py` | One extractor for rules and classifier alike. `FEATURE_NAMES` is append-only |
| `router/rules.py` | Scores difficulty, then the registry applies modality |
| `execution/plane.py` | `ExecutionPlane` / `PlaneOutcome`, and `ProviderRoutedPlane` |
| `execution/claude_code.py` | Claude Agent SDK plane. `PreToolUse` hook calls `resolve_for_write`; quota → `AdapterError` → `TRANSPORT`. Optional extra |
| `execution/worker.py` | `render_spec` (deterministic, field by field) + one dispatch |
| `execution/tools.py` | read / write / edit / list / run / locate, all guard-wrapped |
| `execution/ladder.py` | Executes the Slot 16 decision table. Escalation calls no conductor |

**A tool problem is a message, not an exception.** Unknown tool, malformed JSON
arguments, a handler that raises, a guard denial — all come back as a structured
tool result the model can correct itself from: cheap, same tier, cache intact,
never an escalation.

**Modality overrides difficulty.** `Registry.tier_for(desired, needs_pixels=)`
routes around text-only tiers so visual work is not quietly downgraded.

### `src/aop/registry/` · `memory/` · `context/` · `evals/` · `service/`

| Module | Role |
|---|---|
| `registry/registry.py` | The only path from a role slot to a model identity, prices and capability tags. Modality-aware tiering, sideways failover, credentials from env by name |
| `registry/cost.py` | `Usage` + `CostModel`, Decimal throughout. **Missing usage raises** |
| `registry/adapter.py` | One HTTP client for every provider, with bounded transport retry |
| `registry/providers/mock.py` | Scripted answers over a real `httpx` transport |
| `registry/providers/replay.py` | Record/replay with strict request-hash matching |
| `registry/toolcalls.py` | `ToolBox` + `run_tools`. Schema emission, dispatch, capped multi-turn loop |
| `memory/logbook.py` | One row per attempt; `tier_stats` for "is `low` earning its place" |
| `memory/store.py` | SQLite FTS5 lexical store. Pruned context only — not a document library |
| `context/assembler.py` | `[prefix ‖ tail]`, `append_failure`, explicit `rebuild_prefix` |
| `context/pruner.py` | Store-before-drop, deterministic summary |
| `evals/harness.py` | Runs a suite, grades with the real gate, reports pass-rate beside cost |
| `service/app.py` | Local HTTP + WS surface. Optional extra (`aop[service]`) |

**The mock speaks HTTP, on purpose.** It is an `httpx` transport returning real
OpenAI-dialect JSON and SSE, so every mock-driven test exercises the adapter for
real. Do not add a bypass path "for speed" — the fast path would become the
default and the faithful one would rot. Two details to preserve: usage appears
**only** when `stream_options.include_usage` was sent, and streamed content
arrives fragmented with tool arguments cut mid-JSON.

---

## History, compressed

Kept so decisions are not re-litigated. Anything here naming a "next slot" is
superseded by the plan above.

**Slots 01–40** built the core on mocks at zero spend: directive → checkpoint →
spec → router → test authorship → ladder → verdict → logbook → journal.

**Slots 41–46b** built a desktop shell — service, overlay, scheduler, frameless
window, tray, hotkey, autostart. **Removed in Phase 0.** The scheduler and service
survived; the rest was Jarvis furniture. Its lasting lesson: the shell was a pure
client that never imported the Operator, which is what let the riskiest platform
work be done last and thrown away cheaply.

**Slot 47** was the buy point. DeepSeek chosen; verified prices and paste-ready
blocks for Kimi and Qwen are in `PRICING.md` in git history.

**Slots 48a–48d** added the execution-plane seam, the Claude Code plane, failover,
and the comparison that settled whether to delegate implementation. On the ten
tasks both planes graded: **DeepSeek 8/10 at $0.4789, Claude Code 9/10 at
$0.2650**. Formal verdict is `NO VERDICT` — the candidate lost a task to a
conductor-side `TransportError`, so coverage was 10 vs 11 and the guard refused.
`aop compare` prints the narrowed answer: **PROMOTE at 0.55x**.

**The 22 Aug audit (J2.5)** found the cost verdict was never computed by anything,
the ladder had never escalated, and the router was a constant function. It also
found the `underspecified` diagnosis was wrong on both halves. Full findings above.

**Slot 48e** built `falsifiability()`. *"Make the retriever better."* is handed
back after **0 attempts and $0**; `aop eval --tag refusal` scores 2/2 in 0.3
seconds, where those two tasks previously cost $0.194 and 41 minutes.

**Slots 49–51** made the plane follow the vendor, promoted the Claude split to the
shipped default, and added the discovery scope.

⚠️ **`evals/runs/deepseek.json` predates 48e**, so a fresh run of the suite is no
longer directly comparable to it on the two refusal tasks. Expect 9/11 → 10/11.
