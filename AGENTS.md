## Rules

- Keep it simple.
- Do not rewrite the whole project unless asked.
- Do not add new frameworks/libraries unless approved first.
- Match the existing style.
- Explain risky changes before making them.
- Prefer readable code over clever code.
- Fix the root cause, not just the symptom.
- Keep changes easy to review.
- Can you make sure to add must known knowledge to the README

## Before Editing
- Understand how the app currently works.
- Identify the smallest useful change.
- Check for existing helpers before adding new ones.

## While Editing

- Make one logical change at a time.
- Preserve existing behavior unless asked.
- Add comments everywhere you can.
- Do not rename things unless needed for clarity.
- Avoid abstractions until repeated 3 times.


## Dependencies

- Do not use `latest`.
- Pin versions explicitly.
- Prefer versions stable for ~60 days.
- Do not upgrade dependencies unless asked.
- Avoid brand-new packages.
- Keep dependency count low.
- Check new or changed dependencies with Snyk.
- Do not introduce high or critical vulnerabilities.
- If found:
  - choose a safer version
  - or suggest an alternative package
- Do not mass-upgrade just to clear warnings.
- Focus on real risk, not noise (ignore low unless relevant).
- Make sure that before you're done writing code, you update the requirements.txt with any new libraries
- When installing new packages, check online sources like snyk to make sure they contain no critical or high severity risk vulnerabilities

When adding a dependency, explain:
- why it is needed
- why that version was chosen
- any security risk
Unless I recommended/require it