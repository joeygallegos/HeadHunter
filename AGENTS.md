# AGENTS.md

## Core Rules

* Keep solutions simple, readable, and easy to review.
* Make the smallest useful change.
* Do not rewrite unrelated code.
* Match the existing project style and architecture.
* Preserve existing behavior unless the request requires changing it.
* Fix the root cause, not only the visible symptom.
* Do not rename or reorganize code unless needed.
* Reuse existing helpers before creating new ones.
* Avoid abstractions until the same pattern appears at least three times.
* Do not add frameworks, libraries, or services without approval.
* Explain risky, destructive, or behavior-changing work before implementing it.
* Before editing code in this repository, read [PROJECT.md](./PROJECT.md) if it exists for project specific rules.

## Before Editing

Perform a readiness review proportional to the size and risk of the request.

Check:

* How the affected code currently works.
* The smallest change that solves the request.
* Ambiguous or missing requirements.
* Hidden assumptions and edge cases.
* Security, privacy, operational, and compatibility risks.
* Existing helpers, tests, and documentation.
* How the change will be tested.
* Whether the user experience remains clear and simple.

Raise material concerns early. Do not block straightforward work over minor uncertainty.

## Implementation

* Make one logical change at a time.
* Keep diffs focused.
* Do not modify unrelated files.
* Add comments only when they explain non-obvious intent, constraints, or risk.
* Prefer clear code over clever code.
* Use clear errors, labels, and instructions.
* Keep the experience simple and understandable for corporate users.
* Do not expose sensitive data in logs, errors, or UI messages.

## Dependencies

* Do not add or upgrade dependencies unless required and approved.
* Pin dependency versions explicitly. Never use `latest`.
* Prefer established versions released at least 60 days ago.
* Use newer versions when required for a security fix or compatibility.
* Keep the dependency count low.
* Do not mass-upgrade dependencies just to remove warnings.
* Focus on meaningful risk. Ignore low-severity findings unless they are relevant or exploitable.

Before adding or changing a dependency:

1. Explain why it is needed.
2. Explain why the selected version was chosen.
3. Check Snyk and relevant official security advisories.
4. Do not introduce known high or critical vulnerabilities.
5. Choose a safer version or alternative package when needed.
6. State clearly when vulnerability data could not be verified.
7. Update `requirements.txt` or the project’s equivalent dependency file.

## Testing

* Run the most relevant tests after each logical change.
* Run the broader test suite before finishing when practical.
* Add or update tests for changed behavior.
* Test expected behavior, failure cases, and important edge cases.
* Do not claim tests passed unless they were actually run.
* Report skipped, unavailable, or failing tests clearly.

## Pre-Commit Comment Pass

After the implementation is complete and tests pass:

* Review only the code added or modified for this request.
* Add thorough inline comments for the next developer.
* Explain intent, business rules, assumptions, edge cases, security decisions, and non-obvious behavior.
* Focus comments on why the code works this way, not what each line does.
* Do not add comments to unrelated or unchanged areas.
* Do not comment obvious syntax or simple assignments.
* Keep comments accurate, useful, and close to the relevant code.
* Update or remove outdated comments in the modified area.
* Run formatting, linting, and relevant tests again after the comment pass.
* Complete this pass before declaring the work ready to commit.

## Documentation

Update the README when a change adds or materially changes:

* Features or user workflows.
* Setup or configuration.
* Dependencies.
* Commands, services, ports, or environment variables.
* Architecture or important technical behavior.
* Security or operational requirements.

Keep README updates high-level and useful. Include must-know project knowledge needed to operate, maintain, troubleshoot, or safely modify the application.

## Completion Checklist

Before finishing:

* Confirm the request was implemented without unrelated changes.
* Confirm tests were run and report the results.
* Confirm dependency files are current.
* Confirm dependency security checks were completed when applicable.
* Confirm the README contains any new must-know information.
* Summarize changed files, important decisions, risks, and remaining limitations.
