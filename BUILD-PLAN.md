# Agentic Operator — Build Plan

*Companion to `agentic-operator-spec.md`. The spec is the source of truth for the system's design; this is the plan for building it.*

## Context

`d:/orchestrator` contains one file: `agentic-operator-spec.md`. Everything below is greenfield.

The spec describes an always-on OS-level daemon that executes project work — a conductor model orchestrating a tiered pool of execution workers behind a deterministic verifier gate. Standalone product, not a Claude Code extension.

Decisions from planning:

| | |
|---|---|
| **Scope** | The final system, sequenced by dependency. No throwaway v0. |
| **Keys** | None yet. Mock provider is built first, as a real registry role — the whole control plane is testable at zero spend. Buy point is explicit (Slot 41). |
| **First verifier** | Coding, gated by pytest. |
| **Autonomy** | Path-jailed workspace, auto-run, **guard layer must be zero-token**. |
| **Unit of work** | Tight slots — one component plus its tests, one slot per session, always ending green. |
| **WSL** | Execution is a pluggable backend, Windows default. Perception stays Windows-native. |

Environment: Python 3.13.9 and git present; no Node, npm, or uv. Not yet a git repo.

---

## Additions to the spec

**From you, during planning** — treated as design defaults, not law:

- **Cache-as-latency-defense.** Appending a verifier failure to the volatile tail keeps the cached prefix valid, so the provider skips prefill — ~3s TTFT drops to ~200ms on retry. Note the limit: *strict* append-only can't hold, because pruning necessarily rewrites the prefix. The workable rule is narrower — rebuild only at explicit, logged checkpoints (a prune, a plan revision), never as an incidental side effect of a retry.
- **Static vs. stateful verifiers.** Static (SymPy, AST, syntax) are sub-second — loop immediately. Stateful (port binding, `npm install`, HTTP 200) take seconds to minutes; run them async, poll mechanically, and don't let a model sit burning context. Below a threshold (default 2s) awaiting inline is simpler than serializing to disk and back, so suspension is a policy knob rather than an absolute.
- **Stream the chain of thought.** Trust degrades against a static screen. Raw tokens and gate events (`[FAIL: retrying syntax error]`) render live — which makes the event bus a day-one component.

**Added during planning:**

- **The markdown failsafe (Slot 07).** A plain-markdown journal at `workspace/<project>/OPERATOR.md`, **generated deterministically from SQLite on every checkpoint — never written by a model**, so it costs nothing and cannot hallucinate. Holds the Directive verbatim, the current plan, task states, decision log, recent verdicts, open questions. It buys four things: recovery when the database or vector store is corrupt; a human-readable window into what the daemon believes; a bootstrap context file pasteable into *any* model, including a fresh conductor; and, because it is human-editable, a steering seam where you correct the file and the orchestrator re-ingests. It comes early on the principle that a failsafe must exist before the things it catches.
- **Test-authorship separation.** If the worker writes both code and tests, the gate is theater — a model that can't solve the task can always write tests that pass. Acceptance tests are authored *before* the implementer is dispatched, and the test file is **write-denied to the implementer at the path-guard level**. A guard, not a prompt instruction.
- **Budget guard.** §7 names cost runaway as risk #1 but mitigates it with discipline; discipline is not a mechanism. Per-task and per-day ceilings, priced from the registry, hard-stopped by the orchestrator.
- **The escalation ladder is the router's data-generating process.** The router only sees a verdict for the tier that actually ran, so if `low` never runs on hard tasks you never learn it fails there. A task that fails at `low` and passes at `high` labels both tiers at once. Logbook is therefore **one row per attempt**, not per task.
- **Guard trip ≠ verifier failure.** Separate classes, separate budgets. A guard denial returns as a tool-error to the same tier; it never escalates and never becomes a router training label. Otherwise a path typo promotes the task to a pricier tier for a reason unrelated to difficulty, and poisons the labels doing it.
- **Version the task-spec schema.** It's the conductor↔worker contract and it will change. Pin the version in every logbook row or a format change silently corrupts the training set.

---

## Hard rules vs. tunable defaults

Cheap to enforce, expensive to get wrong — these are structural:

| Rule | Enforcement |
|---|---|
| Code references roles (`conductor`/`low`/`high`/`max`), never model names | Registry lookup is the only path to a model id; no model literals outside `config/` |
| Guards are deterministic and zero-token | Pure functions; no model call anywhere in the guard path |
| Guard trips never escalate and never train the router | Distinct `FailureClass`, excluded at the logbook write |
| Escalation advances only on `Verdict.FAIL` | Worker self-report cannot move the ladder |
| Nothing writes outside the jail root | Path guard wraps every filesystem tool |
| Pruning stores before it drops | Pruner writes to the vector store first, then trims |
| Original intent is immutable | `<Directive>` block, hash-checked each checkpoint |

Everything below lives in `policy.toml` as a knob, because the right value is an empirical question:

`append_on_retry` (default on) · `suspend_threshold_seconds` (2) · `stream_tokens` (on) · `max_attempts` (4) · `retries_before_escalation` (1) · `budget.per_task` / `budget.per_day` · `effort.default` (low) and its escalation triggers · `a11y_coverage_threshold` · `prune_trigger_tokens`

---

## Layout

```
d:/orchestrator/
  config/
    registry.toml            # role -> {provider, model_id, base_url, api_key_ref,
                             #          price_in, price_out, params, capability_tags}
    policy.toml              # jail root, backend, allowlists, budgets, caps, knobs above
  src/aop/
    core/       schemas.py ids.py config.py events.py state.py
                lifecycle.py journal.py loop.py
    registry/   registry.py adapter.py cost.py shims/ providers/mock.py providers/replay.py
    guards/     pathjail.py commands.py budget.py
    backends/   base.py windows.py wsl.py          # RunBackend + path translation
    verify/     base.py static/ pytest_gate.py stateful/poller.py
    context/    assembler.py pruner.py
    execution/  worker.py ladder.py tools/
    conductor/  directive.py taskspec.py checkpoints.py effort.py
    router/     features.py rules.py classifier.py
    memory/     store.py logbook.py
    service/    app.py ui/
    daemon/     tray.py hotkey.py capture.py a11y.py perception.py
  tests/
  workspace/                 # the jail root — nothing writes outside this
```

**Dependencies:** `httpx`, `pydantic` v2, `fastapi` + `uvicorn`, `pytest`, `aiosqlite`; later `scikit-learn`, `pywebview`, `pystray`, `pywin32`, `uiautomation`, `mss`, `rapidocr-onnxruntime`. Plain `venv` + `pip` with a `pyproject.toml`.

Memory sits behind an interface with a local vector store as default — Mem0 is a swappable implementation, not a hard dependency.

---

## Slots

One slot = one session, ending on green tests. Slots 01–40 are specified; 41+ are outlined because their detail depends on data and prompts that don't exist yet.

> **Current position: Blocks A–H complete (Slots 01–40). 549 tests green.**
> **Everything before the buy point is done. Next up: Slot 41 — verify prices and model ids, then buy one key.**
> See [CLAUDE.md](CLAUDE.md) for the per-session working rules.

### A — Foundations (no model calls) — ✅ complete, 152 tests green

| # | Slot | Done when | Deps |
|---|---|---|---|
| 01 | ✅ Repo init, `pyproject.toml`, venv, pytest wiring | `git init` done, `pytest` runs clean | — |
| 02 | ✅ Config loading — both TOMLs → typed settings | Malformed config fails with field-level errors, not at first use | 01 |
| 03 | ✅ Core schemas — `TaskSpec` (versioned), `Attempt`, `Verdict`, `Observation`, **`FailureClass`**, **injectable clock/ids** | Round-trips serialize/deserialize; `schema_version` pinned in every record | 01 |
| 04 | ✅ Event bus — async typed pub/sub | Fan-out correct; one slow subscriber cannot stall the bus | 03 |
| 05 | ✅ SQLite state store — schema, migrations, CRUD | Migration applies cleanly to an empty and a populated DB | 03 |
| 06 | ✅ Task lifecycle — suspend/resume across process restart, orphan recovery | Kill mid-task, reopen, state is identical | 05 |
| 07 | ✅ **Journal writer — deterministic `OPERATOR.md` from state** | Journal rebuilds byte-stable from a DB fixture; a task rehydrates from the journal with the DB deleted | 05, 06 |

**Three changes made during the build**, each because a slot needed something an
adjacent slot was holding:

- **`FailureClass` moved from Slot 16 into Slot 03.** `Verdict` and `Attempt`
  both reference it, so leaving it in Block C meant defining those schemas around
  a type that did not exist. It is a pure enum with no dependencies. Slot 16 is
  now *wiring* the taxonomy through the ladder rather than inventing it. The
  consequences live on the type itself — `escalates`, `trains_router`, `halts` —
  so the ladder and the logbook consult one definition instead of each
  remembering the rule.
- **The journal carries a fenced `aop-state` JSON block.** Slot 07's stated test
  is rehydrating with the database deleted; against prose alone that means a
  parser that reverse-engineers English, which works in a test and fails in
  reality. Prose and fence are generated from the same snapshot, so they cannot
  disagree, and the fence is the documented authority.
- **Clock and id generation are injected** (`core/ids.py`). "Byte-stable from a
  fixture" is untestable when `datetime.now()` and `uuid4()` are called inline,
  and retrofitting it later would have meant touching every schema. Replay
  (Slot 14) needs the same seam.

Also added beyond the slot list: `core/lifecycle.py` holds the legal-transition
map and crash recovery, keeping the store as pure persistence.

### B — Registry & adapter (mock only, zero spend) — ✅ complete, 285 tests green

| # | Slot | Done when | Deps |
|---|---|---|---|
| 08 | ✅ Registry — role resolution, pricing, capability tags incl. modality | Role swap is config-only; unknown role fails loudly | 02 |
| 09 | ✅ Cost accounting — usage → dollars from registry pricing | Known usage yields known cost; **missing usage raises rather than silently costing zero** | 08 |
| 10 | ✅ Adapter core — OpenAI-compatible call, non-streaming | Request shape and response parsing verified against a fake server | 08 |
| 11 | ✅ Adapter streaming — SSE parse, tokens to bus, usage from final chunk | Streamed and non-streamed produce identical final content; requires `stream_options: {include_usage: true}` or the budget guard is blind | 10, 04 |
| 12 | ✅ Provider shim seam — per-provider request/response transforms | Two fake shims mutate requests differently through config alone | 10 |
| 13 | ✅ Mock provider — deterministic scripted responses | Same input → same output, every run | 12 |
| 14 | ✅ Replay provider — record and play back real transcripts | Record then replay reproduces the stream byte-identically | 13, 11 |
| 15 | ✅ Tool-call protocol — schema emission, parse, dispatch loop | Multi-turn loop terminates; malformed `tool_call` handled without a crash | 13 |

**Decisions taken during Block B:**

- **The mock speaks HTTP.** It is an `httpx.MockTransport` returning real
  OpenAI-dialect JSON and SSE, not an object that short-circuits the adapter. So
  every mock-driven test in the codebase exercises request shaping, SSE framing,
  tool-call reassembly, and usage extraction for real — which is the direct
  mitigation for the "mock-only development" risk below. It is faithful in two
  places that matter: usage appears **only** when `stream_options.include_usage`
  was actually sent, and streamed content arrives in fragments with tool
  arguments cut mid-JSON.
- **Replay matches on a strict request hash.** A changed prompt matches nothing
  and fails loudly. That failure is the feature — ordered playback would return a
  response recorded for a different prompt and the test would pass proving
  nothing. `stream`/`stream_options` are excluded from the digest so one
  recording serves both call styles.
- **An unknown provider raises rather than falling back to the baseline.** A typo
  like `moonshto` would otherwise keep working, because it *is* OpenAI dialect,
  while silently losing the real shim. A vendor needing no special handling uses
  `provider = "openai"` with its own `base_url`, so this costs nothing legitimate.
- **Real vendor shims are deliberately absent.** Slot 12 ships the seam, the
  generic OpenAI baseline, and the mock shim. Writing Moonshot/DashScope/DeepSeek
  shims now means unverified code against APIs Slot 41 must re-check, and a wrong
  shim that looks right is worse than an absent one.
- **Amendments to earlier slots:** `base_url` must now be `http://` or `https://`
  (Slot 02) — caught at load rather than as an obscure transport error at first
  dispatch; the mock uses `http://mock.invalid/v1`, whose TLD can never resolve,
  so a bypassed transport fails instantly instead of reaching something real.
  `execution.max_tool_iterations` was added to `policy.toml` for Slot 15.

**Found while building Slot 08 — visual tasks have a one-rung ladder.** With the
shipped ladder (text `low`, multimodal `high`, text `max`), `escalate(high,
needs_pixels=True)` returns `None`: a pixel-bound task that fails at `high` has
nowhere above it to go, because the top tier cannot see the image. Escalating
into `max` anyway would be a downgrade dressed as a promotion, so the registry
refuses and the task goes to a human.

The real fix is the conductor's pre-digest step (§3.1): once an image is
distilled to structured text the task is text-only and gets the full ladder back.
That makes pre-digestion a *routing prerequisite* for hard visual work rather
than an optimisation — Slot 40 must decide between "pre-digest, then route on
difficulty" and "route to a multimodal tier and accept the shorter ladder", and
the honest default is to pre-digest.

Slot 02 note: Slot 08's "unknown role fails loudly" was already half-met by the
config loader, which rejects unknown role names and missing ones at load. Slot 08
covers the runtime path — `Registry.model_id("turbo")` and friends.

### C — Guards & execution backends (zero-token) — ✅ complete

| # | Slot | Done when | Deps |
|---|---|---|---|
| 16 | ✅ `FailureClass` decision table the ladder executes *(the enum landed in Slot 03)* | A guard trip provably cannot advance the ladder or write a router label | 03 |
| 17 | ✅ Path jail | Escape suite denied: `..\..\` traversal, absolute, symlink-out, UNC, device names, drive-relative `D:foo`, alternate data streams | 16 |
| 18 | ✅ Command allowlist — allow/deny with argument inspection | Denials return structured, zero tokens spent | 16 |
| 19 | ✅ `RunBackend` interface + Windows impl | Runs pytest in the jail, captures exit code, honours timeout | 17, 18 |
| 20 | ✅ WSL backend + path translation | Same suite passes through either backend by config alone; `D:\x` ↔ `/mnt/d/x` both directions | 19 |
| 21 | ✅ Budget guard — per-task and per-day ceilings | A few-cent ceiling produces a hard stop, not an overrun | 09, 16 |

### D — Verifier gate — ✅ complete

| # | Slot | Done when | Deps |
|---|---|---|---|
| 22 | ✅ Verifier interface + registry, static/stateful split | Contract tests pass for both shapes | 16 |
| 23 | ✅ Static verifiers — Python syntax/AST, JSON schema | Sub-second, correct verdicts | 22 |
| 24 | ✅ pytest gate — run via `RunBackend`, parse results | Pass / fail / error distinguished; failure reason is the exact text fed to the retry | 22, 19 |
| 25 | ✅ Stateful poller — async command + mechanical poll, timeout | Zero model calls during the wait; suspension is a policy threshold | 22, 06 |

### E — Context & memory — ✅ complete

| # | Slot | Done when | Deps |
|---|---|---|---|
| 26 | ✅ Context assembler — prefix/tail, `append_failure()`, explicit `rebuild()` | Prefix byte-identical across a retry; every rebuild emits a logged event | 03 |
| 27 | ✅ Memory store interface + SQLite FTS5 lexical store | Write / query round-trip; retrieval is precise on filenames and error strings | 03 |
| 28 | ✅ Pruner — deterministic summary, store-before-drop | Nothing is dropped without a prior store write; pruned detail is retrievable | 26, 27 |

**Two bugs found by building, both of the "green tests, wrong system" kind:**

- **The pytest gate trusted exit code 1.** `python -m pytest` exits 1 both when
  tests fail *and* when pytest is not installed. The gate classed the second as a
  verifier failure — so a missing dependency would escalate the task to a pricier
  tier and write "this tier failed" into the router's training set. Fixed by
  requiring positive evidence the run happened (a summary banner or a test count)
  before trusting exit 1. This is the exact failure the module's own docstring
  warned about, which is a fair illustration of why the guard has to be
  mechanical rather than remembered.
- **On Windows, `env` does not select the executable.** `CreateProcess` searches
  the *calling* process's PATH, not the environment block it is handed — so a
  worker setting `PATH` to pick a project toolchain would silently run the
  daemon's. `WindowsBackend` now resolves the program itself against the
  effective PATH before exec. The command guard already requires a bare name, so
  this only ever expands an approved program.

**Decisions taken during C–E:**

- **Slot 16 is a decision table, not an enum.** The taxonomy moved to Slot 03
  earlier; what was missing was the deterministic mapping from
  (failure class, attempts, policy) to an action. `core/failures.py` holds it, and
  Slot 31 executes it rather than re-deriving it. The subtle rule made explicit
  there: guard and transport failures **count toward the attempt cap but never
  toward the escalation counter** — without the first half a worker looping on
  jail escapes never terminates; without the second it climbs the ladder while
  doing so.
- **Memory retrieval is lexical (SQLite FTS5), and phrase-first.** The obvious
  OR-of-tokens implementation is wrong: `uploader.py` tokenises to `uploader` +
  `py`, so a search for one file returns every Python file, and each irrelevant
  hit is noise injected into the context retrieval exists to keep small. Phrase
  match is tried first, any-token only as a fallback.
- **The pruner's summary is built from state, never model-written.** Zero tokens,
  deterministic, and it cannot invent — which matters because the conductor then
  plans from it.
- **Bulk document ingestion is explicitly out of scope.** Memory here is the
  pruned-context blackboard. A reference corpus (PDFs, textbooks) would be a
  separate store with chunking and real embeddings, and would get its own slot.

### F — Execution plane — ✅ complete

| # | Slot | Done when | Deps |
|---|---|---|---|
| 29 | ✅ Worker dispatch — spec → messages → streamed call | A mock task runs end to end | 26, 15 |
| 30 | ✅ Worker tool surface — fs + run-command, every call guard-wrapped | A guard denial returns a tool-error, not an exception | 29, 17, 18 |
| 31 | ✅ Escalation ladder — retry with reason → escalate → cap → human | Fail-twice-at-`low` produces exactly the expected trail | 30, 24 |
| 32 | ✅ Logbook — one row per attempt | Features, tier, verdict, cost, latency, `schema_version` recorded; guard trips excluded from labels | 31, 16 |

### G — Conductor — ✅ complete

| # | Slot | Done when | Deps |
|---|---|---|---|
| 33 | ✅ Directive — immutable block, hash-checked per checkpoint | Mutation is detected and refused | 26 |
| 34 | ✅ Task-spec emission — structured, schema-validated | An invalid spec is rejected and repaired via retry, never passed downstream | 03, 33 |
| 35 | ✅ Checkpoint loop — event-driven conductor | No conductor call fires outside a defined checkpoint | 04, 34 |
| 36 | ✅ Reasoning-effort policy — low default, defined triggers | Effort chosen per the policy table and logged with each call | 35 |
| 37 | ✅ **Test-authorship protocol** — tests before implementer dispatch | Implementer's attempt to edit the acceptance-test file is denied by the path guard | 34, 30 |
| 38 | ✅ Plan-vs-directive rationale — structured delta per re-plan | Delta logged for audit on every re-plan | 33, 35 |

### H — Router — ✅ complete

| # | Slot | Done when | Deps |
|---|---|---|---|
| 39 | ✅ Feature extraction — shared by rules and classifier | Deterministic feature vector from a `TaskSpec` | 03 |
| 40 | ✅ Rule router — difficulty + modality from capability tags | A text-only `max` tier never receives a pixel-bound task | 39, 08 |

**Decisions taken during F–H:**

- **Escalation does not call the conductor.** The same spec is re-dispatched one
  tier up with the failure reason appended, and an event is published. Conductor
  thinking dominates the bill, so firing it on the path that is already going
  badly is the most expensive available reflex. `conductor.replan_on_escalation`
  exists and is off.
- **Test authorship is a separate worker call** (`conductor.test_authorship =
  "separate"`), at the cheap tier — turning criteria into a test file is
  transcription, not judgement, and a ~500-token test file costs about $0.0012
  there versus roughly $0.0075 of conductor output. The file is then frozen
  **on `PathJail`**, not inside `write_file`: a check in one tool is re-opened by
  the next tool that forgets it, and "every tool remembered" is not testable.
- **Four checkpoints, enumerated as data.** `WAKES_CONDUCTOR` lists every
  orchestrator event and whether it may wake the conductor; anything else raises
  `NotACheckpoint`. Waking on every event is the single biggest way to inflate
  the bill, so an event that quietly became a conductor call has to fail loudly
  rather than be caught in review.
- **The rule router's base score sits inside the `high` band, not on its edge.**
  Spec §2 calls `high` the strong default, so unremarkable work belongs there and
  it takes positive evidence of simplicity to drop to `low`. Starting at the
  boundary meant a single mild negative signal demoted ordinary tasks to the cheap
  tier — how a router quietly becomes a false economy.
- **Difficulty is one-hot in the feature vector**, not ordinal. Encoding it 0/1/2
  would tell a linear model that "hard" is twice "medium", which is not a claim
  anyone is making. `FEATURE_NAMES` is append-only: reordering it invalidates
  every stored row.
- **The stated rationale never grants approval.** A model comparing its own plan
  against the directive is grading itself. `check_plan` is deterministic and can
  refuse; the model's prose is recorded verbatim for audit and carries no weight.

**Two blemishes found by running the full pipeline rather than the unit tests:**

- Cost quantisation left an exact zero as `0E-10` — numerically correct, reads as
  a bug wherever displayed, and zero is the *common* case until Slot 41.
- The test-authorship call emitted `AttemptStarted`, so the event stream reported
  four attempts where the logbook had three rows. Only ladder attempts announce
  themselves now, and a test asserts the two counts match.

### I onward — outlined

**41–42 · Live integration (the buy point).** Re-verify prices and model ids, populate the registry, buy one cheap key, tune prompts against reality, and record those transcripts into the replay corpus. Everything before this runs on mocks.

> **Slot 41 research is complete — see [PRICING.md](PRICING.md).** Prices,
> Artificial Analysis intelligence/speed figures, benchmarks, endpoints and a
> ready-to-paste registry block are all recorded there. **The model-allocation
> decision is deliberately left open**; three questions are listed at the top of
> that file. Nothing has been bought.
>
> Headline findings: the spec's prices were close to right (Kimi K3 and Qwen-Max
> exact); Kimi K3's real edge is *agentic* work rather than raw coding (DeepSeek
> V4-Pro beats it on SWE-bench Verified); DeepSeek V4-Pro hallucinates at 94% on
> AA-Omniscience, which the verifier gate makes tolerable at an execution tier and
> disqualifying at the conductor; and the spec's original Qwen-Max choice for
> `high` looks right, because it is the only affordable multimodal tier and
> without one the visual ladder has a single rung.

**43–46 · Service & UI.** FastAPI local service with WS event stream → overlay rendering tokens and gate events → frameless always-on-top webview → tray and global hotkey.

**47–51 · Perception.** Screen capture → UIA reader → coverage-threshold detector → OCR and box-proposal fallback → observation adapter unifying both backends behind one schema.

**52–57 · Learned router & hardening.** Training pipeline from the logbook → exploration seam → candidate-vs-incumbent eval harness (which is also §3.1's safe model-upgrade path) → multi-key failover → dashboard → multi-day soak.

---

## WSL

Perception is inherently Windows-side — WSL cannot read the Windows UI Automation tree — so the orchestrator, daemon, and service run on Windows. Only *execution* varies, through `RunBackend`:

- Commands dispatch as `wsl.exe -d <distro> --cd <path> -- bash -lc "<cmd>"`, exit codes propagating normally.
- Path translation maps `D:\orchestrator\workspace` ↔ `/mnt/d/orchestrator/workspace`; the jail is expressed in the backend's own path space so the guard stays deterministic on both sides.
- Keep the workspace on whichever side runs the tests. Crossing the 9p boundary (`/mnt/d` from Linux, or `\\wsl.localhost` from Windows) is slow enough to matter in a retry loop.
- Stateful polling generally works across the boundary — WSL2 forwards localhost — but a service bound to `127.0.0.1` inside the distro is sometimes unreachable from Windows, so the poller runs inside the backend rather than assuming host reachability.
- One thing lands free: WSLg GUI apps expose almost nothing to UI Automation, which is exactly the §8.2 coverage-threshold fallback doing its job. The OCR path already covers that case.

---

## Risks and how they're handled

| Risk | Handling |
|---|---|
| **Mock-only development.** The control plane can be provably correct against a fake and still fragile against real models — this is the real cost of deferring keys. | Slot 42 is explicitly budgeted for surprises rather than treated as a formality; from the first key onward the replay provider (14) turns every real transcript into a permanent regression fixture. |
| **Windows shell work** — frameless always-on-top, hotkey registration, COM threading. | UI is only a WS subscriber, so a plain browser tab is a fully working fallback if the overlay fights us. Hotkey via `RegisterHotKey` with a tray-menu fallback. UIA gets its own thread with `CoInitialize`, isolated in a subprocess so a COM hang cannot take the orchestrator down. |
| **Vision fallback is the heaviest component.** | Ship ONNX OCR plus contour box proposals first; upgrade the detector later behind the observation schema, which the conductor never sees through. |
| **Unverified prices and model ids.** The spec says so itself and none have been checked. | Slot 41 sits immediately before the buy point. Pricing is registry data, so re-pricing is a config edit. |
| **Router cold start and biased labels.** No data on day one, and the classifier only sees verdicts for tiers that actually ran. | The rule router (40) is a permanent fallback, not a placeholder; the classifier is promoted only when it beats it on the saved suite. Escalation chains supply both-tier labels, and the exploration seam covers the rest. |
| **Conductor is a single point of failure** — it is the only thing that talks to you. | Registry already supports a fallback conductor role; multi-key failover is slotted at 55. |

---

## Verification

Per slot: `pytest tests/`, green before the session ends. The structural rules get named tests, because these fail silently rather than loudly:

- **Role swap** — change `high` from `mock-a` to `mock-b` in `registry.toml`, rerun, assert identical behaviour with zero code edits.
- **Cache integrity** — assert the serialized prefix is byte-identical before and after a retry. This is what catches someone "helpfully" re-inlining a failure into the system prompt, silently costing both the cache discount and the ~3s prefill.
- **Escalation** — mock fails twice at `low`; assert retry-at-same-tier then escalate, exactly two attempt rows, correct label on each.
- **Guard isolation** — worker writes outside the jail; assert tool-error returned, tier unchanged, no escalation, no router label written.
- **Suspension** — poll a port that opens only after 3s; assert zero model calls during the wait, then kill mid-poll and assert resume from SQLite.
- **Budget** — a few-cent per-task ceiling produces a hard stop with a conductor notification.
- **Test-authorship** — implementer's edit of the acceptance-test file is denied by the path guard.
- **Failsafe** — delete the SQLite file and assert a task rehydrates from `OPERATOR.md` alone.

End-to-end from Slot 38, on mocks: submit a coding task, watch the WS event stream, assert `Verdict.PASS` and a complete attempt trail in SQLite. Re-run the transcript to confirm replay determinism.
