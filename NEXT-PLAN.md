# Agentic Operator — Plan for what's left

*Companion to [BUILD-PLAN.md](BUILD-PLAN.md), which covers Slots 01–40 (complete).
This covers 41–73, re-scoped around the Jarvis goal.*

## Context

*Status as of 22 Aug 2026: Blocks A–J2 complete plus the **J2.5 audit and
instrument repair**, **829 tests**, live on DeepSeek + Claude Code, ~$1.20 real
spend.*

**Direction, settled 22 Aug — a bounded Jarvis on Claude Code.** Not free-roaming,
not a Claude Code plugin, no MCP/skills/agents. The orchestrator stays the outer
process; Claude Code is its preferred execution plane and DeepSeek catches it
when the subscription saturates.

**The split that makes it affordable — and it is now the shipped default:**

| role | runs on | why |
|---|---|---|
| conductor | **DeepSeek** | it plans and it talks, and it is ~60% of a task's bill (3,605 output tokens planning vs 338 doing). Cheap, structured JSON |
| low / high / max | **Claude Code** → DeepSeek | it is excellent at coding and expensive as a conversationalist, so it gets the coding and nothing else |

*"Claude as Jarvis will just burn unnecessary tokens — it works great in coding
but using it conversationally is a mistake."* That is the reason the conductor
must never move onto the agent harness, and the reason a future conversational
surface goes on the cheapest capable model, **not** on Claude Code.

**Why not a plugin.** Claude Code plugins package hooks + MCP + skills and load
by local path, so packaging is solved — but the control direction inverts.
Claude Code becomes the caller, the conductor and ladder become redundant, and
**the DeepSeek/Qwen failover dies**, because that only exists while our process
is on the outside. An MCP tool is also model-discretionary: a gate the model
chooses whether to invoke is not a gate, which is the `acceptance: []` lesson one
level up. Only `hooks/` fire deterministically (*"if any hook returns `deny`, the
operation is blocked regardless of other hooks"*), so a plugin, if ever built, is
hooks — never MCP.

**Order:** ~~48e~~ ✅ → ~~49 cross-plane failover~~ ✅ → ~~51 shell discovery~~ ✅ →
**Block L, system hygiene, built to done**. Blocks K (voice), M (proactivity)
and N (perception) stay written down and unbuilt until L is real.

That is a deliberate cut, and the reason is in the audit: the post-Jarvis roadmap
is **four separate products sharing a daemon**, 22 slots at one slot per session,
and only Block L reuses the machinery already built and paid for — the verifier
gate, the `RunBackend`, and the command allowlist. It is also the only one that
makes the daemon useful without a microphone or a screen reader. Starting four
blocks is how a finished core stays unshipped.

Slots 01–40 were complete at: **549 tests, zero spend, whole pipeline on mocks** (directive → checkpoint → spec → router → test authorship → ladder →
verdict → logbook → journal).

Late in the build the actual goal surfaced: **this is meant to be a Jarvis**, not
a batch execution engine. That reframes what remains.

What's built is the *trustworthy execution core* — guards, verifier gate, durable
state, the journal. For a Jarvis those matter **more**, not less: something that
acts while you aren't watching needs deterministic limits and a gate that isn't
the model marking its own homework.

What's missing is everything that makes it *present*: no face, no senses, no
voice, no ability to notice. Two things a Jarvis needs weren't in the spec at all
— proactivity and a runtime I/O mode — and a third, system hygiene, turns out to
be a better fit for the existing engine than anything else on the list.

**Decisions taken:**

| | |
|---|---|
| **First form** | Dev work on this machine, plus **system hygiene** — cleaning up, clearing unwanted scheduled tasks, reclaiming disk. *Not* GUI automation |
| **Voice** | Voice in / voice out, **mode switchable at runtime** — text↔text when quiet, text→voice on headphones, "show and explain" for detail |
| **Proactivity** | Global on/off switch. When on: **notify only**. It acts only when told |
| **Buy point** | Moves earlier — a Jarvis answering "mock reply a3f9" can't be evaluated as a Jarvis |
| **Learned router** | Deprioritised. The conductor is ~90% of the bill, so tier optimisation targets the wrong 10% |

---

## Why system hygiene changes the shape

It is a far better fit than the GUI automation originally assumed:

- **It's command-driven, not pixel-driven** — needs no vision, runs entirely
  through the `RunBackend` and command allowlist that already exist.
- **It's deterministically verifiable.** Disk space freed, process gone, task
  removed, service disabled. Real verdicts, not an LLM judging its own work — which
  is precisely what the gate was built for.
- **It's naturally notify-then-act**, exactly the proactivity model chosen: "you
  have 40GB of npm caches older than six months" → you say go.

It also makes proactivity useful *before* perception exists, because the things
worth noticing (disk filling, cruft accumulating) come from cheap system polling
rather than from watching the screen.

**The one thing it breaks.** `PathJail` (Slot 17) confines everything to a single
workspace root — the whole point of it. System cleaning must reach outside that by
definition, so it cannot reuse the same guard. It needs a **second, differently
shaped guard**, and getting this wrong deletes something unrecoverable:

- **An allowlist of cleanable locations**, deny-by-default, rather than one root.
  Temp dirs, package caches, known-safe paths — each named explicitly.
- **A denylist that always wins**, covering system directories, user documents,
  anything under version control, and the jail root itself.
- **Quarantine, never unlink.** Move to a holding area with a retention period,
  then delete on expiry. The same principle the design already applies to context —
  *prune to memory, not to trash* — extended to the filesystem.
- **Dry-run first, always.** Propose with sizes and ages; act only on confirmation.
  Which is what "notify only, act when told" already asks for.

---

## Open decision — Claude Code as the execution plane

*Raised 13 August 2026. **ANSWERED 22 August 2026 — see Block J2 below for the
measurement.** The reasoning is kept because several of its premises turned out
to be wrong, and which ones is worth knowing.*

**The question.** Claude Code already has a well-tuned agentic coding loop and can
swap models itself. If the coding work is what we care about first, why run our
own `Worker` + `ToolBox` + escalation ladder at all — why not delegate
implementation to Claude Code (via the **Claude Agent SDK**, not by shelling out
to the CLI) and keep the orchestrator for everything around it?

**The measurement that prompted it.** From the partial baseline — 18 calls, $0.081:

| purpose | calls | cost | share |
|---|---|---|---|
| plan | 8 | $0.0383 | **47%** |
| authorship | 5 | $0.0209 | 26% |
| attempt | 5 | $0.0219 | **27%** |

**73% of spend happens before any implementation.** The coding is the cheap
quarter — so this is a quality argument first and a cost argument second.

**What would survive:** conductor and spec emission, the verifier gate (we run
pytest ourselves; that does not move), the path jail and budget guard around the
workspace, journal, scheduler, presence, and everything in the blocks below.

**What it would replace:** `execution/worker.py`, `execution/tools.py`,
`execution/ladder.py`. The orchestrator's value was never the tool loop — ours
took four attempts on a trivial backoff function.

**Three things that genuinely break, in order of concern:**

1. **The guards get delegated.** The path jail and the frozen-test-file rule are
   currently ours, enforced in-process. Claude Code has its own permission model,
   so the test-authorship freeze — the thing that stops the gate being theater —
   would have to be expressed in its deny rules. That is the piece to scrutinise,
   because it is the one that has already failed once in this project.
2. **Cost visibility changes shape.** The spend ledger reads token usage off the
   API response. Under a subscription there are no per-call tokens to read, so
   the execution half of the budget guard goes dark. Arguably moot at flat rate,
   but the ledger stops being complete.
3. **The tiered router dies entirely** — one backend, not three tiers. Worth
   noting this is *consistent* with the earlier finding that tiering economics
   were weak; execution being 27% of spend makes them weaker still. It removes a
   component rather than breaking one.

**The economics, which may dominate.** With an existing Claude Code subscription,
execution goes to zero marginal cost and the bill becomes just the conductor —
roughly $0.081 → $0.038 for the same work, with better implementation quality.
The conductor cannot move: it needs cheap, frequent, structured-JSON calls, which
is the wrong shape for an agent loop. So the sensible split is **conductor stays
an API model, implementation becomes Claude Code.**

**How to settle it — do not argue, measure.** The eval harness was built for
exactly this. Add Claude Code as an execution candidate, run the same 11-task
suite, compare pass-rate against cost graded by the same gate. `Comparison`
already refuses to promote something cheaper-but-worse. Check the Agent SDK's
permission API first, since that is where the jail question is answered.

**Prerequisite (met):** a clean DeepSeek baseline — `evals/runs/deepseek.json`,
22 August, 9/11 at 11/11 coverage. It took five attempts; the four that failed
each exposed a defect in the harness, and those fixes are Block J2's real legacy.

---

## Order

The old plan ran service → perception → learned router. Against the Jarvis goal
that's wrong at both ends: presence should come first because you can't evaluate
something you can't summon, and the learned router should come last because its
economics don't hold.

**Presence → real models → voice → hygiene → noticing → senses → hardening.**

---

## Block I — Presence (Slots 41–46)

The shell. Runs on mocks at zero cost, and turns an abstract backend into
something that exists on your desktop.

| # | Slot | Done when |
|---|---|---|
| 41 | ✅ Local service — FastAPI, WS event stream, submit endpoint | A browser tab shows live events from a mock run |
| 42 | ✅ Overlay UI — tokens, verdicts, task state, spend | A full ladder climb is watchable as it happens |
| 43 | ✅ Frameless always-on-top window (pywebview) | Floats over other apps without stealing focus |
| 44 | ✅ Tray icon + global hotkey (`RegisterHotKey`) | Summoned and dismissed from any application |
| 45 | ✅ Directive intake | A typed goal runs the whole pipeline and reports back |
| 46 | ✅ **Scheduler** — the missing loop that consumes pending and due work | A task interrupted by a restart actually resumes; a suspended task wakes |
| 46b | ✅ Autostart on login | Survives a reboot without being launched by hand |

**Block I is complete.** Everything buildable before the buy point is done.

**Reuses:** `core/events.py` already carries everything the UI needs — the overlay
is a plain subscriber. `TaskLifecycle.recover_orphans()` already exists for 46.

**Risk:** frameless always-on-top and global hotkeys are the fiddliest Windows work
here. Mitigation is already structural — the UI is only a WebSocket subscriber, so
**a plain browser tab is a working fallback**, and the hotkey degrades to a tray menu.

### Slots 41–42, built — what running it turned up

`src/aop/operator.py` is the composition root (the one place that knows the order
things happen in), `src/aop/service/` is the HTTP+WS surface, and
`src/aop/__main__.py` gives `serve` / `run` / `status`. **570 tests green.**

Three things only the live run exposed, all now fixed and tested:

- **`provider = "mock"` was not honoured outside the tests.** Every service test
  injected a transport, so all 17 passed while the daemon itself tried to resolve
  `mock.invalid` over the network and died on its first call. The registry now
  mounts the mock transport by host, which also means a registry mixing the mock
  with a real provider keeps working during the swap.
- **`websockets` is a hard dependency, not an extra.** Uvicorn disables WebSocket
  support entirely when no WS library is present *at startup* and answers `/ws`
  with a 404 — so the overlay connects, fails silently, and shows a dead status
  dot forever.
- **The journal was writable by workers.** It lives in the jail so it is readable,
  but recovery parses it, so a worker able to edit it could mark itself complete
  instead of doing the work. Now frozen via the Slot 37 mechanism — readable
  context, not a rewritable record. The state database was already outside the
  jail for the same reason.

And one invariant confirmed under real conditions rather than in a unit test: with
no pytest in the workspace, the gate returned `error [transport]` rather than
`fail [verifier]`, so the task **stayed at one tier for all four attempts** and
wrote **zero** training labels before handing to a human. A broken tool is not a
weak model, and the system knows the difference where it counts.

### Audit finding — there is no scheduler (Slot 46)

A full audit after 41–42 attacked every stated invariant. **28 of 28 held** —
17 jail escapes contained, the command allowlist unbypassable, frozen files
resistant to every spelling, budget stopping exactly at the ceiling, money exact,
the directive refusing six restatements, the journal surviving total database
loss, no non-verifier failure escalating or training, the prefix byte-stable
across ten retries, every hostile tool call denied as a message, the bus
unstallable, subscriptions not leaking, the pruner storing before dropping, and
replay failing loudly on a one-character prompt change.

The gap it found is structural rather than a defect: **nothing consumes the work
queues.**

| Built and tested | Consumed by |
|---|---|
| `lifecycle.recover_orphans()` | `Operator.start()` — reclaims RUNNING → PENDING |
| `lifecycle.due_for_resume()` | **nothing** |
| `lifecycle.resume()` | **nothing** |
| `SuspensionPlan` / `plan_for` (Slot 25) | **nothing** — the ladder awaits the gate inline |

Only `submit()` ever starts work, and only on the task it has just created.
Measured directly:

```
interrupted task:  running -> pending -> pending (forever)
suspended task:    due_for_resume() reports it; nothing calls it
pending queue:     1 task pending, 0 runners active
```

Two consequences. A task interrupted by a restart is **reclaimed but stranded** —
Slot 46's original "on restart the task resumes" was never met, only half of it.
And a task suspended on a slow check **never wakes**, so the whole Slot 25
"never let a model wait" mechanism parks tasks that nothing un-parks. The second
does not bite today because the default pipeline uses the static pytest gate and
the ladder never suspends, but it is a loaded gun and the static/stateful
verifier split currently has no runtime effect at all.

This is the characteristic failure of a slot-by-slot build: every component green
in isolation, the connective tissue absent. Slot 46 is now the scheduler itself —
a loop that picks up PENDING work, wakes tasks whose timer has expired, and lets
the ladder suspend rather than await when `plan_for` says to.

**Also fixed during the audit:** `LadderStep.cost_usd` was hardcoded to zero, so
the per-step trail reported nothing spent while the total was right. Nothing
load-bearing read it, which is why it survived — it would have surfaced as a
dashboard full of zeros much later.

### Slot 46, built — the scheduler

`src/aop/core/scheduler.py`. **592 tests green.** The gap measured before and after:

| | before | after |
|---|---|---|
| interrupted task | `running → pending → pending` forever | `running → pending → done` |
| suspended task | never woke | wakes on its timer |
| pending queue | 1 stranded, 0 runners | nothing stranded |

`tick()` is public and returns what it did, so every behaviour is asserted
without a single `sleep` — a scheduler tested by waiting is a suite that fails
once a week on a slow machine. Claiming is an in-memory set; the PENDING →
RUNNING transition is the durable half, so a crash between the two leaves the
task PENDING for the next start to pick up. Resumes take priority over new work,
because a task already underway has consumed budget and context.

**Two bugs the scheduler exposed, both fixed:**

- **`Operator.run()` raced the loop.** It was public and bypassed the claim, so
  the scheduler and any direct caller could run the same task at once — and the
  CLI's `aop run` did exactly that. There is now one way in: `submit()` queues,
  `run_directive()` submits and waits, and `start(run_scheduler=False)` exists
  for callers that genuinely want to drive the pipeline themselves.
- **Resuming a task killed it.** The scheduler wakes a task to RUNNING, then the
  pipeline called `lifecycle.start()` again — an illegal transition — and
  `_guarded_run` dutifully failed the very task it had been asked to continue.
  Only visible by running it; every unit test passed.

**Known limitation, deliberately left.** There is no continuation record, so a
resumed task re-runs the pipeline **from the top** rather than from where it
stopped. That is correct but not free, and it is precisely why mid-ladder
suspension is still not wired in: suspending inside a climb would lose the climb.
Completing Slot 25 properly means persisting ladder position, which is its own
slot.

### Slots 43, 44, 45, 46b, built — the desktop shell

`src/aop/daemon/` — window, tray, hotkey, autostart. **635 tests green.**
Verified running:

```
Operator
  overlay    http://127.0.0.1:8767
  window     frameless, always on top
  tray       yes
  hotkey     ctrl+shift+space
```

`aop app` runs service and shell together; `aop serve` stays headless for the
browser-tab route; `aop autostart on|off|status` manages login startup.

**The shell is a client and never imports the Operator** — a test asserts this by
reading the source. That is what lets the window crash, the tray die, or the
hotkey be refused without touching a task mid-flight, and it is why the riskiest
Windows work could safely be left until last. Everything degrades in a stated
order: frameless window → browser tab → the URL in the console, and each failure
is *reported* rather than swallowed.

**The bug worth remembering.** `RegisterHotKey` returns `None` on success and
**raises** on failure — it does not return a boolean. The obvious
`if not win32gui.RegisterHotKey(...)` treats every successful registration as a
refusal, so the shell would have fallen back to the tray *forever* while
truthfully reporting "hotkey unavailable". Invisible precisely because the
fallback works. Found by running it; no unit test had reached that line. The
registration is now a separate function so the pywin32 calling convention is
asserted directly.

Three smaller decisions worth keeping: the window is created with `focus=False`,
because an assistant that steals the caret from your editor to announce itself is
worse than one you have to click; `easy_drag` follows `frameless`, since a window
with no title bar and no drag handle cannot be moved at all; and the tray shows
failed features *disabled with the reason* rather than hiding them, because a
menu that silently omits the hotkey looks identical to one where it works.

---

## Block J — Live integration, the buy point (Slots 47–48)

| # | Slot | Done when |
|---|---|---|
| 47 | Populate registry, buy one key, all four roles on it | A real model completes a real task end to end |
| 48 | Prompt tuning + cassette recording | Real transcripts become permanent replay fixtures |

Research is **already done** — [PRICING.md](PRICING.md) has verified prices,
Artificial Analysis figures, endpoints, and a ready-to-paste registry block. Three
allocation questions remain open there. ~$5–10 of credit is enough. Set
`budget.per_task_usd` and `per_day_usd` **before** the first call.

---

## Block J2 — Execution plane trial (Slots 48a–48d)

*Settles the open decision at the top of this file. Added 14 August 2026.*

Reading the Agent SDK docs before building changed three of the decision's stated
premises, and a fourth came from the user: **failover to Kimi/GLM/Qwen when the
subscription is exhausted.**

**The framing is now "prefer and fall back", not "replace".** Falling from
`claude_code[low]` to Kimi is not a model swap, it is a *plane* swap — Claude Code
is an agent harness, the others are OpenAI-dialect APIs reached through our own
worker and tool loop. So `execution/worker.py` and `execution/tools.py` are never
deleted. The list under "What it would replace" above is wrong: **nothing is
replaced.** Claude Code becomes the preferred plane and ours becomes the one that
remains when the subscription runs out.

**The three corrections to the open decision:**

| Claim above | What the docs say |
|---|---|
| "The tiered router dies entirely — one backend, not three tiers" | Wrong. `ClaudeAgentOptions.model` exists, so `low`/`high`/`max` map to three Claude models. The subscription is in fact the *only* way to afford a full ladder — flat rate reverses the old "tiering economics are weak" finding |
| "The guards get delegated… the piece to scrutinise" | Overstated. `PathJail.resolve_for_write()` already raises `GuardDenied` for both jail escapes and frozen files. A `PreToolUse` hook wraps that one call — the guard is *reused*, not re-expressed. Hooks run before every other permission step and a hook `deny` wins even under `bypassPermissions` |
| "execution goes to zero marginal cost" | True for personal use only. The SDK overview forbids third-party products offering claude.ai login without prior approval. Since CLAUDE.md line 5 says *standalone product*, treat this as a **quality** experiment; if it ever ships, execution is API-billed and the cost argument inverts |

**Quota exhaustion is `FailureClass.TRANSPORT`, never `VERIFIER`.** Same invariant
as "a failed exit code is not automatically a verdict". Get it wrong and a Monday
limit reset reads as "the cheap tier failed four tasks in a row" — escalating for
no reason and writing bogus tier labels into the router's training set. Two
independent axes that must not be confused: `VERIFIER` moves **up** the ladder,
`TRANSPORT` moves **sideways** to the next vendor.

**Failover is a production behaviour, not an eval behaviour.** A suite run that
fails over halfway reports `label="claude_code"` while half the tasks ran on Kimi,
and `Comparison` silently measures a blend. During `aop eval` it stays off and
quota exhaustion fails loud.

| # | Slot | Done when |
|---|---|---|
| 48a | ✅ `ExecutionPlane` protocol | The ladder drives a plane that is not the `Worker` — no adapter, no `ChatResponse` |
| 48b | ✅ `ClaudeCodePlane` — SDK call, `PreToolUse` jail hook, served identity, quota→`TRANSPORT` | A frozen acceptance file is unwritable *by Claude Code*, proven by the existing escape suite run through the hook |
| 48c | ✅ Failover chain — role becomes a list, `TRANSPORT` moves sideways, off during eval | Pulling the Claude plug degrades to Kimi/GLM/Qwen instead of failing. This is **Slot 69 cashed early** |
| 48d | ✅ `compare()` on the 11-task suite | run 22 Aug 2026 — see below |

### Slot 48d, run — the answer

**On the 10 tasks both planes graded: DeepSeek 8/10, Claude Code 9/10.** Claude
Code won one and lost none, and finished the suite faster (99.7 min vs 110.8).

| | DeepSeek | Claude Code |
|---|---|---|
| correct (10 shared) | 8/10 | **9/10** |
| real money | $0.4789 | **$0.2650** |
| list-equivalent | $0.4789 | $10.81 |
| wall clock | 103.6 min | 99.5 min |

> **Corrected 22 Aug by the audit (Block J2.5).** These are the ten *shared*
> tasks, printed by `aop compare`, not hand-computed. The earlier `$0.3096 /
> $12.67` figures came from a manual ledger query that summed the whole database
> including abandoned re-runs. See F-01.

**The decisive task is `topk-shortfall`:** DeepSeek burned all four attempts,
exhausted the ladder and was handed to a human; Claude Code solved it in two.

**Formal verdict is `NO VERDICT`, and that is correct.** The candidate run lost
`unknown-intent` to a `TransportError` on the *conductor's* DeepSeek call, so it
graded 10 against the incumbent's 11. The guard refused to read those pass-rates
against each other, which is exactly what it was built for. `on_common_tasks()`
is the explicitly narrowed answer above.

**Cost is the opposite of what the first report claimed.** It said $10.81 vs
$0.52 — a 20x penalty — because `RunReport.cost` summed a flat-rate plane's
list-equivalent price as if it were money. Only the DeepSeek conductor bills on
this configuration, so **Claude Code is the cheaper of the two**, on a Pro
subscription with no API key.

> ⚠️ **This paragraph claimed the fix and the fix was not there.** `cost` kept
> reporting list price on every run until Slot 48f, because
> `TaskResult.billable_cost_usd` was declared and consumed but **never set** by
> `Harness._run_one`. The `$0.31` was typed in from a manual ledger query, not
> produced by the instrument. Corrected figures are in the table above; the
> mechanism is F-01 in Block J2.5.

**The finding that outranks the comparison: `underspecified` failed on BOTH
planes.** *"Make the retriever better."* was expected to be handed back; both
completed it and the gate certified both. The executor was never the problem —
no implementation plane can fix a conductor that accepts vague directives and a
gate that then certifies them. **That is the next slot, and it is worth more
than the plane swap.**

**Caveat on scope.** The subscription is **Pro**, so all three rungs are Sonnet.
This measured *Claude Code vs our tool loop*, not *their ladder vs ours*.

### The baseline, at last — and what five attempts taught

`evals/runs/deepseek.json`, 22 Aug 2026: **9/11 correct, 11/11 coverage,
$0.5179, 111 minutes.** Four earlier attempts were lost — two to a user-initiated
reboot, one to campus wifi, one abandoned. Each loss exposed a real defect, and
all four are now fixed:

| What broke | Fix |
|---|---|
| The harness scored a dead socket as a model failure — reported 55% when the true graded figure was 86% | `tasks.failure_class` (migration 3); `pass_rate` divides by **graded**; `Comparison` refuses a verdict when two runs graded different sets |
| One blip during *planning* killed a whole task; the ladder retried `TRANSPORT`, the conductor never did | Bounded retry in `Adapter` — covers conductor, author and ladder alike. Only `httpx.HTTPError`; a 4xx is never retried |
| A 65-minute run threw away everything on interruption | `<out>.partial` written atomically after every task; resumes on re-run; wire-killed tasks re-run rather than restore |
| `test_author_role = "low"` would break the instant `low` became `claude_code` | Moved to `conductor` — the one role guaranteed to stay on HTTP, and a stronger author/implementer split |

**Two findings from the baseline itself.** `topk-shortfall` is a genuine
capability failure (four attempts, ladder exhausted). `underspecified` — *"Make
the retriever better."* — was expected to be handed back and **completed
instead, twice running**. The conductor accepts vague work and the gate certifies
it. That is the `acceptance: []` failure in a new costume and deserves its own
slot.

**Cost calibration.** Estimates went $0.06 → $0.12 → $0.29 → $0.45 against an
actual **$0.5179**; every one was low. Budget **~$0.05/task**. The refusal tasks
are the expensive ones — `impossible-offline` $0.115 and `underspecified` $0.079
are a third of the bill.

**Prerequisite unchanged:** a clean DeepSeek baseline. The 13 August attempt was
interrupted and is unusable.

### Slot 48a, built — the seam

`src/aop/execution/plane.py`. **679 tests green**, nothing behaves differently.

The ladder consumed exactly four facts from a dispatch and reached through
`outcome.response.latency_ms` to get one of them. Naming that contract
(`PlaneOutcome`, `ExecutionPlane`) is the whole slot: after it, a plane with no
`ChatResponse` at all satisfies the same interface, which is what makes 48b a new
module rather than a rewrite.

Three decisions worth keeping:

- **`served_model_id` is not the registry's answer.** The logbook recorded
  `registry.model_id(role)` — the *configured* occupant. Identical today,
  divergent the moment 48c lands, at which point every attempt would be labelled
  with a model that never ran and `training_rows()` would hand those to the
  router. Fixed here rather than in 48c, because the contract is where it belongs.
- **Authorship keeps the internal worker whichever plane is selected.** The test
  author and the implementer must not be the same actor; putting them on
  different planes entirely is a stronger separation than the frozen file alone.
- **An unbuilt plane raises rather than falling back.** A run that quietly used
  the internal plane while the report said `claude_code` would not be an execution
  bug — it would be a wrong answer to the question the eval exists to settle.

### Slot 48b, built — `ClaudeCodePlane`

`src/aop/execution/claude_code.py`. **725 tests green**, none of which need the
SDK, a subscription, or a `claude` binary — `query` is injected at the one seam
the plane uses.

**The plan tier was never actually a blocker.** It decides which model ids go in
`registry.toml`, and those must not be written here anyway. The plane reads
`registry.model_id(role)` and passes it to `ClaudeAgentOptions.model`, so it is
tier-agnostic: answering the tier question is now a config edit against a built,
tested plane.

Five things worth keeping:

- **The jail is reused, not re-expressed.** The hook calls
  `PathJail.resolve_for_write` — one method, already escape-tested. The Slot 17
  escape suite is re-run *through the hook* (traversal, UNC, device names, ADS,
  absolute paths), plus the frozen-file case. Restating the rule as SDK deny
  globs would be a second implementation of a rule that has already failed once
  here, and the two would drift.
- **Quota exhaustion raises `AdapterError`**, which the ladder already classes
  `TRANSPORT`. So 48b and 48c compose with no extra wiring: out of credit
  retries here, never climbs, never trains, and fails over sideways.
- **The quota markers are a guess and say so.** Subtypes are snake_case while
  prose is spaced, so matching flattens separators — without that,
  `error_usage_limit_reached` silently fails to match `"usage limit"`. A missed
  marker is the expensive direction: the run would be graded as a verifier
  failure, climb for nothing, and label a model that never ran. When a real
  exhaustion is seen, paste its text into the test rather than widening the list
  blind.
- **`setting_sources=[]`.** A personal allow rule in `~/.claude` would silently
  change what a graded run is permitted to do.
- **The SDK is an optional extra** (`pip install 'aop[claude]'`). Absent, the
  internal plane is unaffected and selecting `claude_code` raises
  `ClaudeCodeUnavailable` rather than quietly running somewhere else.

`ModelEntry.base_url` is now optional for `LOCAL_PROVIDERS`, with a model
validator that still requires an endpoint for every HTTP provider — a deleted
base_url should fail at load, not at the first dispatch.

Original design follows.

### Slot 48b, as designed

**New:** `src/aop/execution/claude_code.py`. **Dependency:** `claude-agent-sdk`
(add to `pyproject.toml`; not currently installed).

`ClaudeSDKClient` rather than `query()` — the session must persist across the
tool loop. `ClaudeAgentOptions`:

| Option | Value | Why |
|---|---|---|
| `cwd` | `jail.root` | first containment layer |
| `model` | `registry.model_id(role)` | keeps the ladder alive — three Claude models on one subscription. No model literal enters `src/`, so `test_no_model_name_appears_outside_config` stays green |
| `hooks` | `{"PreToolUse": [HookMatcher(matcher="Write\|Edit\|NotebookEdit", hooks=[jail_hook])]}` | the real containment layer |
| `permission_mode` | `"acceptEdits"` | the hook is the gate; prompting has no one to answer it |
| `max_turns` | `policy.execution.max_tool_iterations` | reuses the existing cap |
| `setting_sources` | `[]` | never inherit the user's `~/.claude` settings into a graded run |

The prompt stays `render_spec(spec)`, so the anti-drift property — conductor
fills a form, we render it — survives the plane swap.

**The guard hook reuses `PathJail.resolve_for_write()`**, which already raises
`GuardDenied` for both jail escapes *and* frozen files. Not re-expressed as deny
rules — the same escape-tested object:

```python
async def jail_hook(input_data, tool_use_id, context):
    try:
        jail.resolve_for_write(input_data["tool_input"].get("file_path", ""))
    except GuardDenied as exc:
        return {"hookSpecificOutput": {
            "hookEventName": input_data["hook_event_name"],
            "permissionDecision": "deny",
            "permissionDecisionReason": str(exc)}}
    return {}
```

Load-bearing per the SDK's documented evaluation order: **hooks run before every
other permission step, and a hook `deny` applies even under `bypassPermissions`.**

**Open decision inside the slot — the `Bash` tool.** `guards/commands.py` is an
argv-list allowlist with no shell, ever. Claude Code's `Bash` takes a shell
*string*, so it cannot be wrapped the way the path jail is. Recommendation:
`disallowed_tools=["Bash"]`, which removes the tool from Claude's context
entirely. It can still write code; we still run the gate. Revisit only if 48d
shows it materially hurts.

**The outcome** satisfies `PlaneOutcome` with no `ChatResponse`, which is the
point: `served_model_id`, `cost_usd`/`usage` from `ResultMessage.total_cost_usd`
+ `input_tokens`/`output_tokens`, `latency_ms` from `duration_ms`, `exhausted`
when the turn cap is hit. Recorded via `record_spend(purpose="attempt")` so the
ledger stays complete even at zero marginal cost.

**Registry:** `provider = "claude_code"` per role. Prices default to
`Decimal("0")` and `Registry.is_free()` already separates *spent zero* from
*nothing wired up*. One snag: `base_url` validates for an http scheme and
`claude_code` has none — needs a sentinel or a provider carve-out in
`core/config.py`.

**Tests** (`tests/test_claude_code.py`) inject `query`, so no real CLI runs in
the suite. The carrying test is **a frozen acceptance file is unwritable by
Claude Code**, driven through the existing escape suite rather than a happy path.

**Blocked on:** which Claude plan tier. Max exposes three model rungs; lower
tiers may give one, in which case 48d measures "Claude Code vs our tool loop"
rather than "their ladder vs ours". Both are valid experiments.

### Slot 48c, built — the failover chain

**693 tests green.** Slot 69 cashed early, widened from the conductor to every
role. The shipped `config/registry.toml` is untouched: every role has a chain of
one, `advance()` returns `None`, and the behaviour is exactly what it was.

`ModelEntry.fallback` is a list of same-strength vendors. `Registry` gained
`chain` / `advance` / `has_fallback` / `reset`, and `entry()` now resolves
through an active-vendor pointer, so **model id, prices, capabilities and
credentials all move together** — a sideways step re-prices and re-credentials
itself with no further wiring.

The ladder change is four lines: on `RETRY_SAME_TIER` where the class is
`TRANSPORT`, advance the vendor first. No new `Action` was needed —
`core/failures.decide()` already routed transport failures to a same-tier retry
with `trains_router=False`, so the two axes were **already** separated and this
slot only had to use the separation:

| Trigger | Class | Move |
|---|---|---|
| gate rejected the work | `VERIFIER` | **up** a tier, trains the router |
| quota / credit / transport dead | `TRANSPORT` | **sideways**, trains nothing |

Four decisions worth keeping:

- **The vendor pointer is process-wide, not per-task.** Running out of credit is
  a property of the vendor and the key, not of the task that discovered it.
  Per-task state looks tidier and makes every concurrent task pay its own failed
  dispatch to learn the same fact. `test_the_vendor_pointer_is_process_wide`
  pins this down, because it is the obvious thing for a later refactor to
  "clean up".
- **The chain is flat.** A fallback may not declare its own fallback — a tree
  has no obvious traversal order, so "which vendor is next" would stop being
  answerable and the answer would depend on where the failure happened.
- **`advance()` reports exhaustion rather than wrapping.** Wrapping to the
  primary would loop forever on a vendor already known dead.
- **`reset()` exists although nothing calls it on a timer.** Quota comes back;
  without it one bad afternoon pins the process to its last-resort vendor until
  restart.

**Failover is off during eval**, pinned by `Harness` itself on a deep copy rather
than trusted to config, and recorded as `RunReport.failover` so a reader never
has to wonder whether the numbers are a blend. A run that half-finished on Kimi
while labelled `claude_code` is the exact mistake the harness exists to prevent.

**Budget guard:** dormant at flat rate, live the instant failover spends dollars.

Only a DeepSeek key exists today. [PRICING.md](PRICING.md) has verified prices
and paste-ready blocks for Kimi and Qwen; GLM was never researched. The mechanism
is built — adding a rung is now a **config edit**, which is the whole point.

## Block J2.5 — the audit, and Slot 48f (instrument repair)

*22 Aug 2026. A full audit against these notes, before starting Block K. The
execution core held; the **measurement layer** did not, and three findings
recorded above as settled were not supported by the code meant to produce them.
Slot 48f fixed the instrument. **771 tests green.***

### What the audit checked and found sound

`pytest` reports 771 passed / 1 skipped, verified rather than assumed. The 48
completed slots exist as described, the module map matches the tree, and the
guard architecture is real. Every defect below sits in the layer that *reports*
whether the system works — which is exactly where the two "wired to nothing"
warnings above predicted it would.

### F-01 — the cost verdict was never computed by anything ✅ fixed

`TaskResult.billable_cost_usd` shipped as a field with a full docstring, a
`RunReport.cost` property that reads it, and unit tests exercising it — while
`Harness._run_one` **never set it**. Every saved run carried `null`, `cost` fell
through to `list_cost`, and the artifact reported Claude Code at **$10.81**
against DeepSeek's $0.52: a 20x penalty, the exact error this file claims was
fixed. The tests were green because they constructed `TaskResult` by hand — they
*were* the missing producer.

The `$0.3096` written above was a manual ledger query typed into the notes. It is
also slightly wrong: it summed the whole database, including abandoned re-runs.
Joining the ledger to the eleven reported tasks gives **$0.2650**.

Fixed in `_run_one` by reading `store.task_spend(id)` (billable only). The saved
`claude_code.json` was backfilled from the ledger — a deterministic join on
directive plus the exact rollup the report recorded, 11/11 matched — rather than
re-run, since the ledger is the durable authority and a re-run costs $0.31 and
100 minutes to learn nothing new. Pre-fix originals are in git at `94ecc61`.

**The verdict this changes:**

```
before   KEEP INCUMBENT — candidate matched on pass-rate but costs 22.58x
after    PROMOTE        — same or better pass-rate (90% vs 80%) at 0.55x the cost
```

### F-02 — the escalation ladder has never escalated ✅ recorded, deferred by decision

`high` and `max` in `config/registry.toml` are **byte-identical** — same
`model_id` (`deepseek-v4-pro`), same temperature, same capabilities. All three
`config-claude` rungs are `claude-sonnet-5`, which that file's comments state
honestly. The consequence was recorded nowhere: **no run has ever escalated to a
stronger model.** Every "full ladder climb" above — `topk-shortfall` included —
was N attempts at one model with the failure text appended.

The Slot 48d comparison survives this; Sonnet beating `deepseek-v4-pro` is still
a real result. Any claim about *the ladder* does not.

**Decision: tiering is deferred.** The ladder is a retry counter until a
genuinely stronger `max` exists. It stays built and tested; it is not reasoned
from. Revisit when a second vendor arrives — which is also when `48c`'s failover
chain stops being dormant.

### F-03 — the router is a constant function

The conductor emitted `difficulty_hint = "medium"` for **13 specs out of 13**.
Never `simple`, never `hard`. Those drive the router's two largest weights
(`+0.45` / `−0.25`), so neither ever fires and every score starts and mostly
stays at the `0.45` base — inside the `high` band.

| Tier | Attempts | Share |
|---|---|---|
| `high` | 16 | 76% — the default, reached by inertia rather than scoring |
| `max` | 4 | 19% — and identical to `high` anyway (F-02) |
| `low` | **1** | **5% — effectively dead** |

Slot 40's logic is fine; its input is degenerate, so routing has had no
measurable effect on any run. `tier_stats` was built to answer "is `low` earning
its place" — the answer is no, because it is never asked. The fix is at the
conductor or nowhere. **This also settles Slot 73 on evidence:** a learned router
trained on these labels would be learning from a constant.

### F-04 — the scope-drift check cannot fire, on two counts

`check_plan` can refuse a plan that "touches files outside the declared scope."
It never will. `allowed_paths` is never passed by any caller, and the check
compares against `spec.artifacts`, which is `[]` in **all 13 emitted specs**. The
same emptiness covers `inputs` (`{}` in all 13) and `constraints` (`[]` in all
13): three fields of the conductor↔worker contract are declared, versioned,
validated — and never populated by the conductor that owns them.

A guard that cannot fire is worse than an absent one, because the architecture
says it is covered. Not fixed in 48f; it belongs with 48e, which is also about
what the conductor emits.

### F-06 — a dollar ceiling does not bound a flat-rate plane

`per_task_usd = "0.10"`, and on the Claude Code run `impossible-offline` ran
**28 minutes and four attempts** for $3.22 of list-equivalent work. The guard
reads billable spend and flat-rate attempts record `billable = 0`, so nothing
intervened. Correct for money, as the 48c note says — but it leaves *time*
unguarded. If a guard's unit can go to zero, it needs a second unit.

### Slot 48f — what was built

| | |
|---|---|
| `Harness._run_one` | reads `store.task_spend(id)` and sets `billable_cost_usd` when it differs from the rollup |
| `Operator(plane=…)` | plane injection, matching the existing `transport` / `gate` / `clock` seams |
| `RunReport.plane` | records the plane that **actually ran** — an injected plane under its own type name, never the configured one's |
| `aop compare a.json b.json` | `Comparison` reachable from a shell; prints the narrowed answer only when the strict verdict refused |
| `_banner` | calls `Registry.missing_credentials()` — names missing keys up front instead of one failed dispatch at a time. Also prints the plane |
| `tests/test_cli.py` | new; the CLI had no tests at all |

**The carrying test is `test_the_harness_reports_real_money_not_list_price`.** It
injects a flat-rate plane into a real `Harness` and asserts `cost < list_cost`.
Verified to **fail without the two-line fix** — a test that builds `TaskResult`
itself cannot catch a missing producer, which is precisely how the original bug
stayed green.

Housekeeping: removed `probe.db-shm` / `probe.db-wal` (orphaned, no database) and
`claude_code.2026-08-22T1751.json` (byte-identical rotation copy; git is the
provenance).

---

## Block J3 — the conductor's gate (Slot 48e) — DO THIS BEFORE BLOCK K

*Promoted 22 Aug 2026 by the 48d result, which found it by accident.*

`underspecified` — the directive **"Make the retriever better."** — is in the
suite marked `expect_pass = false` because it should be handed back. **Both
planes completed it and the gate certified both**, on every run, at $0.079 and
$0.31 respectively.

That is not an executor problem, and 48d proved it: swapping the entire
implementation plane changed nothing. The conductor accepts a directive with no
checkable content, emits a spec, authorship writes tests against whatever it
invented, and the gate then passes work nobody can evaluate. It is the
`acceptance: []` failure from the first live run wearing a different costume —
that one was fixed by refusing empty criteria, and this is the same hole one
level up: criteria that exist but assert nothing.

| # | Slot | Done when |
|---|---|---|
| 48e | ✅ Refuse under-specified directives at the plan checkpoint | `underspecified` is handed back rather than completed, and a directive with genuine content is unaffected |

### Slot 48e, built — the falsifiability check

`conductor/rationale.falsifiability()`, refused through `check_plan`, wired at
`operator.py` and switchable via `conductor.require_falsifiable_directive`.
**781 tests green.** Measured on the real pipeline, not just in tests:

| directive | attempts | outcome |
|---|---|---|
| *"Make the retriever better."* | **0** | handed back at the plan checkpoint |
| *"Refactor BM25Retriever to be more maintainable."* | **0** | handed back — naming a real class does not rescue it |
| *"Delete the unused imports to clean up the module."* | 4 | ran, as it should |

`aop eval --tag refusal` now scores **2/2 in 0.3 seconds at zero cost.** Those two
tasks previously cost **$0.194 and ~41 minutes** on DeepSeek (**$6.99 list and
~52 minutes** on Claude Code), and `underspecified` was scored wrong at the end
of it.

**The rule: an evaluative directive must say what "done" looks like.** The
trigger is an evaluative term (`better`, `improve`, `robust`, `maintainable`,
`clean up`, …) with nothing anywhere in the directive to check it against —
no quantity, no quoted example, no enumeration, no stated outcome, and no
concrete deliverable stated ahead of the purpose clause. A directive with no
evaluative word is never touched: it is describing a defect or naming a
deliverable, and both are checkable however tersely they are written.

#### What the held-out set was for

The candidate lever this block originally recorded — *does the directive name a
referent present in the staged fixture?* — scored **11/11 on the shipped suite it
was derived from and 12/20 on held-out directives.** Three false refusals and
five false accepts. It was overfit, exactly as the ⚠️ above warned, and writing
the twenty directives **before** designing anything is the only reason that was
visible rather than shipped.

| rule | shipped suite | held-out |
|---|---|---|
| referent test (the original candidate) | 11/11 | **12/20** |
| falsifiability (shipped) | 11/11 | **20/20** |

`evals/holdout-directives.toml` holds all of it, and
`test_the_gate_scores_the_held_out_directives` asserts the count so the set
cannot quietly shrink. **Re-score against it whenever the rule is touched.**

**Two honest caveats, both recorded in the file itself:**

- **Eight more cases were added during implementation and are marked
  `heldout = false`.** Probing by hand found a shape the twenty missed — a
  concrete deliverable carrying an evaluative *motivation* — and the first
  working gate refused six of eight, including *"delete the unused imports to
  clean up the module."* That is the over-refusal failure, the worse of the two.
  The rule was changed to fix it, so those cases are regression tests, not
  evidence. The twenty remain the only honest score.
- **Two mechanical fixes were made against the held-out set** (a digit inside
  `BM25Retriever` reading as a threshold). Bug fixes rather than new signals, but
  it is still tuning against the test set and is written down as such.

#### What it does not catch

**The second failure mode is still open.** `impossible-offline` is now refused —
but on the word *"Improve"*, not on the fact that a live server and production
logs do not exist. Reworded as *"Fine-tune the embedding model on the production
query logs so recall@10 rises 5%"* it would be accepted and burn a full ladder.
Detecting an **unavailable premise** needs criteria checked against the staged
filesystem, and that needs the conductor to populate `spec.inputs` — which
**F-04** says it never does. That is the natural next slot in this area, and it
closes F-04 at the same time.

**This changes the measuring setup.** A future run of the shipped suite is no
longer directly comparable to `deepseek.json` or `claude_code.json` on the two
refusal tasks: they now cost nothing and refuse in milliseconds. Expect DeepSeek
to move 9/11 → 10/11.

**The hard part is not detecting vagueness, it is not over-refusing.** A gate
that rejects real work is worse than one that accepts vague work, because the
failure is silent and the user just stops trusting it. The existing
`check_plan` (`conductor/rationale.py`) is the right home — deterministic,
already able to refuse a plan, already carrying no weight from the model's own
prose, and already wired into the pipeline at `operator.py` (it refuses on
`not rationale.trustworthy`).

### The diagnosis above is wrong — corrected by the 22 Aug audit

Two claims in this block do not survive contact with what the conductor actually
emitted. Read this before building.

**The criteria are not vague.** For *"Make the retriever better."* the conductor
emitted seven criteria, and they are specific and observable:

```
- strictly higher recall@10 than the current baseline retriever on the
  provided relevance-labeled evaluation set
- strictly higher mean reciprocal rank on the same evaluation set
- results within 250 ms for at least 95% of queries
- empty / whitespace-only / stop-words-only queries return an empty
  result set and do not throw
- every result is a JSON object with id, score, content
...
```

The defect is sharper than vagueness: they reference **a fixture that does not
exist.** There is no "provided relevance-labeled evaluation set" and no "current
baseline retriever" in the workspace. The conductor invented a premise,
authorship wrote tests against the invention, and the gate certified the result.

**And the proposed rule would break the suite.** "Criteria must not restate the
goal" assumes the goal is a restatement — but **`goal == directive` verbatim in 9
of 13 emitted specs**, including every task that must keep passing. That rule
refuses most of the working suite.

**These are two failure modes, not one, and they need two checks:**

| | Shape | Detectable by |
|---|---|---|
| **Unfalsifiable directive** | an evaluative adjective on a component — *"make X better"* — with no stated current behaviour and no target | the directive alone |
| **Unavailable premise** | criteria depending on artifacts not present in the jail — a live server, production logs, "the provided evaluation set" | the criteria **against the staged filesystem** |

`underspecified` is the first; `impossible-offline` is the second. Lumping them
together is what produced the wrong fix.

**A probed lever for the first, zero-token and deterministic:** does the directive
name a referent that exists — a path, a symbol in the staged fixture, or a quoted
literal? Across the shipped suite that separates **11 of 11**, refusing exactly
the two refusal tasks and accepting all nine that must pass. `short-token-loss`
is the one that nearly breaks it: it names no symbol, and is saved only by the
quoted literals `'Form 5'` and `'80C'`.

⚠️ **That rule was fitted to the same eleven tasks it was scored on** — eleven
samples, two of them the refusal cases. It is a promising lever, not a validated
one. **Write held-out directives before tuning it**, or 48e will encode this
suite rather than the principle. Half a dozen each of genuinely-vague and
genuinely-specific directives, written without looking at the rule, is enough.

**Related, and probably the same slot: F-04.** `spec.artifacts`, `spec.inputs`
and `spec.constraints` are empty in all 13 emitted specs, which is why the
scope-drift check can never fire — and why the "unavailable premise" check has
nothing structured to test against. A conductor that populated `inputs` would
make the second failure mode mechanically obvious rather than a text problem.

Worth noting the suite already contains the regression test: `underspecified`
and `impossible-offline` exist precisely because *"a suite of only achievable
work cannot detect a model that agrees to anything."* It detected exactly that.

## Block J4 — the Claude Code integration (Slots 49–51)

### Slot 49, built — the plane follows the vendor

`execution/plane.ProviderRoutedPlane`. **785 tests green.**

48c moved a role sideways on `TRANSPORT` but bound the plane **once, at
construction**, so only the model id moved. A `claude_code` role failing over to
an HTTP vendor kept dispatching through the Claude Code harness and handed it a
DeepSeek model id. This block's own notes named the shape correctly — *"falling
from claude_code[low] to Kimi is not a model swap, it is a plane swap"* — and
then swapped only the vendor. `tests/test_failover.py` did not contain the word
"provider": every failover test moved between two vendors sharing a plane, so the
one case the design exists for was never exercised.

**The provider is authoritative, read per dispatch.** `Registry.provider()`
resolves through the active-vendor pointer, so the plane follows the vendor by
construction rather than by anything remembering to update it. Measured on the
real config through a real `Operator`:

```
healthy      low/high/max   claude_code   claude-sonnet-5   plane=ClaudeCodePlane
saturated    low/high/max   openai        deepseek-v4-pro   plane=Worker
```

Three decisions worth keeping:

- **The ladder did not change at all.** `ProviderRoutedPlane` satisfies
  `ExecutionPlane` and delegates, so the Slot 48a seam absorbed this without the
  ladder, gate, logbook or failure taxonomy learning that planes can differ.
  That is what the protocol was for.
- **Local planes are built eagerly, routed lazily.** Both of `ClaudeCodePlane`'s
  startup checks — the SDK import and `claude` on PATH — exist because finding
  either mid-run already cost a real measurement. `_uses_provider()` scans
  fallbacks too: a role whose *fallback* is `claude_code` needs that plane before
  the failover, not after it has failed.
- **An unavailable plane still raises, never silently a Worker.** The invariant
  moved from construction to dispatch and did not weaken.

### Slot 50, built — the chain is configured, and the incumbent is preserved

`config/` is now the daily driver: **DeepSeek conductor + Claude Code execution,
each execution tier falling back to DeepSeek.** `aop status` reports
`SPENDING on: conductor` — the whole point.

| directory | what it is |
|---|---|
| `config/` | the daily driver. Claude Code preferred, DeepSeek underneath |
| `config-deepseek/` | **the preserved incumbent.** Pure DeepSeek — the side `evals/runs/deepseek.json` was measured on. Do not edit |
| `config-claude/` | the preserved 48d candidate, unchanged, no fallback |

Splitting the incumbent out matters: promoting the Claude split into `config/`
would otherwise have made the saved baseline non-reproducible, which is the one
thing the harness exists to prevent.

### Slot 51, built — the discovery scope

`guards/discovery.py` — `DiscoveryScope`, and a `locate` tool on the worker's
surface. **829 tests green**, 44 of them in `tests/test_discovery.py`.

*"A Jarvis, but instead of roaming freely it uses shell to locate."* It can now
locate, and nothing about the jail moved.

**It is a tool, not a shell command, and that is the finding.**
`guards/commands.py` allowlists *which program* may run, not *which paths it may
touch*. Adding `where` or `findstr` to that list would put filesystem reach
behind a guard that cannot see filesystem arguments — capability with no
containment. A path-aware tool is the same capability, actually guarded.

**The shape, which is Block L's Slot 54 in miniature:**

| | |
|---|---|
| allowlist of roots | deny-by-default; empty means nothing outside the jail is reachable **and the tool is not even offered** |
| denylist that always wins | checked before the allowlist, so adding `~` as a root does not re-expose `~/.ssh` |
| **paths, never contents** | `locate` answers *where* and *how big*. Reading still goes through `PathJail`, so a mistake here leaks a filename, not a key |

That last line is what makes this shippable before quarantine and dry-run exist.
Slot 54 only has to add the write half.

**Syntax hazards are reused, not restated.** `reject_dangerous_syntax` was
extracted from `PathJail` and both guards call it, so UNC, drive-relative, device
names, ADS and NUL cannot be present in one guard and missing from the other.
Same reasoning as the Claude Code hook calling `resolve_for_write` rather than
re-expressing the rule as SDK deny globs. The jail's own escape suite is
unchanged and still green against the extracted version.

**Two things worth keeping:**

- **The junction trick.** `symlink_to` needs privilege on Windows, so
  `skipif(os.name == "nt")` would have skipped the escape that matters most on
  the only platform this runs on. A **junction** (`mklink /J`) needs no
  privilege and `os.path.realpath` resolves it identically, so the link escapes
  are tested against a real reparse point here — zero skips.
- **"Refused to look" is not "found nothing".** A tool that returned `[]` when
  denied would teach the model the machine is empty. It raises instead.

#### The defect only running it could find

`locate("orchestrator")` over a real home directory **had not returned after two
minutes.** A cap on *results* does not bound a search that finds nothing —
`os.walk` keeps traversing. Inside a task that is unbounded wall clock, which is
the same shape as **F-06**.

Bounded three ways now — `max_results`, `max_depth` (8), `timeout_seconds` (5) —
and `LocateResult.truncated` says which bound was hit, because *"no matches"* and
*"gave up after five seconds"* are different answers and a model told the first
stops asking. Measured after the fix:

```
locate("orchestrator") over ~     (no matches — search stopped early: time limit)   5.0s
locate("*.toml") over the repo    6 match(es) (truncated: result cap)               0.10s
```

Verified on the real machine: `~/.ssh`, `~/.aws`, `~/.claude`, `~/.gnupg` all
denied with `~` as an allowed root; `id_rsa`, `*.pem`, `.env`, `*.kdbx` return
nothing; UNC, drive-relative, `C:/Windows` and device names all refused.

**Shipped closed.** Every policy file has `roots = []`, so this grants nothing
until someone uncomments a root — and a test asserts that for all three configs.

---

## Block K — Voice (Slots 49–53)

| # | Slot | Done when |
|---|---|---|
| 49 | Audio capture — push-to-talk | Hotkey records, release produces clean audio |
| 50 | Local STT (Whisper-class, ONNX) | Speech → text offline, at usable latency |
| 51 | TTS — local (Piper) or Windows SAPI | Text → speech, interruptible mid-sentence |
| 52 | **I/O mode matrix** | All four combinations switch live, mid-session |
| 53 | Barge-in; wake word behind a flag | Speaking over it stops playback immediately |

**Slot 52 is the one that matters.** Input and output mode are independent runtime
settings, not a build-time choice:

| | Voice out | Text out |
|---|---|---|
| **Voice in** | default | dictation into a quiet room |
| **Text in** | headphones | quiet mode |

Plus **show-and-explain**: the overlay carries detail (code, diffs, paths) while
speech carries narration. Speech is bad at file paths; screens are bad at holding
attention. Each gets what it's good at.

Speech is **one more source feeding the same structured intake**, the same way
perception distils to one observation schema — the conductor never learns whether
input was typed or spoken.

---

## Block L — System hygiene (Slots 54–59)

| # | Slot | Done when |
|---|---|---|
| 54 | `SystemScope` guard — allowlist of cleanable locations, denylist that always wins | Every escape attempt denied; jail root and VCS trees never cleanable |
| 55 | Quarantine store — move with retention, restore, expire | Nothing unlinked directly; anything removed is restorable until expiry |
| 56 | Inspectors — disk by location, stale caches, orphaned files | A sized, aged, sorted report with no side effects |
| 57 | Inspectors — scheduled tasks, startup items, services | Same, for non-filesystem cruft |
| 58 | Hygiene verifiers — space delta, task/service state | A verdict states what actually changed, measured not claimed |
| 59 | Dry-run → confirm → act → verify flow | Nothing touched without explicit confirmation of a specific proposal |

**Slot 54 is the dangerous one.** Deny-by-default, denylist beats allowlist, and
the denylist covers system directories, user documents, anything under version
control, and the workspace jail root. Test it the way `PathJail` was tested — with
an escape suite, not with optimism.

**Slot 55 is what makes the rest safe to get wrong.** Quarantine-then-expire means
a bad judgement is recoverable for a week rather than immediately permanent.

---

## Block M — Proactivity (Slots 60–63)

The genuinely new plane. In direct tension with the cost model, which assumes the
conductor sleeps — so the filter must be free.

| # | Slot | Done when |
|---|---|---|
| 60 | Global on/off switch — `policy.toml` + UI toggle | Off means zero observation processing, provably |
| 61 | **Deterministic trigger rules** — zero-token, like the guards | A trigger fires with no model call at all |
| 62 | Notification surface — overlay cards | It tells you; it never starts work |
| 63 | Suppression and rate limiting | It doesn't raise the same thing twice |

**Slot 61 is the architectural crux.** A cheap deterministic layer decides whether
something is worth surfacing — the same shape the guards already use, a free filter
in front of an expensive model. Waking the conductor on activity would be ruinous,
so it never does: notifications render from rules, and only you wake the conductor.
This adds a fifth category to `WAKES_CONDUCTOR`, defaulting to *not* waking it.

First triggers come from Block L: disk crossing a threshold, caches past an age, a
pile of stale scheduled tasks. Useful before perception exists.

---

## Block N — Perception (Slots 64–68)

| # | Slot | Done when |
|---|---|---|
| 64 | Screen capture + focus tracking (`mss`) | Current window and focus available as data |
| 65 | UI Automation tree reader | Element tree from a real app, no COM hangs |
| 66 | Coverage-threshold detector | Below threshold, backend switches mechanically |
| 67 | OCR + box-proposal fallback (RapidOCR ONNX) | Canvas/Electron apps still yield elements |
| 68 | Observation adapter | Both backends emit an identical `Observation` |

`Observation` and `a11y_coverage_threshold` **already exist** from Slots 03 and 02.
This fills them in. UIA needs its own thread with `CoInitialize`, isolated in a
subprocess so a COM hang can't take the orchestrator down. Ship OCR plus contour
proposals first; upgrade the detector later behind the schema.

---

## Block O — Hardening (Slots 69–73)

| # | Slot | Note |
|---|---|---|
| 69 | Multi-key failover | The conductor is a single point of failure and the only thing that talks to you |
| 70 | Dashboard — spend, tier stats, checkpoint counts | Answers "why did this cost so much" |
| 71 | Multi-day soak | The real test of an always-on daemon |
| 72 | Candidate-vs-incumbent eval harness | §3.1's safe-upgrade path; also settles the open allocation questions |
| 73 | Learned router | **Optional.** Rules stay the permanent fallback; promote only if it wins on the saved suite |

---

## Deferred, with reasons

- **GUI automation** (driving apps by clicking). Not what was meant, and a whole
  new plane where the verifier gate doesn't obviously apply to "did it click the
  right thing".
- **Personal/semantic memory store.** Argued against when the target looked
  industrial. Scoped to dev work and hygiene, the journal and logbook cover most of
  it — but it's the first thing to revisit if the assistant feels like it doesn't
  know you. It would be a *second* store, not a change to the pruned-context one.
- **External connectors.** Gmail, Calendar, Drive and Spotify connectors exist but
  are **unauthorised** — they need authorising through claude.ai connector settings
  and are unavailable until then. Not needed here, but a natural later integration.

---

## Verification

Per slot: `.\.venv\Scripts\python.exe -m pytest tests/ -q`, green before the
session ends. Beyond that, the two habits that have actually caught bugs:

- **Run the pipeline, not just the tests.** The last three real defects — a cost of
  `0E-10`, an event stream claiming four attempts against three logbook rows, and a
  router demoting all ordinary work to the cheap tier — passed every unit test and
  were only visible end to end.
- **Named tests for silent rules.** Guard trips must not escalate; the cached prefix
  must survive a retry byte-identically; the implementer must not be able to edit
  its own acceptance tests.

New end-to-end checks:

- **Presence:** submit a directive from the overlay, watch the ladder climb, see
  the verdict — all on mocks.
- **Voice:** speak a directive, get spoken confirmation, switch to text-out
  mid-session without restarting.
- **Hygiene:** point it at a seeded temp tree; assert the dry-run reports correct
  sizes, that nothing moves without confirmation, that removal goes to quarantine,
  that restore works, and that **every denylist path is refused**.
- **Proactivity:** switch off → assert zero observation processing. Switch on →
  assert a trigger fires and produces a notification with **no model call**.
- **Perception:** point it at an Electron app; assert the coverage threshold trips
  and OCR produces the same `Observation` shape as UIA.
- **Restart:** kill the daemon mid-task and confirm resume; delete the database and
  confirm `Journal.recover()` rebuilds from `OPERATOR.md` alone.

---

## Suggested first session

**Slots 41–42 only** — local service plus overlay, on mocks, zero spend. The
shortest path from "well-tested backend" to "a thing on my screen doing
something". A week of looking at it will tell you more than further backend work
would.
