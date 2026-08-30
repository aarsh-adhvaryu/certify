# certify — working memory

**A deterministic verification layer for coding agents.** It refuses work that
cannot succeed, freezes the success criteria before implementation so the thing
being graded cannot write its own grade, and returns a verdict that distinguishes
*"the model was wrong"* from *"our tooling broke"*.

**It calls no model to do any of this.**

## Three documents, three jobs

| file | job | read it when |
|---|---|---|
| `OPERATOR-v2.md` | the **design** — why this exists and where the boundaries fall | you are questioning a decision |
| `PLAN.md` | the **route** — nine stages, every slot, in order | you are picking up work |
| `CLAUDE.md` | the **memory** — what is true *now*, and what must not be broken | every session |

Do not put plan content here or memory content there. This file is loaded into
context every session; it earns that by being current, not by being complete.

---

## Status

**Phase 0 complete.** 264 tests green, 28 modules. Python 3.13 in `.venv`.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
$env:PYTHONPATH = "d:\orchestrator\src"     # only needed outside pytest
```

`pythonpath = ["src"]` and `asyncio_mode = "auto"` come from `pyproject.toml`, so
async tests need no decorator. `pip install -e .` removes the PYTHONPATH line.

**There is no CLI yet.** `certify` is not a command — `[project.scripts]` lands in
A.1. Anything describing `certify begin` or `certify verify` is describing the
plan, not the code.

**There is no licence, by decision.** Deferred until near the end, which means the
repository is all-rights-reserved and nobody may legally use it. This blocks A.7
(PyPI) and every adoption claim. `README.md` says so plainly; do not let it drift
into being unstated.

---

## What exists

```
src/certify/
  refusal.py    falsifiability() + check_plan()      <- the front door
  session.py    Directive, DirectiveGuard             <- the immutable ask
  criteria.py   freeze_existing()                     <- the freeze
  guards/       pathjail . discovery . commands . budget . denial
  verify/       base . static . stateful . pytest_gate
  backends/     RunBackend + Windows/WSL              <- A.2 makes this portable
  core/         ids . schemas . state . journal . config . events . lifecycle
  hosts/        claude_code.py — the PreToolUse write hook
```

| module | what it does |
|---|---|
| `refusal.py` | `falsifiability()` is pure regex, no imports beyond `re` + schemas. `check_plan()` catches mechanical drift — a plan that grew a file, dropped every criterion, or contradicted its own forbidden list |
| `guards/pathjail.py` | Resolve-then-contain. Traversal, symlinks, UNC, device names, drive-relative, ADS. Holds the freeze. `reject_dangerous_syntax` is shared with `discovery` |
| `guards/discovery.py` | Where an agent may **look**, not write. Allowlist of roots, denylist that always wins, **paths never contents**. Bounded by results, depth and a clock |
| `guards/commands.py` | Allowlist, deny-by-default. argv lists only — no shell, anywhere, ever |
| `guards/budget.py` | Per-task and per-day ceilings, checked *before* dispatch |
| `verify/` | Gate + registry, static/stateful split, pytest gate, mechanical poller |
| `core/state.py` | SQLite at `SCHEMA_VERSION = 5`. Tables: `tasks`, `attempts`, `spend` |
| `core/journal.py` | `OPERATOR.md` — deterministic, generated from state, never model-written |
| `core/lifecycle.py` | Legal transition map, orphan recovery after a crash |

**The journal is the failsafe:** prose for humans, a fenced `certify-state` JSON
block that is the authority. Delete the database and `Journal.recover()` rebuilds
from the markdown alone. Both halves come from one snapshot, so they cannot
disagree.

**The event bus is lossy on purpose.** Blocking a publisher until the slowest
consumer catches up would let a wedged UI wedge everything else. Durability is
SQLite's job.

### Known gaps, each with a test that says so

- **The freeze does not survive the process.** `PathJail` holds its frozen set in
  memory, on the instance. `begin` and `verify` are separate processes, so
  containment is real within one run and absent across two. Closed by **E.1**.
  See `test_criteria.py::test_the_freeze_does_not_survive_the_process`.
- **The write hook is unproven.** `test_hosts.py` proves the hook *works*, not
  that it is *connected*. The previous build's hook was written, unit-tested,
  green — and never passed to the SDK. Closed by **E.3**.
- **`TaskStatus.SUSPENDED` and its two columns are inert.** Suspension went in
  0.3; the durable shape waits for E.1 rather than spending a migration twice.
- **`Role` describes nothing.** conductor/low/high/max were model tiers. Left in
  place until **E.5** builds the ledger line and reveals what the column records.

---

## Where Graphify sits

**Graphify makes the agent stop guessing. certify makes it stop lying.**

Graphify attacks token burn on the *input* side — stop re-grepping. certify
attacks it on the *output* side — don't certify wrong work. A map with no rules
stops nothing; rules with no map cannot tell what you are pointing at.

Two things are independently ours and survive Graphify disappearing tomorrow:
**refusing a directive with no checkable success condition**, and **structurally
separating the author of the criteria from the implementer**. Those are the
pitch. The graph-powered checks are our logic over their data — additions, never
the pitch.

**Verified upstream facts** (fetched, not remembered — the previous notes had two
of these wrong):

| | actual |
|---|---|
| PyPI name | **`graphifyy`** (double-y); the import is `graphify` |
| Licence | Apache-2.0 **and** MIT, dual |
| LLM | **Partial.** Code extraction is local tree-sitter. The semantic pass over docs, PDFs and media **does call a backend** |
| Output | `graphify-out/{graph.json, graph.html, GRAPH_REPORT.md}` |
| Invocation | `graphify extract ./path`, `import graphify`, `python -m graphify.serve` (MCP), `/graphify .` |
| Coverage | 37 tree-sitter grammars |

**Reading the graph needs no dependency at all.** `graph.json` is a file; parsing
it is stdlib JSON. Graphify is needed to *produce* the artifact, never to consume
it — so the guard path imports no third-party code, and CI can produce in one
step and consume in another.

> **certify consumes code-extraction nodes and `EXTRACTED` edges only.** Never an
> `INFERRED` edge, and never anything from the semantic pass. A guard consuming
> an LLM's reading of a PDF would be model-dependent while looking deterministic.

**Adopt it, never reimplement it.** certify ships no alternative and never
competes on the input side. Absent, certify offers to install `graphifyy` —
prompted, never silent, and never in a non-interactive run.

---

## Rules that must not be broken

Most have a named test. Each was paid for.

### Determinism

- **No model call in a guard path, ever.** A guard that costs tokens is not a
  guard — it is a second opinion, and it can be talked out of its answer.
- **The model's own rationale never grants approval.** Prose is recorded for
  audit; the machine-readable half is authoritative.
- **Clock and ids are injected** (`core/ids.py`). Never `datetime.now()` or
  `uuid4()` inline — determinism tests and replay both depend on it.
- **Money is `Decimal`, stored as TEXT.** Never float, never a REAL column.
- **Datetimes are timezone-aware UTC**, ISO-8601. Naive values rejected at the
  schema *and* at the store boundary.
- **Schemas subclass `Strict`** (`extra="forbid"`). A typo should fail at
  construction, not vanish into a dict nothing reads.
- **Every durable record pins `schema_version`.**
- **Migrations are append-only.** Editing an applied migration leaves migrated
  databases silently inconsistent with fresh ones.
- **Tunable values live in `policy.toml`.** Their right setting is empirical.

### Refusal

- **Over-refusing is the worse failure, everywhere.** A gate that accepts a vague
  directive at least produces something to argue with; one that rejects real work
  fails silently and the user uninstalls.
- **Trigger on an evaluative term with nothing anywhere to check it against**,
  never on the term alone. The first rule refused *"delete the unused imports to
  clean up the module"* — an evaluative word used as the *motivation* for a
  concrete deliverable.
- **Empty criteria are refused, not warned about.** They do not make the gate
  vague — they disable it while still reporting a pass.
- **Every refusal names exactly what was missing and offers an escape hatch.**
- **Only refuse on `EXTRACTED` graph edges.**

### Verdicts

- **A failed exit code is not a verdict.** `pytest` exits 1 both when tests fail
  and when pytest is not installed. Confirm the check *ran* — `_actually_ran()`.
- **Distinguish "the model was wrong" from "our tooling broke."** Only the first
  may trigger anything.
- **A guard trip is not a verifier failure.** `FailureClass` carries this on the
  type — consult it, don't re-derive it.
- **The directive is immutable, hashed at creation.** Recovery *reproduces* the
  hash, never recomputes it — recomputing makes every tampered record
  self-consistent.

### Containment

- **Nothing writes outside the jail root.**
- **Denylist before allowlist, always**, applied to every *result* and not just
  the search root. A walk that only checked where it started follows a junction
  straight out of the allowlist.
- **A result cap does not bound a search that finds nothing.** Depth and a clock
  do, and the result reports which bound was hit — *"no matches"* and *"gave up
  after five seconds"* are different answers.
- **Anything durable an agent could rewrite is a way to pass without working.**
  The frozen criteria file is the obvious case; apply the same test to anything
  new.
- **Path-syntax rejection is shared, not duplicated.** Two copies drift, and the
  one that drifts is the one nobody runs an escape suite against.
- **On Windows, `env` does not choose the executable.** `CreateProcess` searches
  the *calling* process's PATH, not the environment block you pass.
- **`python` means the operator's own interpreter.** Bare `python` on PATH is
  usually a system install with no pytest, so the gate reported "could not run
  the suite" on every attempt.

### Cost

- **If a guard's unit can go to zero, it needs a second unit.** A dollar ceiling
  does not bound a flat-rate plane: one task ran 28 minutes for $3.22 of
  list-equivalent work while the guard read `billable = 0`.
- **Every billable call goes in the `spend` ledger**, not just attempts.
- **Report real money and list price side by side, never one instead of the
  other.** Collapsing them produced both opposite errors: $10.81 read as money,
  and $0 read as "free, therefore unbounded".

### Measurement

- **A rule scored on the cases it was derived from is fitted, not measured.** The
  first refusal rule separated the shipped suite 11/11 and scored **12/20** on
  twenty directives written before it existed. `evals/holdout-directives.toml`
  keeps both sets; `heldout = false` marks regression cases, which are not
  evidence.
- **An outage is not a result.** A baseline once read 6/11 = 55%; four of the five
  "failures" were transport errors that had never been graded. The true figure
  was 6/7. Divide by **graded**, and refuse to compare runs that graded different
  task sets.
- **Report the harm metric beside the saving metric.** For the output contract
  that means follow-up rate beside tokens saved — if follow-up rate rises, the
  contract is wrong however good the saving looks.

### Testing

- **Run the pipeline, not just the tests.** Every real bug found so far passed
  the whole suite first: a cost of `0E-10`; an event stream claiming four
  attempts against three rows; a router demoting all work to the cheap tier; a
  `locate` that never returned.
- **A test that builds the record itself cannot catch a missing producer.**
  `billable_cost_usd` had a field, a consumer, and green tests — including one
  using the literal numbers `10.81` and `0.31`. Nothing ever set it.
- **A component green in isolation may be wired to nothing.** Check who calls it.
- **The suite has its own config at `tests/config/`**, and no model configured
  anywhere. It used to load the project's config, which started making live
  billable calls the moment a real key was bought.
- **On Windows, test link escapes with a junction, not a symlink.** `symlink_to`
  needs `SeCreateSymbolicLinkPrivilege`, so `skipif(os.name == "nt")` skips the
  escape that matters most on the platform this actually runs on. `mklink /J`
  needs no privilege and `realpath` resolves it identically.
- **Tests assert the consequence, not the implementation.**

---

## What the orchestrator taught us

The first build was an LLM orchestrator: a conductor model planning, a router
choosing tiers, an escalation ladder, execution planes. It is at the
`v1-orchestrator` tag. The code is not needed. These are.

**Where it was right**

| lesson | what it cost to learn |
|---|---|
| A spec with no acceptance criteria is *refused* | A live run emitted `acceptance: []`; the implementer wrote both the code and the tests grading it. pytest passed, the task reported success |
| A failed exit code is not a verdict | `pytest` exits 1 for "tests failed" and "pytest not installed" alike |
| Over-refusing is the worse failure | The first rule refused "delete the unused imports to clean up the module" |
| A fitted rule is not a measured one | 11/11 on the suite it came from, **12/20** held out |
| A test that builds the record cannot catch a missing producer | `billable_cost_usd`: field, consumer, green tests, no producer |
| Green in isolation is not wired | The whole suspend/resume mechanism was consumed by *nothing*; the jail hook was never passed to the SDK |
| An outage is not a result | 6/11 reported; the true figure over what ran was 6/7 |

**Where it was wrong**

| mistake | what it teaches |
|---|---|
| The router trained on a constant | `difficulty_hint = "medium"` for 13 specs of 13. Check an input has variance before building a learned component on it |
| The ladder never escalated | `high` and `max` were byte-identical. A mechanism nobody exercised was reasoned from for weeks |
| A dollar ceiling on a flat-rate plane | If a guard's unit can go to zero, it needs a second unit |
| Three contract fields declared, validated, never populated | `artifacts`/`inputs`/`constraints` were `[]` in all 13 specs, so the scope-drift guard could not fire. **A guard that cannot fire is worse than an absent one** |
| The conductor was ~60% of a task's cost | 3,605 output tokens planning against 338 doing. The lever was never which tier the router picked |
| A desktop shell built before the core settled | 844 lines deleted. It survived being thrown away *only* because it was a pure client that never imported the core — that seam is the reusable part |

**Explicitly dropped:** voice, perception (screen capture, UIA, OCR), system
hygiene, a learned router, the conductor, the tier ladder, the model registry.

---

## Working rhythm

One slot per session: one component plus its tests, sized to finish on green.
`pytest` passes before the session ends, and the pipeline gets run by hand.

Update this file when something here stops being true. Deviating from `PLAN.md`
is fine and sometimes correct — write down what changed and why.

**Do not commit or push unless asked.**
