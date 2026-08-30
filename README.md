# certify

**Your coding agent said all the tests pass. It had written the tests.**

certify is a deterministic verification layer for coding agents. It refuses work
that cannot succeed, freezes the success criteria *before* implementation so the
thing being graded cannot write its own grade, and returns a verdict that tells
"the model was wrong" apart from "our tooling broke".

**It calls no model to do any of this.** No API key, no tokens, nothing to bill.

> ### ⚠️ Status: early. Nothing is runnable yet.
>
> There is no `certify` command — the CLI lands in Stage A. What exists today is
> the verified core: the refusal check, the freeze, the guards, the gate and the
> ledger, at **280 passing tests**. See [Where this actually is](#where-this-actually-is).
>
> This README describes an idea and a route, not a shipped tool. A project whose
> pitch is *"do not believe confident claims"* does not get to make one.

---

## The problem

Three things go wrong in a long agent session. They are not equally expensive,
and the order is not the one people assume.

**It lies.** Not maliciously. It writes real code in real files, writes the test
that grades it, passes its own test, and reports success. Nothing was fabricated.
The work is still wrong, and you find out later. This is the most expensive
failure, because it costs the doomed attempt, the correction, *and* the context
bloat both leave behind.

**It hunts.** The agent does not know your codebase, so it greps, opens a file,
guesses, opens three more. Every one of those files stays in the context window
and is re-sent on every following turn. One unnecessary read of a 600-line file
is not a one-time cost — it is a tax for the rest of the session.

**It talks.** Preamble, narration of an edit you can already see in the diff,
closing summaries recapping what just happened. Nobody reads it. You pay for it.

## The idea

**[Graphify](https://github.com/Graphify-Labs/graphify) makes the agent stop
guessing. certify makes it stop lying.**

Graphify hands the agent a map of your codebase so it does not have to hunt.
certify decides what may be done to that codebase, and whether it was actually
done. A map with no rules stops nothing; rules with no map cannot tell what you
are pointing at.

certify does not compete with Graphify and never will — it consumes the graph and
adds three checks Graphify does not perform. More on that
[below](#where-graphify-sits).

### The session protocol

The agent owns the ordering of its own work. Left alone it writes the tests and
the implementation in the same breath, which is exactly the failure above. The
guarantee is recovered by stepping in at the session boundaries instead of
mid-loop:

```
certify begin "<what you want>"    ← ours.   Refuse it if it cannot succeed.
        ↓
    freeze the criteria            ← ours.   Different actor. Hashed.
        ↓
    the agent works                ← theirs. Plans and implements normally.
        ↓
    hooks fire on every write      ← ours.   Frozen path denied. Out of scope denied.
        ↓
certify verify                     ← ours.   Gate runs. Verdict and ledger line.
```

### Two things that are ours alone

Everything else here is an addition. These two are the reason the project exists,
and they would survive every dependency disappearing tomorrow:

**Refusing a directive with no checkable success condition.** *"Make the retriever
better"* names a real file and resolves every symbol. It is still unanswerable —
there is no state of the world that counts as having arrived. A code graph cannot
catch this, because success conditions are not a structural property of code.

**Structurally separating the author of the criteria from the implementer.** The
criteria are fixed before implementation begins and frozen on disk. The agent can
read them and cannot edit them.

---

## Design principles

These are not aspirations. Each one is a rule with a test behind it, and most
were paid for by a real bug in the previous build.

**Over-refusing is the worse failure, everywhere.** A gate that accepts a vague
directive at least produces something to argue with. One that rejects *real* work
fails silently, and you uninstall it. Every refusal names exactly what was missing
and offers a way through.

**Never block by default.** certify will ship in report mode — it tells you what
it *would* have blocked. Enforcement is a flag you flip once you trust it. This is
how linters won: warn before error.

**No model call in a guard path, ever.** A guard that costs tokens is not a guard,
it is a second opinion — and it can be talked out of its answer.

**A failed exit code is not a verdict.** `pytest` exits 1 both when tests fail and
when pytest is not installed. Confirm the check actually *ran* before calling
anything a failure, or a broken environment reads as a weak model.

**Enforcement is honest per host.** "Works everywhere" is true of installation,
not of enforcement:

| surface | enforcement | what gets claimed |
|---|---|---|
| CI (`certify verify` as a build step) | unbypassable — the agent cannot route around it | the strongest claim available |
| Claude Code | strong — hooks fire deterministically | verified before building |
| Cursor / Codex / others | advisory to partial | said plainly, and not listed as supported until tested |

An MCP tool is not enforcement: the model chooses whether to call it.

**Degrade, never hard-fail.** No graph — still works. Stale graph — still works
and says so. Unsupported host — still works with less enforcement and says so.

---

## Where this actually is

**Phase 0 is complete.** The repository has been stripped to the deterministic
core and renamed. 280 tests pass; 30 modules.

```
src/certify/
  refusal.py    falsifiability() + check_plan()      ← the front door
  session.py    the immutable, hashed directive
  criteria.py   the freeze
  guards/       path jail · discovery · commands · budget
  verify/       the gate: static/stateful split, pytest gate
  core/         schemas · state (SQLite) · journal · config · events · lifecycle
  hosts/        claude_code.py — the PreToolUse write hook
```

The refusal check works today, if you drive it directly:

```python
from certify.refusal import falsifiability

falsifiability("Make the retriever better.")
# ('better', "the directive asks for 'better' without saying what would count
#             as having achieved it: no threshold, no example, no enumeration,
#             and no statement of what the behaviour should become. Add any one
#             of those and it becomes checkable")

falsifiability("Add a retry to upload() in src/uploader.py, retrying 3 times")
# None  — accepted
```

### Known gaps, stated rather than hidden

- **No CLI.** `certify` is not a command yet. Stage A.
- **The freeze does not survive the process.** The frozen set is held in memory,
  so containment is real within one run and absent across two. Stage E.
- **The write hook is unproven.** Its tests prove the hook *works*, not that it is
  *connected*. In the previous build a hook exactly like this was written,
  unit-tested, green — and never actually passed to the SDK. Stage E.
- **Shell writes bypass the freeze.** The hook inspects a tool's path argument; a
  shell call carries a command string instead, so `echo x > frozen.py` walks
  straight through. The previous build closed this by removing the shell tool
  outright, and that half was not carried over. Stage E.
- **No measured numbers.** None are claimed anywhere in this README. The set
  that could have produced one was spent — it was re-scored during a refactor to
  check nothing had broken, which is enough to burn it. A fresh blind set has to
  be written by someone who has not read the rule before any number here means
  anything. The protocol for that is in `evals/directives/blind.toml`.

### The route

Each stage ships alone; nothing later is needed to make anything earlier useful.
Full detail in [PLAN.md](PLAN.md).

| | stage | what it delivers |
|---|---|---|
| ✅ | **0** | strip to the deterministic core |
| | **A** | floor — cross-platform, install/uninstall, report mode |
| | **B** | measurement — the baseline, before any behaviour change |
| | **C** | refusal — the front door; asks nothing of you |
| | **D** | the output contract — stop it talking |
| | **E** | the gate — `begin`, the freeze, write hooks, `verify` |
| | **F** | the graph |
| | **G** | the quality axis — complexity and benchmark budgets |
| | **H** | other hosts |
| | **I** | state you can query, for free |

Refusal comes before the gate deliberately. Refusal asks nothing of you; the gate
asks you to state success criteria first, and that is a habit change. Leading with
the gate leads with the room most people will not walk into.

---

## Where Graphify sits

[Graphify](https://github.com/Graphify-Labs/graphify) is a separate project that
turns a codebase into a queryable graph, on-device, with tree-sitter. certify does
not vendor it, fork it, or rebuild any part of it.

Two failure modes get lumped together as "the model lied", and only one is
structural:

- **Hallucinating structure** — calls a method that does not exist, assumes a
  module layout. A grounding problem, and **Graphify's**, entirely.
- **Claiming completion falsely** — real symbols, real files, writes its own
  tests, passes them, reports done. **A map cannot detect this.** There is nothing
  structurally false to find.

Given a graph, certify adds three things Graphify does not do: refuse a directive
naming something that does not exist, bound where the agent may write, and expand
the gate to everything the change could have broken. Our logic, their data.

**Reading a graph needs no dependency at all** — `graph.json` is a file. Graphify
is needed to *produce* it, never to consume it. If it is absent, certify offers to
install it, explains what it is, and works fully without it if you decline.

---

## Development

Python 3.13, no Node, no uv.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q     # 264 passed
```

`pythonpath = ["src"]` comes from `pyproject.toml`, so tests need no setup.
Outside pytest, set `PYTHONPATH=src` or `pip install -e .`.

| document | what it is for |
|---|---|
| [OPERATOR-v2.md](OPERATOR-v2.md) | the design — why this exists, where the boundaries fall |
| [PLAN.md](PLAN.md) | the route — nine stages, every slot, in order |
| [CLAUDE.md](CLAUDE.md) | the memory — what is true now, and the rules that must not be broken |

The previous incarnation of this repository was an LLM orchestrator — a conductor
model, a tier router, an escalation ladder. It is preserved at the
`v1-orchestrator` tag. The code is not needed; what carried forward is the list of
things it got wrong, which is in `CLAUDE.md`.

## Licence

Not yet chosen, which means **all rights are reserved for now**. A permissive
licence is intended before any public release.
