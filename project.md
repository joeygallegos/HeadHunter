# Project Notes

## Recipe validation

When validating a generated StepScraper recipe, use `test.json` directly with
`python run.py test`. Do not create a separate temporary recipe file or custom
test harness. If `test.json` contains user-owned content, preserve and restore
it after the validation run.
