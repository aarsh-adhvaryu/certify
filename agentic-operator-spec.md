# Agentic Operator — System Specification

*An OS-level autonomous operator that executes project and industrial work, coordinated by a single conductor model over a tiered pool of execution models. Text/tool domain only. Built and run in-house.*

---

## 1. What it is

A background system that takes a goal and does the execution work a small team would otherwise handle — planning, coding, testing, research, wiring services, document work — while keeping a human in the loop for the decisions that have no verifier.

It is **not** an app extension. It runs as an always-on OS-level daemon that follows you across the desktop (editor, browser, files) and is summoned from anywhere. The intelligence is not one model; it is an orchestration layer wrapped around a conductor and a pool of workers.

**Explicitly out of scope (this version):** image/video generation, voice interface, and the open-ended "emergent communication" research direction. Those are separable and can be added later; none are load-bearing for the industrial tool.

---

## 2. Architecture

The system is four planes plus a feedback loop. Two are cheap/deterministic (the control side); one is the expensive generative side; one is the perception surface.

**Perception (daemon).** OS-level capture: screen (screenshot + accessibility tree), focus tracking, plus speech/text input. Raw perception is **distilled to structured observations** before it reaches the conductor — the smart model reasons on structured facts, not raw pixels or audio. Cheap and low-hallucination.

**Conductor (the reasoning model).** The single point of understanding and the only thing that talks to the user. It understands intent, plans and decomposes, shapes an **optimized task spec** so the execution plane never sees a raw prompt, delegates, and supervises. It is **event-driven** — it acts at checkpoints (a step finishes, a verifier fails, a worker asks a question), not on every token. It budgets its own thinking with a low/high/max reasoning effort.

**Router (small discriminative classifier).** Not an LLM. Reads features of a task and emits a discrete tier — low / high / max. Auditable, near-free, no hallucination surface. Trained on logged verifier outcomes (supervised), with a thin exploration seam so it keeps learning without RL's failure modes.

**Execution plane (the workers).** The generative muscle, tiered:
- **low** — Qwen (cheap tier): boilerplate and simple sub-tasks.
- **high** — Qwen-Max: the strong default; autonomous work.
- **max** — DeepSeek V4-Pro: hardest reasoning and agentic coding, escalated to only when needed.

**Verifier gate.** Deterministic checks (Lean / SymPy / MCP / test suites) — not an LLM judge. Everything the execution plane produces passes the gate before it is trusted or acted on. Pass/fail is also the training signal that flows back to the router and the conductor's logbook.

**Feedback loop.** Verifier outcomes train the router and fill a logbook of coordination patterns that worked, so handoffs get tighter over the system's lifetime. Workers escalate ambiguity *up* to the conductor rather than guessing — the mechanism that prevents "confidently wrong" output cascading downstream.

### Request flow

1. You speak / type / the daemon observes → distilled to structured input.
2. Conductor (Kimi) scopes intent, produces a task spec, sets difficulty.
3. Router picks the execution tier from the spec.
4. Execution worker does the task; verifier checks it.
5. On failure or ambiguity, the worker reports back up; conductor re-plans or asks you.
6. Conductor delivers the verified result and shows/narrates progress.

---

## 3. Component choices

| Role | Choice | Why |
|---|---|---|
| Conductor | Kimi K3 | Strongest at multi-tool orchestration and tool use; steerable reasoning effort (low/high/max); natively multimodal (text/image/video), so it also serves as the vision-distiller — it can read a screenshot and emit the structured text a text-only worker consumes. The more conversational candidate, so it fits the user-facing seat. |
| Execution — low | Qwen (cheap tier) | Cheap grind for simple sub-tasks. |
| Execution — high | Qwen-Max | Strong autonomous worker; multimodal; the default. |
| Execution — max | DeepSeek V4-Pro | Difficult reasoning + agentic coding, open-weight, cheap per token. **Text-only** — fine here because perception is distilled to text before it reaches any model, and the conductor can pre-digest any images. Pixel-bound tasks route to a multimodal tier instead (see §3.1). |
| Router | Small classifier (logistic reg / small MLP / boosted trees) | Discrete, auditable, no hallucination; trained on verifier logs. |
| Verifiers | Lean / SymPy / MCP / test suites | Deterministic; also the training signal. |
| Memory | Mem0 + local vector store | Persistent state + retrieval; the shared "blackboard." |
| Orchestrator | Plain code (own loop, or a light state-machine lib) | Free; runs on your machine; only model calls cost money. |

The router, verifiers, orchestrator, and memory are all **free** (local/deterministic). API cost comes only from the conductor and the execution workers.

### 3.1 Model registry — swap models like batteries

Models will keep getting better and cheaper, so no model is hardcoded. The current picks (Kimi K3, Qwen, Qwen-Max, DeepSeek V4-Pro) are placeholders in their role-slots — the moment a better or cheaper model appears for any tier, it's a config swap. The system references **roles** (`conductor`, `low`, `high`, `max`), never model names; all model identities live in one config file. Swapping or upgrading a model is a config edit, not a code change.

Registry entry per role: `{ provider, model_id, base_url, api_key_ref, price_in, price_out, params, capability_tags }`. Because every model speaks the OpenAI-compatible dialect, a single adapter drives all of them, with tiny per-provider shims for quirks (e.g. Kimi's `reasoning_effort`, JSON-mode flags, headers). Keys live in the environment/secret store and are referenced by name, so the config is shareable without leaking secrets.

Two things belong in the registry, not the code:
- **Pricing** — the cost model reads it, so swapping a model recalculates the monthly estimate automatically.
- **Capability tags** — context window, **modality (text-only vs multimodal)**, tool-use, reasoning knob — so the router and cost model adapt to the new model instead of assuming the old one's limits.

**Modality is a routing axis, not just difficulty.** Some tiers are text-only (e.g. DeepSeek V4-Pro at `max`) while others are multimodal (Kimi K3, Qwen-Max). So the router carries a "needs raw pixels?" flag alongside its difficulty score: a task that is both hard *and* visual cannot go straight to a text-only tier — either the conductor pre-digests the image into structured text first, or the task routes to a multimodal tier. The router reads each tier's modality tag from the registry and routes around text-only tiers automatically; when you swap a model, its modality tag comes with it, so routing stays correct for free.

**Safe upgrade path (not just a config edit):** models differ in behavior — tool-use reliability, output format, context limits — so a mechanically-clean swap can still perform worse. Keep a saved suite of representative tasks and run any candidate model through it, letting the **verifier gate** grade pass-rate and cost against the incumbent; promote only if it wins. For zero-risk upgrades, canary it: shadow a small % of live traffic to the candidate and let the verifier signal decide automatically. The verifiers turn "swap the battery" into "swap and auto-validate."

---

## 4. API price reference

Per 1M tokens, input / output. Prices move — re-check before committing.

| Model | Role | Input | Output | Cached input |
|---|---|---|---|---|
| Kimi K3 | Conductor | $3.00 | $15.00 | ~$0.30 (90% off) |
| Qwen (cheap) | Exec low | ~$0.40 | ~$2.40 | ~$0.10 |
| Qwen-Max | Exec high | $2.00 | $6.00 | ~$0.25 |
| DeepSeek V4-Pro | Exec max | ~$0.44 | ~$0.87 | — |

**Critical cost note:** Kimi bills its internal *thinking* tokens as standard output at $15/M. A conductor running at reasoning-effort = max on every checkpoint is the single biggest way to inflate the bill. Keep it at low effort for routine coordination; escalate effort only for hard judgment. The conductor is your main cost, and its reasoning-effort setting is the main dial.

---

## 5. Monthly cost model

Cost is driven by two things: how hard each task is (which tier runs, and how much the conductor thinks) and how many tasks per month. Verifiers and routing add nothing.

### Per-task estimate

Assumptions: ~4,000-token standing context (instructions + project state + memory), re-sent per call but **cached** after first use; the conductor fires a few checkpoint calls per task; execution runs 1–2 worker calls. Figures are indicative, not exact.

| Task type | Conductor (Kimi) | Execution | Per-task total |
|---|---|---|---|
| Simple | ~$0.015 (1–2 low-effort calls) | Qwen low ~$0.005 | **~$0.02** |
| Medium | ~$0.035 (2–3 calls) | Qwen-Max ~$0.027 | **~$0.06** |
| Hard | ~$0.06 (3+ calls, some high effort) | DeepSeek ~$0.007 | **~$0.07** |

Note the shape: hard tasks aren't much pricier than medium, because DeepSeek execution is cheap — the conductor's thinking dominates the cost, not the heavy worker.

### Monthly scenarios

Mix assumed: 60% simple, 30% medium, 10% hard.

| Volume | Tasks / month | Estimated monthly cost |
|---|---|---|
| Light | 500 | **~$20** |
| Moderate | 2,000 | **~$75** |
| Heavy | 10,000 | **~$370** |

These sit on top of any chat subscriptions and assume disciplined reasoning-effort. Realistic swing factors, in order of impact:

1. **Conductor reasoning effort** — running max freely can 2–3x the total. This is the dial to watch.
2. **Context caching** — the standing context is re-sent every call; caching it (Kimi 90% off, Qwen-Max ~8x off) is the biggest structural saving.
3. **Escalation discipline** — how often the router promotes to Qwen-Max / DeepSeek vs. staying on the cheap tier.
4. **Decomposition restraint** — split a task only when a single call would fail; needless decomposition multiplies calls.

---

## 6. Build phases

Part-time estimates, assuming existing building blocks (agent runner, MCP tools, memory, verifiers).

- **v0 — weekend.** Orchestrator loop + conductor (Kimi) + one execution tier + one verifier, running a small task end-to-end. Rule-based routing.
- **v1 — weeks 1–2.** OS daemon (tray app, hotkey, screen capture, focus tracking) + accessibility reads + shared state/logbook + escalation-on-failure + the full low/high/max execution pool.
- **v2 — weeks 3–5.** Swap rule router for the discriminative classifier trained on verifier logs; add context caching; add the event-driven checkpoint discipline and reasoning-effort policy.
- **v3 — months 2–3+.** Reliability hardening, multi-day persistence, a dashboard to watch/steer, multi-key failover across vendors.

Rule of thumb: the happy path is ~20% of the effort; reliability, error recovery, and stopping bad handoffs from propagating is the other 80%.

---

## 7. Key risks

- **Conductor cost runaway.** Uncapped reasoning effort at every checkpoint. *Mitigation:* event-driven checkpoints, low default effort, cache the standing context.
- **Latency.** Each hop adds a round-trip; a hard multi-worker task can take seconds to tens of seconds. *Mitigation:* stream output, keep the router fast, push long work to the background, acknowledge immediately.
- **Error propagation.** A wrong result handed downstream compounds. *Mitigation:* verifier gate before trust; workers escalate ambiguity up, never guess.
- **Multi-vendor overhead.** Three API keys, three failure modes. *Mitigation:* build the happy path on one execution tier first; add failover only once it's stable.
- **Prompt drift in the reasoning plane.** Letting the conductor freely rewrite intent can distort it. *Mitigation:* the conductor emits a *structured* task spec, not a reworded prompt.

---

## 8. Hardening & edge cases (v1 / v2)

The happy path is solid; these are the corners that bite in real use. The unifying rule: wherever the design was about to trust a model to police itself, put a deterministic check or a structural constraint under it instead.

**8.1 Escalation must be verifier-driven, not self-reported.** Cheap models are poorly calibrated — they rarely know when they're in an ambiguous spot and tend to confidently produce a wrong answer. Do not rely on the low tier to raise its hand. Trigger escalation from the deterministic verifier gate:
- On the **first** verifier failure, retry once at the same tier with the verifier's failure reason injected into the prompt (catches format/transient failures cheaply).
- On the **second** failure, escalate to the next tier, bypassing the worker's own judgment. Cap total attempts so a doomed task can't loop up the bill.
- Over time, the learned router should pre-empt this: it learns which task shapes the low tier reliably fails and routes them straight to high, so you stop paying for the doomed first attempt.

**8.2 Perception needs a visual fallback.** Accessibility (A11y) trees are fragmented and often return empty/garbage for Canvas-, Flutter-, Electron-, or legacy-framework apps. Detect this mechanically (element count or % of window area covered); below threshold, fall back to a **fast local** quantized vision path — a UI-element / bounding-box detector plus OCR (set-of-marks style). Both backends emit the **same structured observation schema**, so the conductor is agnostic to which produced it (one perception adapter, two backends). This local model is the justified exception to the all-API stance: perception is on the per-frame hot path where cloud vision would be too slow and costly.

**8.3 Context pruning, separate from caching.** Caching fixes cost; it does not fix attention dilution as the standing context bloats with completed sub-tasks and stale OS observations. Add a rolling-summary / pruning step (a cheap local script or the low tier) that keeps only the active frontier. Two rules:
- **Prune to memory, not to trash.** Move completed/stale detail into the vector store so it's retrievable if a later step needs it — lossy deletion causes failures when you drop something that turns out relevant.
- **Order context so pruning doesn't bust the cache:** `[ stable cached prefix: instructions + immutable directive | volatile tail: active frontier ]`. Keep all churn in the uncached tail so the cached head stays valid.

**8.4 Anti-drift: immutable directive + mechanical guards.** When the conductor re-plans after a failure it can subtly mutate the user's original intent. Hardcode the raw original intent at the top of the conductor's system prompt as an immutable `<Directive>` (which conveniently is also the stable cache prefix from 8.3). Require the conductor to emit a structured rationale comparing each new plan against the directive before dispatching. Caveat: that self-comparison is the model grading itself — a speed bump, not a guarantee. Back the non-negotiables (allowed scope, forbidden actions, success criteria) with **deterministic guards**, not the model's say-so. Use the rationale for auditability (log every plan-vs-directive delta); use the guards for enforcement.

---

*Prices and model choices are current-as-of-writing and should be re-verified before committing. Everything except the conductor and execution calls runs locally at no per-token cost.*
