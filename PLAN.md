# Pivot: strip the orchestrator, build the verification layer

## Context

`d:\orchestrator` currently holds a working LLM orchestrator — 786 tests, ~13,000
lines, six commits, no remote. It plans with a conductor model, routes across a
tier ladder, dispatches to execution planes, and grades with a gate.

[OPERATOR-v2.md](OPERATOR-v2.md) is a pivot away from that. It keeps one half and
discards the other. The half it keeps is the deterministic part: refuse work that
cannot succeed, freeze the success criteria before implementation so the thing
being graded cannot write its own grade, return a verdict that distinguishes "the
model was wrong" from "our tooling broke". The half it discards is everything
that calls a model to decide something — conductor, router, ladder, registry,
planes.

The audit of 22 Aug is why. It found the router was a constant function (13 specs
out of 13 got the same difficulty hint), the escalation ladder had never actually
escalated (`high` and `max` were byte-identical), and the cost verdict was never
computed by anything. The measurement layer had recorded three findings the code
could not have produced. Meanwhile the guards, the gate and the falsifiability
check worked, cost nothing to run, and needed no API key.

So the new product is a CLI that sits beside any coding agent: it refuses
unfalsifiable directives, freezes criteria, bounds writes, runs the gate, and
reports a verdict plus a ledger line. It calls no model.

Later it consumes [Graphify](https://github.com/Graphify-Labs/graphify)'s code
graph and trims agent verbosity — but the boundary there is load-bearing enough
to get its own section below, because getting it wrong is how this project turns
into a worse copy of a tool that already won the input side.

**The user's decisions, taken as given:**

- The old orchestrator is not needed as code. Git history is the only
  preservation; what carries forward is the *lessons* — what it did, where it was
  right, where it was wrong.
- Surgical strip in place, not a fresh package beside the old one.
- Plan all nine stages in full detail.
- The package is named **`certify`** — verified free on PyPI. The command is the
  sentence: `certify begin`, `certify verify`.

---

## What we keep from the old orchestrator

Not code — judgment. These go into the rewritten `CLAUDE.md` in slot 0.4, because
each was paid for with a real bug.

**Where it was right.**

| Lesson | Cost of learning it |
|---|---|
| A spec with no acceptance criteria is *refused*, not warned about | A live run emitted `acceptance: []`; the implementer wrote both the code and the tests grading it. pytest passed, task reported success |
| A failed exit code is not a verdict — confirm the check *ran* | `pytest` exits 1 both when tests fail and when pytest is not installed |
| Over-refusing is the worse failure | The first falsifiability rule refused "delete the unused imports to clean up the module" |
| A rule scored on the cases it was derived from is fitted, not measured | First rule: 11/11 on the shipped suite, **12/20** on a held-out set |
| A test that builds the record itself cannot catch a missing producer | `billable_cost_usd` had a field, a consumer, and green tests — and nothing ever set it |
| A component green in isolation may be wired to nothing | The whole suspend/resume mechanism was consumed by *nothing*; the Claude Code jail hook was built, unit-tested, and never passed to the SDK |
| An outage is not a result | First baseline read 6/11 = 55%; four "failures" were transport errors that had never been graded. True figure: 6/7 |
| Report real money and list price side by side | Collapsing them produced both opposite errors — $10.81 read as money, $0 read as "free, therefore unbounded" |
| Denylist before allowlist, applied to every result not just the search root | Otherwise adding `~` as a discovery root silently re-exposes `~/.ssh` |
| A result cap does not bound a search that finds nothing | `locate` over a home directory had not returned after two minutes |

**Where it was wrong.**

| Mistake | What it teaches the new build |
|---|---|
| The router trained on a constant | Do not build a learned component before checking its input has variance |
| The ladder never escalated | `high`/`max` identical — a mechanism nobody exercised was reasoned from for weeks |
| A dollar ceiling on a flat-rate plane | `impossible-offline` ran 28 minutes for $3.22 of list-equivalent work and nothing intervened. **If a guard's unit can go to zero, it needs a second unit** |
| Three contract fields declared, versioned, validated, never populated | `spec.artifacts` / `inputs` / `constraints` were `[]` in all 13 specs, so the scope-drift guard could not fire. A guard that cannot fire is worse than an absent one |
| The conductor was ~60% of a task's cost | The lever was never which tier the router picked |
| A desktop shell built before the core was settled | 844 lines deleted in Phase 0. It survived being thrown away *only* because it was a pure client that never imported the core — that seam is the reusable part |

---

## Where Graphify sits

The design document's most important architectural claim, and the one easiest to
blur. Restating it here because the stage list alone does not carry it.

**The division.** Graphify attacks token burn on the *input* side — stop
re-grepping, stop guessing at structure. Certify attacks it on the *output* side
— don't certify wrong work. A map with no rules stops nothing; rules with no map
cannot tell what you are pointing at. Graphify makes the agent stop guessing;
certify makes it stop lying.

**It is upstream, not a component.** Separate project, separately installed. We
do not vendor it, fork it, or rebuild any part of it. Two failure modes get lumped
together as "the model lied" and only one is structural:

- *Hallucinating structure* — calls a method that doesn't exist, assumes a module
  layout. A grounding problem. **Graphify's**, entirely.
- *Claiming completion falsely* — real symbols, real files, writes its own tests,
  passes them, reports done. The graph has no objection because nothing was
  fabricated. **A map cannot detect this.**

The same split governs refusal. "Make the retriever better" — Graphify will
happily show the agent exactly where the retriever is, and every symbol resolves.
The directive is still unfalsifiable, because success conditions are not a
structural property of code.

**So two things are independently ours and survive Graphify disappearing
tomorrow:** refusing a directive with no checkable success condition, and
structurally separating the author of the criteria from the implementer. Those
are Stages C and E, they take no graph, and they are the pitch. Stage F's checks
are genuine additions — our logic over their data — but never the pitch.

### Verified upstream facts (fetched, not remembered)

`CLAUDE.md` is wrong on two of these, so slot 0.4 must correct them.

| | Actual | `CLAUDE.md` says |
|---|---|---|
| PyPI name | **`graphifyy`** (double-y); import is `graphify` | — |
| Licence | Apache-2.0 **and** MIT, dual | Apache 2.0 only |
| LLM | **Partial.** Code extraction is local tree-sitter, no LLM. The semantic pass over docs, PDFs and media **does call a backend** | "needs no LLM" — flatly |
| Written in | Python | — |
| Output | `graphify-out/{graph.json, graph.html, GRAPH_REPORT.md}` | `graph.json` |
| Invocation | `graphify extract ./path` (headless), `import graphify`, `python -m graphify.serve` (MCP), `/graphify .` skill | — |
| Coverage | 37 tree-sitter grammars | — |
| Edges | tagged `EXTRACTED` / `INFERRED` | same |

**The LLM finding changes a rule.** Certify's hardest constraint is *no model call
in a guard path, ever* — a guard that costs tokens is not a guard, it is a second
opinion, and it can be talked out of its answer. If a guard consumed a node
produced by Graphify's semantic pass over a PDF, that guard would be transitively
model-dependent while looking deterministic. So the rule is sharper than the
design document states:

> **Certify consumes code-extraction nodes and `EXTRACTED` edges only.** Never an
> `INFERRED` edge, and never anything from the semantic pass. Refusing real work
> on an inference is over-refusal, which is the worse failure everywhere; refusing
> it on an LLM's reading of a PDF is that *and* a broken determinism claim.

The adapter enforces this by filtering at the boundary, and a test asserts a graph
containing semantic-pass nodes cannot reach any refusal path.

### Where it lands in the tree

`src/certify/graph/` — one adapter, one internal protocol, one null fallback:

```
graph/
  protocol.py   what certify needs: symbol lookup, callers_of, importers_of
  detect.py     which of the four states we are in, and the adoption offer
  produce.py    import graphify, else `graphify extract` via the guarded runner
  graphify.py   reads graphify-out/graph.json, filters to code + EXTRACTED
  null.py       no graph: today's regex referent extraction, and says so
  staleness.py  graph build point vs working tree
```

Everything above programs against `protocol.py`, never against Graphify's schema.
That seam is the whole point: a fast-moving upstream schema is not something to
couple hard to, and when it changes, `graphify.py` changes and `refusal.py`, the
write hook and the gate do not.

**Reading the graph needs no dependency at all.** `graph.json` is a file. Parsing
it is stdlib JSON. Graphify only needs to be *present* to **produce** the
artifact, never to consume it — which means CI can have one step produce it and
certify consume it with nothing shared, and the guard path imports no third-party
code. `certify[graph]` therefore pulls `graphifyy` purely as a convenience for
producing, and `graph/graphify.py` reads the artifact whether or not the extra is
installed.

### Adopt it, don't reimplement it

Graphify is a good tool that already won the input side. Certify never ships an
alternative to it and never competes there. What certify does is make the graph
worth more: three checks Graphify does not perform — refuse a directive naming a
symbol that doesn't exist, bound the write scope, expand the gate to the blast
radius. Our logic, their data.

So the install flow adopts whatever is already there, and offers to fetch it when
it isn't. Four states, all of which must work:

| Found | What certify does |
|---|---|
| Graphify installed, graph fresh | Use it. Nothing to do — the good case |
| Graphify installed, graph missing or stale | Offer to run `graphify extract`. It is local, deterministic and costs nothing, so this is a prompt, not a question of policy |
| **Graphify absent** | Explain what it is — a separate Apache-2.0/MIT project, not ours — and offer to install `graphifyy`. Decline and certify still works, fully, on the null fallback |
| Installed but the schema is one certify doesn't know | Advisory, and say which version it saw against which range it supports. **Never guess at an unknown schema** |

**Installing another project's software onto someone's machine is not a silent
step.** It is prompted, it names what it will install and where, `--with-graph` /
`--no-graph` skip the prompt for scripted use, and a non-interactive run (CI)
defaults to **not** installing. This is the "zero decisions, ask nothing on day
one" rule holding: the question appears when the user reaches for Stage F, never
at first install. Prefer `pip install graphifyy` into certify's own environment;
fall back to `uv tool install graphifyy` when uv is present and pip is not.

Producing the graph has two paths, in order: import `graphify` if it is
importable, else run the `graphify extract` CLI through certify's own guarded
runner. Both feed `protocol.py`. Neither is required to read an existing graph.

And the fallback never goes away. No graph, still works. Stale graph, still works
and says so. Language outside the 37 grammars, still works with less. Install
declined, still works. Degrade, never hard-fail.

### Which stages consume it

| Stage | Uses the graph | Must work without it |
|---|---|---|
| C — refusal | no | **yes — this is the front door and the pitch** |
| E — the gate | E.3 write hook takes a scope if one exists | yes |
| F — the graph | all of it | n/a — F *is* the integration |
| G — quality | G.2 fan-in budget only | yes, G.1/G.3/G.4 are AST-local |
| I — Jarvis | `why` reports what changed since the graph was built | yes |

### Coexistence, since we share the same files

`graphify install --platform <name>` already writes hook and instruction files
into Claude Code, Cursor, Codex and others, and registers an MCP server at
`python -m graphify.serve`. Certify's A.5 install and H.1 plugin write to the same
places. Clobbering a neighbour's hook config is the
one-false-positive-and-uninstall failure wearing a different costume — and here it
would break the tool we depend on.

Five rules, each with a test:

1. **Read before writing.** Parse what is already installed; never truncate a
   config file and never rewrite a key certify did not create.
2. **Certify's additions are marked and namespaced** — a delimited block carrying
   a certify marker — so uninstall can identify exactly its own bytes.
3. **Install is idempotent.** Running it twice changes nothing.
4. **Uninstall reverts only marked blocks.** If a marked block was edited by hand
   since certify wrote it, report and leave it, rather than clobbering.
5. **Never duplicate a registration.** If Graphify's MCP server is already
   registered, use it; do not add a second.

Acceptance test for the pair: install certify over an existing Graphify install,
assert Graphify still runs; uninstall certify, assert Graphify's config is
**byte-identical** to before certify touched it.

**If Graphify ships its own gate**, the differentiation narrows. The defence is to
be the thing already integrated with it rather than the thing duplicating it —
which is why the boundary above is stated as a rule and not a preference, and why
Stages C and E are ordered before F.

---

## Phase 0 — The strip ✅ complete

Goal: a green suite over a tree that contains only what OPERATOR-v2 needs.

**Done.** 786 tests before, 264 after; 28 modules, all importing as `certify`.
The 522 tests that went were tests of deleted code. Two things came out better
than forecast and one worse:

* `backends/` did not need cutting — it imports nothing but `core.schemas` and
  `guards`, so `verify/` had no coupling to break. Kept, and A.2 makes it
  portable rather than replacing it.
* Config surgery went further than planned. `config.py` fell 564 → 240 lines
  once every section describing deleted machinery came out, and `load_settings`
  no longer needs a model configured before anything will load.
* **0.1 deleted the tests for code 0.3 then rehomed.** `test_conductor.py`
  covered `rationale`, `directive` and `authorship`; deleting it left the
  falsifiability check with no tests at all for two slots. Recovered from the
  tag in 0.3. Sequence the test move with the code move next time.

### Slot 0.1 — Tag, then delete ✅

`git tag v1-orchestrator HEAD` first (free, local, recoverable), then delete in
one commit.

**Delete entirely:**

```
src/aop/registry/          ~1,959 lines  the only HTTP egress in the tree
src/aop/execution/          1,527        planes, worker, ladder, tools
src/aop/conductor/            872        minus two files lifted in 0.3
src/aop/memory/               475        logbook + FTS store
src/aop/context/              429        assembler + pruner
src/aop/router/               297        the constant function
src/aop/evals/                667        harness drives Operator; Stage B rewrites it
src/aop/service/              179 + ui/  imports operator; Stage I rebuilds it
src/aop/operator.py            26 KB     composition root, imports all ten packages
src/aop/daemon/                          empty dir, stale __pycache__ only
src/aop/core/scheduler.py                v2 is one-shot: begin / verify
config/ config-claude/ config-deepseek/  three model registries, all moot
evals/shramiksaathi.toml, evals/runs/    reports of a tool that no longer exists
workspace/, state.db, .coverage          runtime state, gitignored
```

**Delete these test files** (16 fully dead + 3 collaterally dead by import):
`test_conductor test_context test_execution test_claude_code test_plane
test_failover test_adapter test_registry test_cost test_toolcalls test_replay
test_evals test_service test_backends test_cli test_discovery test_scheduler`,
and rewrite `test_verify.py` in 0.2.

**Explicitly keep** — this is not runtime state, it is evidence:

- `evals/holdout-directives.toml` — 20 directives written *before* the
  falsifiability rule existed, plus 8 marked `heldout = false` because they are
  regression cases and not evidence. Nothing in `src/` loads it today; Stage C
  builds the scorer.
- `evals/fixtures/bm25/` and `evals/fixtures/gate/` — two small real repos, reused
  as Stage B baseline tasks and Stage E gate fixtures.

Expected after this slot: ~196 of 717 tests still importable, suite red at three
known points, fixed in 0.2.

### Slot 0.2 — Cut the three coupling lines ✅

The exploration found exactly three places where a survivor reaches into a
deleted package. Nothing else in `core/`, `guards/` or `verify/` names one.

1. [verify/pytest_gate.py:30](src/aop/verify/pytest_gate.py#L30) and
   [verify/stateful.py:28](src/aop/verify/stateful.py#L28) import
   `aop.backends.base`. `backends/` is only 398 lines and imports nothing but
   `core.schemas` + `guards`, so **keep it**, collapsed into one portable
   `run/` module (Stage A.2 adds POSIX). Both are re-exported from
   `verify/__init__.py`, so this is an import-time failure, not a runtime one.
2. [core/config.py:538](src/aop/core/config.py#L538) — `load_settings()` cannot
   run without a `registry.toml` naming a model per `Role`. Split it:
   `load_policy()` standalone, `Settings` no longer requires `RegistryConfig`.
   `PolicyConfig` and every sub-policy are already model-free.
3. [core/schemas.py](src/aop/core/schemas.py) — trim `Observation`, `UIElement`,
   `ObservationSource` (perception, explicitly dropped), the `Role` tier ladder,
   and `Attempt.features` (router training set).

Suite green at the end of this slot.

### Slot 0.3 — Rehome the survivors and rename ✅

Move, do not rewrite. Each of these was confirmed **reusable as-is**:

| From | To | Why it survives |
|---|---|---|
| `conductor/rationale.py` | `refusal.py` | `falsifiability()` is pure regex, zero imports beyond `re` + schemas. The single cleanest lift in the repo |
| `conductor/directive.py` | `session.py` | `Directive.of()` hash-pins raw bytes; `DirectiveGuard.verify()` re-checks |
| `conductor/authorship.py` → `freeze_existing`, `default_test_path` | `criteria.py` | Both pure. The model call (`author_acceptance_tests`) does not come |
| `execution/claude_code.py` → `build_jail_hook` | `hosts/claude_code.py` | Takes only a `PathJail`, no registry/config/clock |
| `guards/*`, `verify/base|static|stateful` | stay | Model-free today, verified |

Delete `core/failures.py`'s ladder decision table and `core/lifecycle.py`'s
suspend/resume (no continuation record exists; the v2 CLI does not need one).

Rename the package `aop` → `certify`, in one mechanical pass.

**Target layout** (directories created empty as their stage arrives):

```
src/certify/
  cli.py        begin · verify · check · status · why · doctor · demo · install · uninstall
  session.py    directive hash, criteria path, scope, frozen set — persisted
  refusal.py    falsifiability + referent check
  criteria.py   the three sources, and the freeze
  gate.py       verdict assembly
  guards/       pathjail · discovery · commands · budget · denial
  verify/       base · static · stateful · pytest_gate
  run/          portable guarded subprocess runner
  core/         ids · schemas · state · journal · config · events
  measure/      Stage B     contract/  Stage D     graph/  Stage F
  quality/      Stage G     hosts/     Stage H     jarvis/ Stage I
```

### Slot 0.4 — Rewrite `CLAUDE.md` ✅

Replace it with the v2 memory: the lesson tables above, the merged "Rules that
must not be broken" from both documents, and the new layout. Three documents from
then on, with distinct jobs — `OPERATOR-v2.md` is the design (why), `PLAN.md` is
the route (what, in order), `CLAUDE.md` is the memory (what is true now). Correct
the two Graphify facts recorded wrong, and drop the commands section, which
describes a CLI that no longer exists.

---

## Stage A — Floor

Cross-platform from day one. A single-platform package gets zero adoption.

- **A.1 — Name and identity.** Rename, pyproject metadata, licence. Add
  `[project.scripts]` — there is none today, so `aop` has never been a real
  binary, only `python -m aop`.
- **A.2 — Portable runner.** One `run/` module replacing `backends/`. Preserve the
  Windows lesson: `CreateProcess` searches the *calling* process's PATH, not the
  environment block you pass, so the runner resolves the program itself. And
  `python` must mean the operator's own interpreter — bare `python` is usually a
  system install with no pytest, which made the gate report "could not run the
  suite" on every attempt.
- **A.3 — PathJail on POSIX.** Settles open decision #4.
  [pathjail.py:82](src/aop/guards/pathjail.py#L82) uses `PureWindowsPath`
  unconditionally, which is *over*-strict on Linux, not unsafe — it rejects legal
  colons in filenames. Recommendation: stay paranoid by default, add an opt-in
  relaxation, and test both.
- **A.4 — CI matrix.** linux · macos · windows × py3.11–3.13.
- **A.5 — `install` / `uninstall`, and the coexistence discipline.** Uninstall
  fully reverts, including host hook config, and is on the first screen of the
  README. Cheap exit is what makes people willing to enter something that modifies
  their editor. Build the five coexistence rules in here, not in Stage H — read
  before writing, marked and namespaced blocks, idempotent install, revert only
  our own bytes, never duplicate a registration. Graphify writes to the same
  files, so this is load-bearing from the first install, not a Stage H concern.
  A.5 does **not** offer to install Graphify: that question belongs to F.1, when
  the user reaches for the graph.
- **A.6 — Report mode is the default.** One `mode = report | enforce` honoured by
  every refusal and denial path. Ship warning-before-error, the way linters won.
  One false positive on an enforcing tool means uninstall. Test: in report mode,
  nothing is ever blocked, in any path.
- **A.7 — TestPyPI dry run.** Build, upload, install into a clean venv on all
  three OSes.
- **A.8 — `doctor`.** One command that prints what certify found: host, mode,
  whether hooks are wired, whether Graphify is present and at what schema, whether
  the graph is fresh. It is the surface F.1's four-state ladder reports through,
  and the first thing to ask for in a bug report.

## Stage B — Measurement

Before any behavioural change. This tool refuses unfalsifiable claims; shipping
it on one would be self-refuting.

- **B.1 — Host token accounting. GO/NO-GO.** Open decision #5, and it blocks the
  stage. Spike: read Claude Code's transcript JSONL for per-turn usage. Deliver a
  reader *and* a written note on what is and is not obtainable per host. If no
  credible number exists, Cuts 2 and 3 are unprovable marketing and Stage D
  should not be built.
- **B.2 — The metric set.** Tokens per *completed* task (not per turn — halving
  turn size and doubling turns achieves nothing), wall clock, turns, prose:diff
  ratio, follow-up rate, context growth curve. One versioned report record.
- **B.3 — Baseline suite.** Fixed tasks over `evals/fixtures/`, per host, zero
  behavioural change. This produces the "before" number.
- **B.4 — `compare`, with the comparability guard.** Pass rate divides by
  **graded**, not attempted. Two runs that graded different task sets are not
  comparable and the tool refuses a verdict. Real money and list price side by
  side, never one instead of the other.
- **B.5 — Interruption survival.** `<out>.partial` written atomically after every
  task. Tasks killed by the wire are re-run, never restored — restoring bakes the
  outage into the report permanently.
- **B.6 — Record the baseline** and write the numbers into `CLAUDE.md`.

## Stage C — Refusal (the front door)

Asks nothing of the user, changes no habits, and is the one result already worth
showing.

- **C.1 — `certify check "<directive>"`.** Falsifiability over an argument or
  stdin. Zero dependencies, exit-code semantics, the smallest shippable thing.
- **C.2 — The holdout scorer.** Loads `evals/holdout-directives.toml`, keeps
  held-out rows separate from `heldout = false` regression rows, prints the
  confusion matrix. Nothing loads this file today; it has been scored by hand.
- **C.3 — Refusal messages.** Every refusal names exactly what was missing and
  offers an escape hatch. One test per refusal reason.
- **C.4 — Over-refusal regression suite.** Explicit tests for the known false
  positive: an evaluative word used as the *motivation* for a concrete
  deliverable ("delete the unused imports to clean up the module"). The rule
  triggers on an evaluative term with **nothing anywhere to check it against**,
  never on the term alone.
- **C.5 — Refusal saving, measured** with Stage B's instrument: tokens and minutes
  not spent on correctly refused work.

## Stage D — The output contract

Gated on B.1 succeeding. Behind a single off switch throughout.

- **D.1 — Ceremony rules only.** The always-cut half: opening acknowledgements,
  narrating an edit the diff already shows, restating the request, closing
  recaps, transitional filler. No information is lost, so no classifier is needed
  and this half carries no risk.
- **D.2 — Turn classifier, rules-based.** Settles open decision #7 in favour of
  rules: inspectable, no training set, cannot silently degenerate. Deterministic
  features over the prompt — imperative verb, named symbol present, question
  form, files in scope, whether the previous turn was a correction. Discrete class
  out, never a score thresholded into vagueness. The taxonomy is versioned,
  because changing it changes what every recorded measurement means.
- **D.3 — Fail permissive, and prove it.** Wrongly allowing length costs tokens;
  wrongly trimming a debug answer destroys information and costs a recovery turn.
  Test degeneracy in **both** directions — the degenerate direction must be the
  harmless one.
- **D.4 — Exemptions.** Warnings, flagged uncertainty, security notes,
  destructive-action caveats, "I did not verify this" — never cut, at any turn
  class. Suppressing a caveat to save tokens manufactures the exact failure this
  tool exists to stop.
- **D.5 — One-word override.** "explain", "why", "in detail" lifts the contract
  for that turn. No flag, no config, no restart.
- **D.6 — Budget is a signal, never a wall.** Overruns are recorded for
  calibration. Nothing is truncated mid-answer, ever.
- **D.7 — Ship criterion.** Report follow-up rate *beside* the saving. If
  follow-up rate rises, the contract is wrong regardless of how good the savings
  look — revert.

**Never reduce thinking.** The contract governs what is written, not what is
reasoned. Trading reasoning for brevity buys a confidently wrong answer, which is
the thing this project exists to prevent.

## Stage E — The gate

Opt-in, behind the refusal, because it asks the user to state criteria first and
that is a habit change.

- **E.1 — The session record. This is the real gap.** `PathJail.freeze()` keeps
  its frozen set **in memory, per instance**
  ([pathjail.py:110](src/aop/guards/pathjail.py#L110)), and nothing persists it.
  `begin` and `verify` are separate processes, so the freeze currently evaporates
  between them. Persist directive + hash + criteria path + write scope + frozen
  set. The record lives outside the jail; anything durable the agent could
  rewrite is a way to pass without working.
- **E.2 — `begin --criteria <path>`.** Criteria source 1 only: user-supplied.
  Free, deterministic, highest trust, the documented CI path. Refuse empty
  criteria — empty criteria do not make the gate vague, they disable it while
  still reporting a pass.
- **E.3 — Write hooks.** Port `build_jail_hook`: on `PreToolUse`, resolve the
  target through `resolve_for_write` and return a structured **deny** rather than
  raising, so the agent can self-correct. Two disciplines, both learned here:
  **verify the live hook contract before building** — exact names, payload shape,
  and whether a hook can deny or only observe — and **add a wiring test**, because
  an earlier version of exactly this hook was built, unit-tested, and never passed
  to the SDK, so containment was theatre while the tests stayed green.
- **E.4 — `verify`.** Run the gate. Port `_actually_ran()`: exit 1 counts as a
  test failure *only* if the output matches a pytest summary line. Otherwise the
  verdict is `errored` — our tooling broke, not the model was wrong.
- **E.5 — The ledger line.** `spend_breakdown(task_id)` already returns cost per
  purpose. Add wall clock as a **second unit** beside dollars, because a dollar
  ceiling does not bound a flat-rate plane — the old build watched a task run 28
  minutes for $3.22 of list-equivalent work while the guard read `billable = 0`.
- **E.6 — The journal.** Prose for humans, a fenced machine-authoritative block
  that is the sole authority, both rendered from one snapshot so they cannot
  disagree. Byte-deterministic, and frozen so the agent cannot rewrite it.
- **E.7 — Criteria sources 2 and 3.** Source 2 (mechanically derived from the
  graph, for narrow directives) lands in Stage F. Source 3 (one cheap model call,
  a different actor from the implementer) settles open decision #3 the way the
  document leans: **out of v1**, shipped as an opt-in extra with a user-supplied
  key. When no source is available, `begin` says so plainly and runs advisory. It
  never silently pretends to gate.
- **E.8 — `demo`.** Reproduces one specific before/after number locally in two
  minutes. Reproducibility is the entire trust unlock for a tool whose pitch is
  "do not believe confident claims".

## Stage F — The graph

See **Where Graphify sits** for the boundary and the dependency posture. This
stage is our logic over their data, never the pitch.

- **F.0 — Verify the upstream contract before building.** Install `graphifyy`,
  run `graphify extract` over this repo, and read the real `graph.json`: node and
  edge shapes, how code-extraction nodes are distinguished from semantic-pass
  nodes, where the build point / commit is recorded, what a 37-grammar miss looks
  like. Write the observed schema down. Same discipline as the hook contract in
  E.3 — do not design against a remembered schema, and note that the design
  document's "needs no LLM" was already wrong.
- **F.1 — Detect and adopt.** The four-state ladder: Graphify present with a fresh
  graph, present with a stale or missing one, absent, or present at a schema
  certify does not know. `graph/detect.py` reports which state, and `certify
  doctor` prints it in plain words. The absent case explains what Graphify is,
  that it is a separate project, and offers to install `graphifyy` — prompted,
  never silent, `--with-graph` / `--no-graph` for scripts, and **no install in a
  non-interactive run**. `graph/produce.py` runs the extract, by import if
  importable and by CLI through the guarded runner otherwise. Pin the supported
  schema range here; an unknown version is advisory, never a guess.
- **F.2 — Adapter, protocol and fallback.** `graph/protocol.py` is what certify
  needs; `graph/graphify.py` reads `graphify-out/graph.json` and **filters to
  code-extraction nodes and `EXTRACTED` edges at the boundary**; `graph/null.py`
  is today's regex referent check. Reading imports no third-party code. With no
  graph, degrade and say so. Test: a graph carrying semantic-pass nodes cannot
  reach any refusal path.
- **F.3 — Staleness is a state, not an error.** A graph built three commits ago is
  old, not wrong. Compare its build point against the working tree; if stale,
  degrade to advisory and say so. Never silently trust a stale map, never
  hard-fail on one.
- **F.4 — Referent check.** The highest-value single use. The hard case is not
  "make it better" — it is a directive that is perfectly concrete and refers to
  things that do not exist. "Fine-tune the embedding model on the production
  query logs so recall@10 rises 5%" reads as real work and burns a full cycle.
  **Only refuse on an `EXTRACTED` edge**, never on Graphify's own inference.
  Re-score the Stage C holdout set and expect that case to flip.
- **F.5 — Write scope from the graph.** Named symbols → one or two hops on call
  and import edges → the legal write set, fed to the write hook. An agent that
  cannot wander into unrelated files also cannot burn context reading them.
- **F.6 — Blast radius.** Every caller of every changed symbol, and the gate
  requires those tests too. "Your test passed and you broke four callers" is a
  verdict nothing else in this space produces.
- **F.7 — Measure the context growth curve** before and after.

## Stage G — The quality axis

The genuinely new half, and nothing built touches it. The gate is pass/fail
against acceptance criteria; a passing implementation can be O(n²), allocate in a
loop, or be unreadable, and the gate certifies it happily.

- **G.1 — Complexity budget.** Deterministic over the AST: cyclomatic complexity
  and nesting depth ceilings per changed function, configured in policy.
- **G.2 — Fan-in budget**, from the graph.
- **G.3 — Frozen benchmark assertions.** Reuse the E.1 freeze exactly: a benchmark
  file is frozen the way criteria are, the gate runs it, and the bound is
  asserted. Same mechanism, second axis.
- **G.4 — Diff-churn ceiling** — lines touched against task size.
- **G.5 — All four in report mode first**, per A.6, and promoted individually.

## Stage H — Other hosts

Only once one host is finished. "Works with Claude Code. Cursor next." reads
honest; four hosts listed reads as tested on none.

- **H.1 — Claude Code, finished.** Plugin packaging and docs. A plugin is
  packaging, not enforcement: `hooks/` fire deterministically and `.mcp.json` does
  not — an MCP tool the model chooses whether to call is not a gate.
- **H.2 — Contract discovery.** A written per-host table for Cursor, Codex and
  Antigravity: hook names, payload shape, deny or observe only. Verified, not
  remembered. Nothing is claimed as supported until it is tested.
- **H.3 — The CI wrapper.** A GitHub Action plus a generic `verify` build step.
  This is the strongest claim available — in CI the agent cannot route around it —
  so lead with it. Document the two-step form where a `graphify extract` step
  produces the graph and a `certify verify` step consumes it: nothing is shared
  between them but a file, which is the cleanest demonstration that reading the
  graph needs no dependency.
- **H.4 — Coexistence, per host.** A.5's five rules are generic; each host needs
  them proven against a real Graphify install of that host. Per host: install
  Graphify, install certify, assert both work, uninstall certify, assert
  Graphify's config is byte-identical. A host that cannot pass this is not
  listed as supported.
- **H.5 — One additional host**, chosen by what H.2 finds.
- **H.6 — An honest support table** in the README.

## Stage I — Jarvis

Most of what people want from Jarvis is not answering — it is presence: knowing
where you are in the work, what is frozen, what is in scope, what failed and why.
That is *state*, and state is free.

**The rule that keeps it free: Jarvis reads state and never plans.** The moment it
reasons about what you should do next it becomes a planner, and planners are the
single most expensive thing in an agent loop.

- **I.1 — `status` and `why`.** Answered from the journal and the graph. What is
  frozen, what is in scope, the last verdict, what changed since the graph was
  built, what has been spent. Zero tokens.
- **I.2 — A local surface.** TUI or HTTP/WS, rendering the same state live. It
  **must never import the core** — client only. That seam is precisely what let
  the old desktop shell be built last and thrown away cheaply in Phase 0.
- **I.3 — Cross-session recall**, lexical over the journal. Zero tokens if
  lexical.
- **I.4 — Voice.** Last, optional, possibly never. Nothing above it may depend on
  it.

---

## The name — settled

**`certify`.** Verified free on PyPI at time of writing; claim it in A.1 before
anything else in Stage A, because the rename touches every file.

The command reads as the sentence: `certify begin "…"`, `certify check "…"`,
`certify verify`. In a CI file it is one line — `- run: certify verify` — which is
the surface the strongest enforcement claim rests on.

Rejected as taken: `falsify`, `testify`, `litmus`, `verdict`, `gavel`,
`touchstone`, `assay`, `crucible`, `vouch`, `attest`, `arbiter`, `groundtruth`.
Free but not chosen: `attestify`, `refute`, `unbluff`, `proofify`, `verdictor`.

Remaining open decisions from the design document, and where each is settled:
**#3** cheap criteria-authoring call → out of v1, opt-in extra (E.7). **#4** POSIX
path containment → paranoid by default with an opt-in relaxation (A.3). **#5**
per-host token accounting → the go/no-go spike (B.1). **#6** Graphify → adopted if
present, offered if absent, never required (F.1). **#7** turn classifier → rules,
not a trained model
(D.2). **#2** personal project or adopted tool → answer it after Stage E, against
real users rather than against this plan.

---

## Verification

Phase 0 and every stage end the same way: `pytest` green, and the pipeline run by
hand — not just the suite. Every real bug in this repo's history passed the whole
suite first.

**Phase 0.**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
python -c "import certify; import certify.guards, certify.verify, certify.core"
```

Then grep the tree for any surviving reference to a deleted package — the
strip is only done when `registry`, `router`, `execution`, `conductor`, `memory`,
`context`, `operator` appear nowhere in `src/` or `tests/`.

**Per stage.**

| Stage | End-to-end check |
|---|---|
| A | Clean venv on Linux, macOS and Windows: install, run `--help`, `uninstall`, confirm nothing is left behind |
| B | Run the baseline suite twice; the two reports agree. Kill a run mid-suite and confirm it resumes from `.partial` |
| C | `check` against `holdout-directives.toml` — the held-out rows are the score, the regression rows are not |
| D | Baseline vs contract on the same suite; saving **and** follow-up rate both reported. Flip the off switch and confirm behaviour returns to baseline exactly |
| E | Real repo, real directive: `begin --criteria …`, have an agent try to write the frozen file (must be denied), `verify`, read the verdict and ledger line. Then delete the state database and confirm the journal rebuilds it |
| F | Five ways: with a fresh graph, without one, with a deliberately stale one, with Graphify absent and the install declined, and with a graph at an unknown schema version. All five must work; four of them must say which they are |
| F+ | On a machine with no Graphify: `certify doctor` reports absent, the install offer appears, `--no-graph` suppresses it, and a non-interactive run never installs anything |
| G | A passing-but-slow implementation must be caught by G.1/G.3 and must only *warn* in report mode |
| H | The CI wrapper fails a build on a real gate failure, in the two-step form where `graphify extract` and `certify verify` share only a file |
| H+ | Per host: Graphify installed, then certify installed — both work; certify uninstalled — Graphify's config byte-identical to before |
| I | `status` and `why` with the core uninstalled from the surface's import path — proving the client seam holds |

**Degeneracy tests, at every stage that can refuse or trim.** No graph, stale
graph, unsupported host, report mode, classifier unsure: the tool still works, and
says which of those it is.
