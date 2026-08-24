# Operator — design document

One tool. You install it once and turn it on. From then on your coding agent
stops guessing and stops lying, and it costs you less to run.

It calls no model to do this.

Working name is `aop`, which nobody chose on purpose. See Open decisions.

This is a clean-start document. It assumes no existing code.

---

## 1. The problem, in plain terms

You pay for Claude Code. You still run out of tokens. You use it as-is, without
extra tooling, because tooling is a hassle and most of it asks for
configuration you do not want to give.

Three things go wrong in a long session.

**It hunts.** The agent does not know your codebase, so it greps, opens a file,
guesses, opens three more. Every one of those files stays in the context window
and is re-sent on every following turn. One unnecessary read of a 600-line file
is not a one-time cost — it is a tax for the rest of the session. This is why
sessions get slower and more expensive the longer they run.

**It lies.** Not maliciously. It writes real code in real files, writes the test
that grades it, passes its own test, and reports success. Nothing was
fabricated. The work is still wrong, and you find out later.

**It talks.** Preamble, narration of an edit you can already see in the diff,
closing summaries recapping what just happened. Nobody reads it. You pay for all
of it.

The order matters and it is not the order people assume. Rework from confident
wrongness is the most expensive, because it costs the doomed attempt, the
correction, and the context bloat both leave behind. Hunting is next. Talking is
the smallest, and also the easiest to fix, which is why it ships first.

---

## 2. The answer, in one line

**Graphify makes the agent stop guessing. Operator makes it stop lying.**

Graphify hands the agent a map of the codebase so it does not have to hunt.
Operator decides what may be done to that codebase and whether it was actually
done. A map with no rules stops nothing; rules with no map cannot tell what you
are pointing at.

That division is the whole architecture, and section 4 explains exactly where
the line falls.

---

## 3. What gets built

Three cuts. All deterministic. All free at runtime. None calls a model.

**Cut 1 — stop it lying.** Refuse work that cannot succeed, before a token is
spent. Freeze the success criteria before implementation so the thing being
graded cannot write its own grade. Return a verdict that distinguishes "the
model was wrong" from "our tooling broke".

**Cut 2 — stop it hunting.** Consume Graphify's graph. Use it for three things
Graphify does not do: refusing directives that name things which do not exist,
bounding where the agent may write, and expanding what the gate checks to
everything the change could have broken.

**Cut 3 — stop it talking.** An injected output contract that removes ceremony
always and substantive prose only when the turn does not need it. Never a hard
cap. Details in section 7, because the naive version of this makes things worse.

---

## 4. Where Graphify ends and Operator begins

This section exists because the boundary is easy to blur and expensive to get
wrong.

Two failure modes get lumped together as "the model lied", and only one of them
is structural.

**Hallucinating structure.** The agent calls a method that does not exist,
assumes a module layout, greps around confirming its own guess. This is a
grounding problem. Graphify solves it — on-device tree-sitter parsing, real
nodes, real edges, no model in the loop. We do not rebuild any part of it.

**Claiming completion falsely.** The agent uses real symbols in real files,
writes the tests, passes them, reports done. The graph has no objection because
nothing was fabricated. A map cannot detect this. There is nothing structurally
false to find.

The same split governs refusal. "Make the retriever better" — Graphify will
happily show the agent exactly where the retriever is. Every symbol resolves.
The directive is still unfalsifiable, because it has no success condition, and
success conditions are not a structural property of code.

So the two things that are independently ours, and would survive Graphify
disappearing tomorrow:

- Refusing a directive with no checkable success condition.
- Structurally separating the author of the criteria from the implementer.

These are the reason this project exists. The graph-powered checks in section 6
are genuine additions, but they are our logic over their data — treat them as
additions, never as the pitch.

Graphify is wired as an optional extra with a working fallback. A fast-moving
upstream schema is not something to couple hard to.

---

## 5. Architecture — the session protocol

The agent owns the ordering of its own work. Left alone it will write the tests
and the implementation in the same breath, which is exactly the failure above.
The guarantee is recovered by interposing at session boundaries rather than
mid-loop.

```
aop begin "<directive>"      ← ours.   Refuse if it cannot succeed.
     ↓
freeze the criteria           ← ours.   Different actor. Hashed.
     ↓
the agent works               ← theirs. Plans and implements normally.
     ↓
hooks fire on every write     ← ours.   Frozen path denied. Out of scope denied.
     ↓
aop verify                    ← ours.   Gate runs. Verdict and ledger line.
```

### 5.1 The one place a model is touched

Someone must write the criteria, and it must not be the actor that writes the
implementation. Three sources, in priority order:

1. **The user supplies them.** `aop begin --criteria tests/spec_login.py`. Free,
   deterministic, highest trust. The documented happy path for CI.
2. **Derived from the graph.** For narrow directives — rename X to Y, add a null
   check to Z — the criteria are mechanically derivable from the symbol table
   with no model at all.
3. **One cheap model call.** Different actor from the implementer, cheap tier,
   opt-in, user-supplied key. The only model call anywhere in the tool.

If none is available, `aop begin` says so plainly and runs advisory. It never
silently pretends to gate.

### 5.2 Enforcement is honest per host

"Works everywhere" is true of installation, not of enforcement. Document the
difference.

| Surface | Enforcement | What to claim |
|---|---|---|
| CI (`aop verify` as a build step) | Unbypassable. The agent cannot route around it. | Strongest claim available. Lead with it. |
| Claude Code | Strong. Hooks fire deterministically. | Verify the hook contract before building. |
| Cursor / Codex / Antigravity | Advisory to partial. | Say so. Do not list them as supported until tested. |

An MCP tool is not enforcement — the model chooses whether to call it. Hooks
where hooks exist, CI everywhere else, MCP as ergonomics only.

**Verify before building:** exact hook names, payload shape, and whether a hook
can deny or only observe, per host. Do not design against remembered API names.

---

## 6. What the graph buys us

### 6.1 Refuse work that names nothing

The hard case is not "make it better". It is a directive that is perfectly
concrete and refers to things that do not exist. "Fine-tune the embedding model
on the production query logs so recall@10 rises 5%" reads as a real task and
burns a full attempt cycle.

With the graph: does a node matching `embedding model` exist? Does anything
matching `query log` exist? If not, refuse — zero tokens, before dispatch, with
a message naming the missing referent.

Highest-value single use of the graph.

### 6.2 Bound the write scope

Resolve the directive's named symbols to nodes, traverse one or two hops along
call and import edges, and that set is the legal write scope. Feed it to the
write hook. An agent that cannot wander into unrelated files also cannot burn
context reading them — one guard, two benefits.

### 6.3 Expand the gate to the blast radius

The frozen criteria answer "did you do what you said". They do not answer "did
you break something else". With a call graph, every caller of every changed
symbol is known and the gate can require those tests too. "Your test passed and
you broke four callers" is a verdict nothing else in this space produces.

### 6.4 Two disciplines that are not optional

**Only refuse on an `EXTRACTED` edge.** Graphify tags edges `EXTRACTED`
(explicit in source) or `INFERRED` (its own resolution). Refusing real work on
an inference is over-refusal, and over-refusal is the worse failure everywhere
in this document.

**Staleness is a state, not an error.** A graph built three commits ago is old,
not wrong, and the two demand different behaviour. Compare the graph's build
point against the working tree. If stale, degrade to advisory and say so. Never
silently trust a stale map; never hard-fail on one.

---

## 7. The output contract

The naive version — always be terse — is a hardcoded limit, and a severed answer
costs a whole extra turn to recover. That is worse than the verbosity it saved.
The target is *unrequested* length, not length.

### 7.1 Three layers, three policies

**Thinking — never touched.** Reasoning is where correctness comes from. Trading
it for brevity buys a confidently wrong answer, which is the thing this project
exists to prevent. No rule here ever reduces how much the model thinks.

**Ceremony — always cut, every turn, no classifier needed.** Opening
acknowledgements, narrating an edit the diff already shows, restating the
request back, closing recaps, transitional filler between tool calls. No
information is lost, so this half carries no risk. It is also probably fifteen
to twenty-five percent on its own.

**Substantive prose — conditional.** Reasoning, tradeoffs, alternatives,
explanation. The only layer needing judgment, and where all the risk sits.

### 7.2 Classify the turn, do not budget the tokens

| Turn kind | Correct output | Substantive prose |
|---|---|---|
| Mechanical edit — rename, add a guard, fix a typo | the diff | none |
| Status or lookup | one line | none |
| Multi-file change | diff plus short plan | brief |
| Debug or diagnose | the reasoning | full — cutting this makes it wrong |
| Design or architecture | the argument | full |
| Explain or teach | the explanation | full |
| Unclassified | — | full, by default |

The classifier is deterministic over the prompt: imperative verb, presence of a
named symbol, question form, files in scope, whether the previous turn was a
correction. Discrete class out, never a score thresholded into vagueness.

### 7.3 The rules that keep it from doing harm

**It fails permissive.** The errors are asymmetric. Wrongly allowing length
costs tokens. Wrongly trimming a debug answer destroys information and costs a
recovery turn. Trim only on high confidence; allow whenever unsure. A classifier
that degenerates to "allow everything" costs money; one that degenerates to
"trim everything" destroys the product. Build it so the degenerate direction is
the harmless one, and test for degeneracy explicitly.

**Warnings are exempt.** Flagged uncertainty, security notes, destructive-action
caveats, "I did not verify this" — never cut, at any turn class. Suppressing a
caveat to save tokens manufactures the exact failure this tool exists to stop.

**Budget is a signal, never a wall.** Exceeding the expected length is recorded
for calibration. Nothing is truncated mid-answer, ever.

**Override is one word.** "explain", "why", "in detail" lifts the contract for
that turn. No flag, no config, no restart.

**Progressive disclosure over deletion.** Short form leads, long form available
on request. The reasoning still exists; it is not spent on every reader upfront.

Parameters live in `policy.toml`, because their correct values are an empirical
question. The taxonomy is versioned, since changing it changes what every
recorded measurement means.

---

## 8. Measurement

This tool refuses unfalsifiable claims. Shipping it on one would be
self-refuting.

**Baseline first, before any behavioural change.** Per host, on a fixed suite:
total tokens per completed task, wall clock, turns, prose-to-diff ratio.

**Metrics.**

- Tokens per *completed* task, not per turn. Halving turn size and doubling
  turns achieves nothing.
- Prose over diff tokens — Cut 3's saving.
- **Follow-up rate — Cut 3's harm, and the more important of the two.** How
  often the user's next message is a clarification request. Trimming that
  provokes a follow-up spent a full turn to save half of one. If follow-up rate
  rises, the contract is wrong regardless of how good the savings look. Report
  it beside the saving, never instead of it.
- Context growth curve across a session — Cut 2.
- Refusal saving: tokens and minutes not spent on correctly refused work.
- Rework rate: tasks needing a correction turn, before and after.

**Discipline.** A rule scored on the cases it was derived from is fitted, not
measured. Hold out a directive set written before the rule exists. Anything
added afterwards to fix a discovered bug is marked as a regression test, not
evidence. Runs that graded different task sets are not comparable and the tool
must refuse to compare them.

**Known hard problem.** Measuring another program's token usage from outside may
need transcript parsing or host telemetry. Solve it in Stage B, before Cut 3,
or the whole project rests on an anecdote.

---

## 9. Jarvis

It belongs here and it is cheaper than assumed, because most of what people
actually want from Jarvis is not answering.

What they want is presence: it knows where you are in the work, what you decided
last week, what is frozen, what is in scope, what failed and why. That is
*state*, and state is free. The journal, the graph, the frozen criteria, the
ledger and the session record together are a persistent memory that costs
nothing to keep and nothing to query.

**The rule that keeps it free: Jarvis reads state and never plans.** The moment
it reasons about what you should do next it becomes a planner, and planners are
the single most expensive thing in an agent loop.

By cost:

1. **Free.** `aop status` and `aop why` answer from the journal and the graph.
   What is frozen, what is in scope, the last verdict, what changed since the
   graph was built, what has been spent. Zero tokens.
2. **Nearly free.** A local surface — small TUI or an HTTP/WS view — rendering
   the same state live. It is a view over a database.
3. **Cheap.** Cross-session recall answered by lexical search over the journal.
   Zero tokens if lexical.
4. **Expensive, optional extra, last, possibly never.** Voice and
   always-listening. Nothing above it may ever depend on it.

Jarvis is a client of state, forever. It must never import the core. That seam
is what lets the riskiest surface be built last and thrown away cheaply.

---

## 10. Build order

Each stage ships alone. Nothing later is needed to make anything earlier useful.

**Stage A — floor.** Cross-platform from day one: Linux, macOS, Windows. A
single-platform package gets zero adoption. Package name, licence, PyPI, CI. One
install command, one uninstall command that fully reverts.

**Stage B — measurement.** The baseline harness from section 8, and the
host-side token accounting. Before any behavioural change.

**Stage C — refusal.** Falsifiability: refuse directives with no checkable
success condition. This is the front door — it asks nothing of the user, changes
no habits, and it is the one result already worth showing.

**Stage D — the contract.** Cut 3. Ceremony-only trimming first, classifier
after, both behind a single off switch.

**Stage E — the gate.** `aop begin`, the freeze, the write hooks, `aop verify`,
the verdict and ledger. Opt-in, behind the refusal, because it asks the user to
state criteria first and that is a habit change.

**Stage F — the graph.** Referent check first, then write scope, then blast
radius.

**Stage G — the quality axis.** Complexity and fan-in budgets, frozen benchmark
assertions using the same freeze mechanism, diff-churn ceiling. Nobody else has
this.

**Stage H — other hosts.** Only once one host is finished.

**Stage I — Jarvis levels 1 to 3.** Level 4 whenever, or never.

A through E is a real tool. F and G are what make it worth keeping.

Note the ordering: refusal before the gate, because refusal requires no
behaviour change and the gate requires test-first discipline. Leading with the
gate leads with the room most people will not walk into.

---

## 11. Making it install-worthy

A good tool nobody keeps is a failed tool. These are load-bearing, not polish.

**Ask nothing on day one.** The front door requires zero behaviour change. The
parts that require discipline sit behind it, opt-in.

**Never block by default.** Ship in report mode — it tells the user what it
*would* have blocked. Enforcement is a flag they flip once they trust it. One
false positive on an enforcing tool means uninstall. This is how linters won:
warn before error.

**Zero decisions.** No key, no config file, no model choice, no mode question.

**Advertise the exit.** `uninstall` on the first screen of the README, fully
reverting. Cheap exit is what makes people willing to enter something that
modifies their editor's behaviour.

**Name the moment, not the category.** "Saves tokens" is a reasoned pain you
have to argue someone into. "It said all tests pass — it had written the tests"
is a felt pain everyone has had.

**One reproducible number.** Not "up to 40%". A specific before-and-after on a
specific suite, with `aop demo` reproducing it locally in two minutes.
Reproducibility is the entire trust unlock for a tool whose pitch is "do not
believe confident claims".

**A fifteen-second recording.** Vague directive in, refusal out, cost and time
beside the same directive without the tool. Worth more for adoption than every
section above.

**One host, finished.** "Works with Claude Code. Cursor next." reads honest.
Four hosts listed reads as tested on none.

**Degrade gracefully, always.** No graph — still works. Stale graph — still
works and says so. Unsupported host — still works with less enforcement and says
so.

The test: hand it to five people who do not know you, watch sixty seconds
without helping, and count where they stop.

---

## 12. Rules that must not be broken

**Determinism.**

- No model call in a guard path, ever. A guard that costs tokens is not a guard,
  it is a second opinion, and it can be talked out of its answer.
- The model's own rationale never grants approval. Prose is recorded for audit;
  the machine-readable half is authoritative.
- Clock and ids are injected. Never `datetime.now()` or `uuid4()` inline.
- Money is `Decimal` stored as TEXT. Never float, never a REAL column.
- Datetimes are timezone-aware UTC, ISO-8601. Naive values rejected at the
  boundary.
- Schemas forbid extra fields. Every durable record pins a schema version.
- Tunable values live in config, not code. Their right setting is empirical.

**Refusal.**

- Over-refusing is the worse failure, everywhere. A gate that accepts a vague
  directive at least produces something to argue with; one that rejects real
  work fails silently and the user uninstalls.
- Trigger on an evaluative term with nothing to check it against, never on the
  term alone.
- A directive with no criteria is refused, not warned about. Empty criteria do
  not make the gate vague — they disable it while still reporting a pass.
- Only refuse on `EXTRACTED` graph edges.
- Every refusal names exactly what was missing and offers an escape hatch.

**Output.**

- Never reduce thinking. The contract governs what is written, not what is
  reasoned.
- Warnings, caveats and destructive-action notices are exempt from every
  trimming rule.
- Never truncate mid-answer. A budget records; it does not interrupt.
- The classifier fails permissive. Test for degeneracy in both directions.
- Report the harm metric beside the saving metric.

**Verdicts.**

- A failed exit code is not automatically a verdict. `pytest` exits 1 both when
  tests fail and when pytest is not installed. Confirm the check ran.
- Distinguish "the model was wrong" from "our tooling broke". Only the first may
  trigger anything.
- A guard trip is not a verifier failure.
- The directive is immutable and hashed at creation. Recovery reproduces the
  hash, never recomputes it.

**Containment.**

- Nothing writes outside the jail root.
- Denylist before allowlist, always, applied to every result rather than only
  the search root.
- A result cap does not bound a search that finds nothing. Depth and a clock do,
  and the result reports which bound was hit — "no matches" and "gave up after
  five seconds" are different answers.
- Anything durable the agent could rewrite is a way to pass without working. The
  frozen criteria file is the obvious case; apply the same test to anything new.
- Path-syntax rejection is shared, not duplicated. Two copies drift, and the one
  that drifts is the one nobody runs an escape suite against.

**Testing.**

- Run the pipeline, not just the tests. Green suites hide missing producers.
- A test that constructs the record itself cannot catch a missing producer.
- A component green in isolation may be wired to nothing. Check who calls it.
- The suite has its own config and its own mock provider. A suite whose
  behaviour depends on which key you happen to have configured is not testing
  what it thinks.

---

## 13. Open decisions

1. **The package name.** Also the public identity of the whole thing.
2. **Personal project or adopted tool.** Section 11 assumes the second, and the
   second carries a documentation and support burden the first does not.
3. **The cheap criteria-authoring call — in or out of v1.** In: the gate works
   with no user effort. Out: the tool stays genuinely zero-key. Current lean is
   out, documented as an opt-in extra.
4. **Path containment on Linux** — platform-aware, or paranoid everywhere and
   over-strict about legal filenames.
5. **How token usage is measured per host.** Blocks Stage B.
6. **Graphify as hard dependency or extra.** Current lean: extra with fallback.
7. **How the turn classifier is built.** Rules over the prompt, or a small
   trained discriminative model. Rules first — inspectable, no training set,
   cannot silently degenerate. Revisit only if measured follow-up rate says the
   rules misclassify.

---

## 14. Honest risks

**The input side is already occupied.** Graphify solves retrieval, is free,
adopted and well funded. Any version of this that competes there loses. The plan
consumes it deliberately. If Graphify later ships its own gate the
differentiation narrows — the defence is to be the thing already integrated with
it rather than the thing duplicating it. This is also why section 4 insists the
pitch rests on the two checks that need no graph.

**The core value asks for a habit people refuse.** Stating success criteria
before implementing is test-first development. It is free, proven, decades old,
and most developers still skip it. This is the real adoption risk and it is not
technical, which means no amount of good code fixes it. The mitigation is the
ordering in section 10 — lead with refusal, which asks nothing.

**Measuring savings from outside is genuinely hard.** If Stage B produces no
credible number, Cuts 2 and 3 become unprovable marketing.

**Enforcement does not port.** Hooks are per-host and weaker hosts get weaker
guarantees. Mitigation: lead with CI, where the guarantee is absolute.

**The output contract could make the tool worse rather than cheaper.** The
saving is easy to measure and the harm is not. Mitigations are in section 7, but
the real defence is that follow-up rate is tracked from Stage B onward and the
cut is reverted if it rises. Ship it behind a single off switch.

**Over-refusal kills adoption faster than under-refusal.** One wrongly blocked
task and the user uninstalls. Report mode by default exists for this reason.

**Scope.** This is one person. Stages A through E are a solo project. The whole
document is not. Ship A through E, then reassess against real users rather than
against the plan.
