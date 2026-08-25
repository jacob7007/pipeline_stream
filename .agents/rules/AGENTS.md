# Pipeline AI Development Instructions

> These rules MUST be followed on every new task, feature, or code change in this codebase.

---

## 1. Understand Before You Code

- Before starting any work, ask the user if there are questions that could improve the idea.
- Read related files before writing any code — understand what existing functions do, what they depend on, and what depends on them.
- Never assume. If something is unclear, ask.

---

## 2. Plan Before You Build

- Before writing any code, create an `implementation_plan.md` describing what will change, which files are affected, function signatures, and edge cases.
- Wait for user approval before proceeding.
- After approval, create a `task.md` checklist and work through it one task at a time.

---

## 3. Function Placement Rules

Before writing any new function, answer:
1. What does it do? (one sentence)
2. Which file is the right place? (follow the single-responsibility principle — each file owns one concern)
3. Does a similar function already exist? (search first)

Every file in this project owns exactly one concern. Put code where its concern lives. If you are unsure, ask.

---

## 4. Code Quality Rules

- **No duplicated logic.** If the same logic exists in two places, refactor it into a shared function.
- **Small functions.** Each function does one thing. If it exceeds ~50 lines, break it up.
- **No dead code.** If removing a feature, remove all related code, imports, and references.
- **No silent errors.** Never use bare `except Exception: pass`. Always log the error at minimum.
- **No unused imports or variables.**

---

## 5. Comments

Docstrings are allowed but not required. Use `#` comments for complex logic — explain **why**, not what, in 2–3 lines max:

```python
# Competitor timestamps are in KSA (GMT+3), convert to display timezone
# before storing so all downstream code works in one consistent offset
kickoff = parse_match_time(date_str, time_str, source_tz)
```

Do not comment obvious code:

```python
# BAD: Loop through matches
for match in matches:
```

---

## 6. Before Finishing Any Task

- Re-read the implementation plan and confirm all items are addressed.
- If you changed a function signature, update ALL callers.
- If you moved a function, update ALL imports.
- If you added a new package, update `requirements.txt`.
- Verify no duplicated logic was introduced.
- Run the pipeline end-to-end if possible, or at minimum verify changed modules import without errors.

---

## 7. Git Commit Messages

- Format: `type: short description` — e.g. `fix: resolve duplicate status priority`
- Types: `feat` `fix` `refactor` `docs` `chore`
- One concern per commit. Don't mix a refactor with a new feature.

---

## 8. Anti-Patterns to Never Repeat

| Anti-Pattern | Correct Approach |
|---|---|
| Same logic in multiple files | One shared function in the file that owns that concern |
| Pass-through wrapper functions | Call the source module directly |
| Reading the same env var in multiple files | Read once — either at module level as a constant or passed as argument. Both are fine; just don't scatter reads of the same var across files |
| Giant functions doing multiple things | Break into focused helper functions |
| `except Exception: pass` | Log the error, handle gracefully |