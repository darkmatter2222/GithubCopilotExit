# Agent Operating Instructions

## Objective

Implement requested changes completely within this repository. A task is **not complete** when code has merely been written. It is complete only when the requested behavior is implemented and the applicable validation succeeds, or when a concrete, unrecoverable blocker is explicitly documented with evidence.

## Required Working Pattern

### 1. Inspect before changing

- Read the relevant source files, tests, configuration, and nearby implementation patterns before writing any code.
- Identify the repository's established conventions (naming, error handling, test structure, dependency patterns) and follow them unless the task explicitly requires a change.
- Do not assume what a file contains — read it.

### 2. Implement coherently

- Make the smallest coherent set of changes that fully satisfies the requested outcome.
- Preserve all existing behavior outside the requested scope.
- Do not rewrite unrelated components or introduce new dependencies unless necessary and justified.
- Do not generate placeholder implementations. Implement the real behavior.

### 3. Validate after every change

- Run the narrowest relevant tests first (unit tests for the modified module).
- Then run the full applicable chain: formatter → linter → type checker → build → integration tests.
- Use the commands listed in the **Repository Commands** section below.
- When validation fails because of a change, diagnose the actual root cause, apply a corrective fix, and rerun validation. Do not suppress, skip, or weaken tests to obtain a passing result.

### 4. Continue until done

- Do not stop after producing a plan, outline, partial patch, or explanation.
- Do not report success while any relevant test, type check, or build step fails.
- Continue the inspect → implement → validate → fix loop until validation succeeds or a specific blocker is proven with evidence.
- "I believe this should work" is not completion. Running the validation and seeing it pass is completion.

### 5. Protect the repository

- Operate only in the assigned branch or isolated worktree.
- Do not commit, push, publish, deploy, change credentials, delete significant files, or modify infrastructure without explicit instruction from the user.
- Do not weaken tests, remove assertions, or disable linting rules to obtain a passing run.
- Do not introduce hardcoded credentials, secrets, insecure defaults, or SQL/command injection vectors.

## Completion Report

Before reporting completion, provide all of the following:

1. **What changed** — which files were modified and why.
2. **Validation commands executed** — exact commands run, in order.
3. **Validation results** — pass/fail for each command, with any relevant output.
4. **Assumptions made** — anything inferred rather than explicitly stated.
5. **Remaining limitations or known issues** — if any exist after completion.

## Repository Commands

> Fill these in for each project. Run `./scripts/setup.ps1` or your project's bootstrap command first.

```
Install dependencies : [COMMAND]
Format               : [COMMAND]
Lint                 : [COMMAND]
Type check           : [COMMAND]
Unit tests           : [COMMAND]
Integration tests    : [COMMAND or NOT APPLICABLE]
Build                : [COMMAND]
```

**Common examples by ecosystem:**

| Ecosystem | Test command | Build command |
|---|---|---|
| Python | `python -m pytest` | `python -m build` |
| Node/TypeScript | `npm test` | `npm run build` |
| .NET | `dotnet test` | `dotnet build` |
| Rust | `cargo test` | `cargo build` |
| Go | `go test ./...` | `go build ./...` |

## Architecture and Conventions

> Fill in project-specific rules. Generate a starting point with `/init` in Qwen Code or Roo Code.

- **Primary language/framework:** `[VALUE]`
- **Preferred testing pattern:** `[VALUE]`
- **Error handling convention:** `[VALUE]`
- **Logging convention:** `[VALUE]`
- **Dependency management:** `[VALUE]`
- **Security/privacy constraints:** `[VALUE]`

## Prohibited Shortcuts

- Do not claim completion without executing the relevant validation commands.
- Do not replace working implementation patterns with untested speculative abstractions.
- Do not suppress errors, remove assertions, disable tests, comment out checks, or weaken validation to obtain a passing run.
- Do not introduce secrets, hardcoded credentials, insecure defaults, or unrelated formatting churn.
- Do not modify files outside the scope of the requested change without explicit justification.
- Do not invent API signatures, function names, or module paths that do not exist — read the actual source first.
