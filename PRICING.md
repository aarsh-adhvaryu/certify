# Slot 41 — price and model verification

*Researched 9 August 2026. **No decision taken; nothing bought.** Prices move —
re-check the vendor console before paying.*

The spec's model choices and prices were written down without checking. This is
the check. Short version: **the spec was close to right**, and one of its original
choices that I argued against turns out to have been correct.

---

## The plan — measure, don't argue

Two of the three questions below **should not be settled by research**. They are
empirical, and the instrument now exists.

- **`high`** turns entirely on how much of your work is pixel-bound. Qwen buys
  multimodality at 4.6× the price of DeepSeek V4-Pro. Answer it by counting
  `needs_pixels` in the logbook after a week of real use.
- **`max`** only matters for the ~10% of tasks that get there after the gate has
  already rejected two cheaper attempts. Low frequency, low stakes.
- **The conductor is the one worth real effort** — ~90% of the bill, and the
  candidates differ 7.8× on output price.

### Phases

| Phase | What | Spend |
|---|---|---|
| 0 | ✅ Eval suite + harness built (`aop eval`) | £0 |
| 1 | One **DeepSeek** key, all four roles; prompt tuning | ~$2 |
| 2 | Baseline suite run, recorded as the incumbent | ~$0.50 |
| 3 | Conductor bake-off: **GLM-5.2** and **Kimi K3** against it | ~$5 |
| 4 | Read `needs_pixels` off the logbook; settle `high` and `max` | £0 |

**DeepSeek first because prompt tuning is where tokens burn.** Getting the
conductor to reliably emit valid specs takes dozens of iterations; at Flash rates
that is cents, at K3 rates it is a few dollars for identical learning.

### Phase 1 is done — what the first live run cost and found

**DeepSeek key bought and wired. Total spend so far: $0.0024.**

| | |
|---|---|
| Three probe calls | $0.0001 |
| First real task, end to end | $0.0023 |
| Verdict | `done`, verified by pytest, 2 attempts |

The task — *"create src/backoff.py containing a function exponential_backoff(attempt)
that returns 2 ** attempt seconds, capped at 60"* — produced correct code and
correct tests. Attempt 0 errored (`pytest` invoked before any `tests/` directory
existed), was classed `transport` rather than `verifier`, retried at the same
tier without escalating, and passed. The ladder behaved exactly as designed.

A real task costs **~$0.0023**, against my $0.0033 estimate. At those rates the
$1.00 daily ceiling is roughly 400 tasks.

**Two defects the first run exposed, both now fixed:**

- **The gate was theater.** The conductor emitted `acceptance: []`. That is
  schema-valid, so `check_plan` only *warned*; the authorship step then found
  nothing to write and froze no test file; and the implementer wrote both the
  implementation and the tests it was graded by. pytest passed, the task
  reported success, and the whole point of Slot 37 was bypassed silently. Empty
  criteria are now a **refusal**, and the conductor prompt states they are
  mandatory with examples of good and bad ones.
- **The test suite started making live API calls.** Ten test files loaded the
  project's `config/`, which was fine while it named the mock. Pointing it at
  DeepSeek meant `pytest` began spending money and failing on any machine without
  the key. The suite now has its own config at `tests/config/`, always mock —
  verified by running the whole suite with the credential unset.
- **The budget guard could not see the conductor.** Cost was summed from
  `attempts`, so it counted execution only: the planning call and the test-author
  call were unrecorded, unpriced and outside the ceiling. A conductor looping on
  spec repair could have spent without limit against the component the spec names
  as cost risk #1. There is now a `spend` ledger (migration 2) recording every
  billable call with a purpose tag; `attempts` stays the router's training set,
  because a planning call is not evidence about tier capability.

### What a task really costs

With the accounting fixed, the same task breaks down as:

| Purpose | Role | Tokens | Cost |
|---|---|---|---|
| plan | conductor | 857 in (512 cached), **3,605 out** | $0.00329 |
| authorship | low | — | $0.00165 |
| attempt | high | 4,377 in, 338 out | $0.00054 |
| **total** | | | **$0.00548** |

**The conductor is 60% of the bill**, and emitted 3,605 output tokens against 338
for the work itself. Measured, not estimated — and it is the spec's central cost
claim holding up on real traffic. It is also the strongest possible argument for
the checkpoint discipline in Slot 35: the lever is how often the conductor thinks,
not which tier the router picks.

A real task costs **~$0.0055**, so the $1.00 daily ceiling is roughly 180 tasks
and the 11-task eval suite is about **$0.06**.

### Phase 2 attempted, interrupted

The baseline run was stopped part-way (laptop shut down). **$0.081 spent, 5 tasks
done, 2 left stuck in `running`, no report written** — the scheduler reclaims
orphans on start, but the run is not a usable baseline and must be repeated from
a clean slate.

What the partial run did establish:

- **Real suite tasks cost ~$0.0104 each**, roughly double the trivial `backoff`
  task. Budget the full suite at **~$0.12**, and a candidate comparison at ~$0.25.
- **73% of spend is planning plus test authorship** (47% + 26%), against 27% for
  implementation. That reframes where optimisation is worth doing, and it is what
  prompted the open question about delegating execution to Claude Code — see
  [NEXT-PLAN.md](NEXT-PLAN.md).

```powershell
# 1. credential — never into registry.toml; the loader rejects pasted secrets
$env:DEEPSEEK_API_KEY = "sk-..."

# 2. ceilings BEFORE the first call
#    policy.toml -> [budget] per_task_usd = "0.10", per_day_usd = "2.00"

# 3. point every role at the one key (see the paste block below)

# 4. one real task, watched
.\.venv\Scripts\python.exe -m aop app
.\.venv\Scripts\python.exe -m aop run "add a docstring to src/bm25_retriever.py"

# 5. baseline the suite
.\.venv\Scripts\python.exe -m aop eval --out evals/runs/deepseek.json

# 6. swap the conductor, re-run, compare
.\.venv\Scripts\python.exe -m aop eval --label glm --out evals/runs/glm.json
```

**Expect one thing on day one:** V4-Pro is text-only, so any task with
`needs_pixels` raises `NoCapableTier`. Correct behaviour, worth seeing once.

---

## The decision to take next session

Four role slots, four models. The recommendation below is a starting point, not a
conclusion — the registry makes every row a config edit, and the eval harness
(§3.1) is what should actually settle it once there is real traffic.

| Role | Recommended | In / Out $/M | Cached in | Why |
|---|---|---|---|---|
| `conductor` | **GLM-5.2** | 0.60 / 1.92 | — | Tool use ≈ Opus, ~3× faster output, 7.8× cheaper than K3 |
| `low` | **DeepSeek V4-Flash** | 0.14 / 0.28 | 0.0028 | Intelligence 52 at $0.10 blended — best value found |
| `high` | **Qwen3.8-Max** | 2.00 / 6.00 | 0.25 | Intelligence 58, multimodal, restores the visual ladder |
| `max` | **Kimi K3** | 3.00 / 15.00 | 0.30 | Agentic 89.5 vs 59.1, best independent validation, multimodal |

**Estimated cost: ~$0.013 per task, ~$26/month at 2,000 tasks** — against ~$0.030
and ~$59 for the spec's original all-frontier allocation.

### The three live questions

1. **`high`: Qwen3.8-Max or DeepSeek V4-Pro?** Qwen buys multimodality and a
   higher intelligence index; V4-Pro is **4.6× cheaper** ($0.435/$0.87) and scores
   better on coding. This turns entirely on how much of your work is pixel-bound —
   which the logbook can answer by counting specs carrying `needs_pixels`.
2. **`conductor`: GLM-5.2 or keep Kimi K3?** K3 is measurably better at agentic
   work, which is what a conductor does. GLM is ~7.8× cheaper on output and ~3×
   faster, on the only model sitting on the interactive path.
3. **`max`: Kimi K3 or Qwen3.8-Max?** Near-identical intelligence (57–60 vs 58);
   Qwen is roughly half the blended price. K3's advantage is that its score is
   independently measured rather than vendor-reported.

None of these has to be right first time. That is the point of the registry.

---

## Verified data

### Artificial Analysis

Two snapshots disagree slightly — AA re-measures, and the "max reasoning" setting
shifts scores — so read these as ranges.

| Model | Intelligence | Blended $/M | Output tok/s | TTFT | Context | Modality |
|---|---|---|---|---|---|---|
| Kimi K3 | **57–60** | 2.30 | 43–62 | — | 1M | text + image + video |
| **Qwen3.8-Max** | **58** (#9/185) | 1.18 | 85 | **2.90s** | 1M | text + image + video |
| GLM-5.2 | 51–53 | 0.90 | **123–168** | — | 1M | unconfirmed |
| DeepSeek V4-Flash | 52 | **0.10** | 128 | — | 1M | text only |
| DeepSeek V4-Pro | 44 (max reasoning) | ~0.60 | ~62 | — | 1M | text only |
| MiniMax-M3 | 45 | 0.20 | 112 | — | 1M | — |

### Task-level benchmarks

| | Kimi K3 | Qwen3.8-Max | DeepSeek V4-Pro | GLM-5.2 |
|---|---|---|---|---|
| SWE-bench Verified | 76.8% | — | **80.6%** | ~77.8% |
| SWE-bench Pro | — | **67.7** | — | 62.1 |
| LiveCodeBench | — | — | **93.5% (#1 global)** | — |
| Terminal-Bench | 88.3 | 86.6* | — | — |
| Agentic tasks | **89.5** | 74.8 (CoWork)* | 59.1 | — |
| OSWorld-Verified | — | **86.1** | — | — |
| MCP-Atlas (tool use) | — | — | — | 76.8–77.0 |
| AA-Omniscience hallucination | — | — | **94%** | — |

\* Vendor-reported on Alibaba's own harnesses — discount relative to K3's
independently measured AA score.

### Prices as verified

| Model | Provider | Model ID | In $/M | Cached in $/M | Out $/M |
|---|---|---|---|---|---|
| Kimi K3 | Moonshot | `kimi-k3` | 3.00 | 0.30 | 15.00 |
| Kimi K2.6 | Moonshot | `kimi-k2.6` | 0.95 | — | 4.00 |
| Qwen3.8-Max | Alibaba | `qwen3.8-max` | 2.00 | 0.25 | 6.00 |
| Qwen3.5 Plus | Alibaba | `qwen3.5-plus` | 0.40 | — | 2.40 |
| Qwen3.5 Flash | Alibaba | `qwen3.5-flash` | 0.10 | — | 0.40 |
| GLM-5.2 | Z.AI | `glm-5.2` | 0.60 | — | 1.92 |
| DeepSeek V4-Pro | DeepSeek | `deepseek-v4-pro` | 0.435 | 0.003625 | 0.87 |
| DeepSeek V4-Flash | DeepSeek | `deepseek-v4-flash` | 0.14 | 0.0028 | 0.28 |

The spec's §4 table said Kimi $3.00/$15.00/$0.30 — **exact**. DeepSeek V4-Pro
~$0.44/~$0.87 — actual $0.435/$0.87. Qwen-Max $2.00/$6.00 — **exact**.

---

## Five findings worth keeping

**1. DeepSeek's cache-hit is not a discount, it is a different order of
magnitude.** $0.003625/M against $0.435/M on V4-Pro — **120× cheaper**, where
other vendors offer 2–10×. On a stable prefix, DeepSeek input is effectively free,
which strengthens the retry-appends-to-tail rule rather than weakening it.

**2. K3's distinctive edge is agentic, not raw coding.** DeepSeek V4-Pro beats it
on SWE-bench Verified (80.6 vs 76.8) and is #1 globally on LiveCodeBench. Where K3
pulls decisively ahead is agentic tasks — 89.5 against 59.1. So "great at coding"
slightly mis-locates it: it is great at *multi-step tool-driven work*, which is
what both the conductor seat and the `max` tier actually do.

**3. DeepSeek V4-Pro hallucinates at 94% on AA-Omniscience.** Disqualifying for a
user-facing conductor; largely irrelevant at an execution tier, because everything
it produces passes a deterministic gate before it is trusted. The verifier gate
earns its place here — it makes an exceptional-but-unreliable coder safe to use
where a gateless system could not.

**4. Qwen3.8-Max's 2.90s TTFT is the first hard number confirming the latency
concern.** Three seconds before the first token, on a retry, is exactly what the
cache-preserving append rule avoids. `append_on_retry` is worth more than the cost
saving alone suggested.

**5. Kimi K3's context is 1.05M tokens**, not the 200K assumed in the mock
registry. The router and the pruner both read `context_window`, so this needs
correcting when the real entry goes in.

---

## Things that could still bite

- **DeepSeek announced a price rise on 6 August 2026** — "a significant increase",
  no figure given. Separately a 2× peak-hour surcharge is announced for
  01:00–04:00 and 06:00–10:00 UTC, currently **not active**. Neither is in the
  numbers above.
- **Qwen3.8-Max is "very verbose" in reasoning output** per AA. At $6/M that is a
  cost risk the headline rate does not show.
- **Alibaba: International (Singapore) and China (Beijing) are separate
  endpoints**, with separate keys and prices. Keys are not interchangeable and
  Beijing is 60–70% cheaper.
- **GLM pricing varies by host** — $0.60/$1.92 direct, up to $1.00/$3.20 via
  third parties. The benchmarks cited are for **5.2**; the price was quoted for
  "GLM-5".
- **Moonshot's docs 301 from `platform.moonshot.ai` to `platform.kimi.ai`.**
  Whether the API host moved too is unconfirmed.

---

## Endpoints and credentials

| Provider | Base URL | Env var |
|---|---|---|
| Moonshot | `https://api.moonshot.ai/v1` | `MOONSHOT_API_KEY` |
| DeepSeek | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` |
| Alibaba (Singapore) | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` |
| Z.AI (GLM) | confirm in console | `ZAI_API_KEY` |

All speak the OpenAI dialect. Alibaba explicitly documents
`stream_options={"include_usage": true}`, which the adapter already sends and
without which the budget guard is blind.

---

## Cost model

Using verified per-token prices with the spec's assumptions: ~4,000-token standing
context cached after the first call, 2–3 conductor calls per task, 1–2 worker
calls.

| Volume | Tasks/month | Spec's original allocation | Recommended allocation |
|---|---|---|---|
| Light | 500 | ~$15 | **~$7** |
| Moderate | 2,000 | ~$59 | **~$26** |
| Heavy | 10,000 | ~$295 | **~$130** |

**The conductor is ~90% of the bill** — roughly $0.027 of the ~$0.030 per-task
average under the spec's allocation. Not the heavy tier; the thinking. That is the
empirical justification for the checkpoint discipline in Slot 35 and for
`effort.default = "low"`, and it is why the conductor seat is the highest-leverage
row in the table.

Cold vs warm conductor call at K3 rates: 4,000 in + 600 out costs **$0.021**
uncached, **$0.0102** cached. Caching roughly halves it — the §5 claim holding up.

---

## Before paying — checklist

- [ ] Decide the three live questions at the top of this file
- [ ] Confirm GLM's current model ID and rate; benchmarks cited are 5.2, price was quoted for "GLM-5"
- [ ] Confirm whether GLM-5.2 is multimodal — if not, the conductor stops being the vision-distiller and the execution ladder must carry vision
- [ ] Confirm Qwen3.8-Max's exact model ID (`qwen3.8-max` vs a `qwen-max` alias)
- [ ] Choose Alibaba Singapore vs Beijing deliberately
- [ ] Confirm whether Moonshot's API host moved with its docs
- [ ] Check whether DeepSeek's announced increase has landed
- [ ] Set `budget.per_task_usd` and `per_day_usd` in `policy.toml` **before** the first real call
- [ ] Export keys as env vars; never paste into `registry.toml`

---

## Suggested first purchase

**One key, all four roles pointed at it, smallest useful credit.**

Whichever vendor you start with, fill every role from it first. One key exercises
the entire pipeline — real tool-use, real streaming, real usage reporting — which
is the "mock-only development" risk the plan names as Block B's main danger. Every
transcript recorded becomes a permanent replay fixture (Slot 14).

- **Cheapest smoke test:** DeepSeek. Roughly $5 goes a long way at $0.14/$0.28.
- **Most useful first:** Z.AI (GLM), because the conductor carries ~90% of the
  bill and needs the most prompt tuning.

Then swap roles to their real models one at a time, re-running the saved suite
after each. That is §3.1's safe-upgrade path used on the way in rather than only
on the way up.

**Expect this once:** if every role points at a text-only model, any task with
`needs_pixels=True` raises `NoCapableTier`. That is correct behaviour, and worth
seeing so it is not a surprise later.

---

## Ready to paste

Replace the mock entries in `config/registry.toml` once the decisions above are
made. Prices are strings so they parse as exact `Decimal`, never float.

```toml
[roles.conductor]
provider = "openai"                       # generic OpenAI-dialect shim
model_id = "glm-5.2"
base_url = "CONFIRM_IN_CONSOLE"
api_key_ref = "ZAI_API_KEY"
price_in = "0.60"
price_out = "1.92"

[roles.conductor.capabilities]
context_window = 1000000
modality = "text"                         # CONFIRM — affects whether it can pre-digest images
tool_use = true
reasoning_effort = true
supports_caching = false                  # CONFIRM

[roles.low]
provider = "openai"
model_id = "deepseek-v4-flash"
base_url = "https://api.deepseek.com"
api_key_ref = "DEEPSEEK_API_KEY"
price_in = "0.14"
price_out = "0.28"
price_cached_in = "0.0028"

[roles.low.capabilities]
context_window = 1000000
modality = "text"
tool_use = true
supports_caching = true

[roles.high]
provider = "openai"
model_id = "qwen3.8-max"
base_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
api_key_ref = "DASHSCOPE_API_KEY"
price_in = "2.00"
price_out = "6.00"
price_cached_in = "0.25"

[roles.high.capabilities]
context_window = 1000000
modality = "multimodal"
tool_use = true
supports_caching = true

[roles.max]
provider = "openai"
model_id = "kimi-k3"
base_url = "https://api.moonshot.ai/v1"
api_key_ref = "MOONSHOT_API_KEY"
price_in = "3.00"
price_out = "15.00"
price_cached_in = "0.30"

[roles.max.capabilities]
context_window = 1048576
modality = "multimodal"
tool_use = true
reasoning_effort = true
supports_caching = true
```

---

## Sources

**Artificial Analysis** — [open-source models](https://artificialanalysis.ai/models/open-source) · [Qwen3.8 Max](https://artificialanalysis.ai/models/qwen3-8-max)

**Comparisons** — [K3 vs V4-Pro vs GLM-5.2](https://deepinfra.com/blog/kimi-k3-vs-deepseek-v4-pro-vs-glm-5-2) · [MarkTechPost trillion-scale MoE](https://www.marktechpost.com/2026/07/18/kimi-k3-vs-deepseek-v4-pro-vs-glm-5-2-open-trillion-scale-moe-models-compared-on-benchmarks-license-and-serving-cost/) · [Qwen vs K3 vs V4-Flash](https://kingy.ai/blog/qwen3-8-max-benchmarks-specs-kimi-k3-deepseek-v4-flash/)

**Vendors** — [DeepSeek pricing](https://deepseek.ai/pricing) · [DeepSeek docs](https://api-docs.deepseek.com/quick_start/pricing) · [Kimi platform](https://platform.kimi.ai/docs/pricing/chat) · [Model Studio OpenAI compatibility](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope) · [Z.AI GLM-5 docs](https://docs.z.ai/guides/llm/glm-5)

**Benchmarks** — [K3 coding evaluation](https://www.nxcode.io/resources/news/kimi-k3-benchmarks-coding-agent-evaluation-guide-2026) · [K3 multimodal](https://www.kimi.com/blog/kimi-k3) · [GLM-5.2 agentic](https://www.mindstudio.ai/blog/glm-5-2-vs-gpt-5-5-vs-claude-opus-agentic-workflows) · [Qwen3.8-Max computer use](https://venturebeat.com/technology/qwen3-8-max-arrives-with-a-bold-claim-it-outperforms-gpt-5-6-sol-max-and-fable-5-on-agentic-computer-use) · [Qwen3.8-Max overview](https://www.datacamp.com/blog/qwen3-8-max)
