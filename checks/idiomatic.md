---
id: idiomatic
label: "Idiomatic Implementation"
---

Find code that re-derives what the language already provides in a recognised built-in form, and replace it with the built-in form. The goal is the shortest implementation a fluent reader of the language would write, with no behaviour change. This check is narrower than `complexity` (which targets nested control flow and cyclomatic count), narrower than `readability` (which targets naming and function size), and distinct from `cleanup-ai-slop` (which removes code rather than rewriting it).

## When to make a replacement

Make the replacement only when ALL of the following are true:

- The behaviour is exactly preserved on every input — including None/null, empty containers, falsy values that are NOT None (e.g. `0`, `""`, `[]`), Unicode, negative numbers, paths containing `..` or symlinks, and any input the existing tests already pin.
- The replacement uses a Python built-in, standard-library symbol, a JavaScript/TypeScript built-in, or a dependency that the project ALREADY imports somewhere. No new dependency may be added by this check.
- The replacement is shorter AND a fluent reader of the language will find it clearer at a glance. If you have to think for more than a second to convince yourself the rewrite is equivalent, leave the code as it is.
- The existing test suite passes after the change, unchanged. If a test had to be edited to make the rewrite pass, the behaviour changed — revert.

When in doubt, leave the code alone. The cost of a silently-changed behaviour is much higher than the benefit of a tidier block.

## Python targets (illustrative, not exhaustive)

- Multi-step `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` chains used to compute a single relative path → `pathlib.Path(__file__).resolve().parent.parent`, but only if the project already uses `pathlib` elsewhere. Otherwise leave it.
- Hand-rolled dict merge loops → `{**a, **b}` (3.5+) or `a | b` (3.9+). Preserve the existing precedence — confirm which side wins on key conflicts and pick the operand order accordingly.
- `try: x = d[k]` / `except KeyError: x = default` patterns → `d.get(k, default)` for a read, `d.setdefault(k, default)` for a write-back. Pick by which the original code does; they are not interchangeable.
- `if x is None: x = default` → `x = default if x is None else x`. Do NOT collapse to `x = x or default` unless every falsy value (`0`, `""`, `[]`, `False`) is genuinely interchangeable with the default for this variable. Note the equivalence reasoning in the commit message.
- `for i in range(len(items)): ... items[i] ...` where the index is used only to index back into the same list → `for item in items`, or `for i, item in enumerate(items)` if the index is also used.
- Two-list parallel iteration via index → `for a, b in zip(xs, ys)`. If lengths may differ and the existing code truncates, plain `zip` matches; if it raised, use `zip(xs, ys, strict=True)` (3.10+).
- `[]` accumulator + `for ... append(f(x))` → list comprehension, but only when the comprehension fits readably on one line. A dense multi-line comprehension is worse than the loop.
- `dict()` / `list()` / `set()` constructor with a literal → `{}` / `[]` / `set()`. Preserve `dict()` when it is being called with kwargs (`dict(a=1, b=2)`) — that has no literal equivalent.
- Manually counting occurrences in a loop → `collections.Counter`. Manually building a key-to-list map → `collections.defaultdict(list)`. Manually popping from the front of a list → `collections.deque` + `popleft()`. Apply only when the surrounding code is already importing from `collections` or when the manual form is the bottleneck.
- `f.read().splitlines()` followed by per-line work with no further state → iterate `f` directly: `for line in f`. Note that iterating `f` preserves the trailing newline on each line; if the caller relies on stripped lines, keep the `.splitlines()` form or add `.rstrip("\n")`.
- String concatenation in a loop → `"".join(parts)` after building a list, when the loop is doing nothing else and the input is finite.
- `len(x) == 0` / `len(x) > 0` for built-in containers → `not x` / `x`, only when the variable is known to be a list/dict/set/str and `None` is not a valid input. If `None` is possible, the explicit `len` check or an `x is not None and not x` is correct and must stay.

## JavaScript / TypeScript targets

- Hand-written object merge (`Object.assign({}, a, b)` or a for-in loop) → `{ ...a, ...b }`.
- `arr.indexOf(x) !== -1` → `arr.includes(x)`.
- `arr.filter(...).length > 0` → `arr.some(...)`.
- `arr.filter(...)[0]` → `arr.find(...)`. Note that `.find` returns `undefined` rather than throwing — preserve the caller's handling.
- `for (let i = 0; i < arr.length; i++) { const x = arr[i]; ... }` where `i` is unused → `for (const x of arr)`.
- `Object.keys(obj).map(k => [k, obj[k]])` → `Object.entries(obj)`. `Object.entries(obj).reduce((acc, [k, v]) => ({ ...acc, [k]: f(v) }), {})` → `Object.fromEntries(Object.entries(obj).map(([k, v]) => [k, f(v)]))` when it fits.
- Promise `.then`-chains where `await` is already in scope in the enclosing function → linearise to `await`.

## Things NOT to rewrite

- Code that is already idiomatic for the project's chosen style. If the codebase consistently uses `os.path`, do not unilaterally migrate it to `pathlib` — that is a project-wide refactor decision, not an idiomatic-replacement.
- A `for` loop that does multiple things (filtering, side effects, accumulation) — compressing this to a comprehension produces a denser line that is harder to read, not easier.
- An explicit, step-by-step block whose verbosity is the point — reference implementations, slow paths kept for debugging, code under test that is deliberately written one way so the test exercises that form.
- A pattern that requires a new import to enable an idiom (e.g. importing `more-itertools` for one `pairwise` call) when the project does not already use that dependency.
- Anything where the rewrite needs an inline comment to explain what it does. If the idiom needs a comment, the original was clearer.
- The `x or default` / `x ?? default` collapse for code where falsy and null/None are NOT interchangeable. This is the most common silent behaviour change in this kind of rewrite — be conservative.
- Test code that documents how something is done in long form deliberately, for traceability.
- Operational config, retry/timeout/CORS settings, security headers — those are out of scope for this check.

## Process

1. Walk the codebase looking for the patterns above. Do not scan exhaustively — focus on hot files (the largest modules, the most-edited files, anything the run banner highlights).
2. For each candidate, mentally execute both forms on the inputs the existing tests use. If you cannot convince yourself in one pass that they are behaviour-identical, leave it.
3. Make the smallest possible edit — one replacement per commit when reasonable, grouped only when the same pattern repeats across a single file.
4. Run the full test suite after every meaningful batch. Any test failure means revert, even if you "know" the test was wrong — file a separate report rather than amending the test in this check.
5. Report what you rewrote: the file and line, the pattern, and one short sentence on why the replacement preserves behaviour.

Do not change observable behaviour. Do not add imports unless the project already uses the target module. Do not introduce new dependencies. Do not chase comprehensive coverage — a smaller set of high-confidence replacements is the goal.
