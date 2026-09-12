# Job-Specific Resume Variant and PDF Export Plan

## Goal

From a job's Application Prep card, compare the existing baseline/skeleton
resume bullets with the selected job description, identify lower-value
candidate bullets to swap out, and create a job-specific resume with
user-approved, evidence-grounded, higher-value replacements. Keep the Google
Docs baseline resume unchanged, save a generated PDF locally, and make that PDF
available for download from the same job card.

The intended outcome is a tailored PDF that the user can manually attach to
that job's application. This plan does not automate submission to a job board.

## Current State

- Application Prep creates grounded draft bullets for jobs moved forward from
  Swipe, using `resume.txt` and the optional responsibilities inventory.
- The Application Prep UI now uses a resume bullet swap workflow instead of
  presenting draft bullets for manual copying.
- The app connects to Google with a local service account for read-only
  baseline sync, keeps the baseline Google Doc unchanged, and renders generated
  PDFs locally.
- The synced Google Doc snapshot is the canonical source for PDF generation.
  `resume.txt` and the responsibilities inventory still provide prompt evidence
  for Application Prep.
- `job_application_preps` represents AI-generation state, not a generated
  resume artifact. Resume variants need their own persisted records.
- The Application Prep draft-bullet contract now uses `evidence_sources` end to
  end.
- The local PDF export path is implemented: resume source settings, encrypted
  service-account storage, Google baseline snapshot sync, swap-analysis
  validation, approved-pair validation, local PDF generation, saved variants,
  and guarded download APIs exist without any Google document mutation.

## Product Principles

1. Never modify the baseline/skeleton Google Doc.
2. Start with the existing baseline bullets, not a detached list of suggested
   new bullets.
3. Propose a swap only when it replaces a lower-value bullet for this job with
   a grounded, higher-confidence item that better supports the job description.
4. Never apply an AI draft without the user explicitly approving both the
   replacement and the baseline bullet it supersedes.
5. Do not turn inventory-only evidence into a resume claim without review.
6. Keep an auditable record of the baseline version, approved replacements,
   and locally rendered PDF.
7. Treat a baseline change as a reason to review or regenerate a variant, not
   as permission to silently overwrite it.

## User Flow

1. The user opens Settings and pastes a Google service-account JSON key.
2. The user shares the baseline Google Docs resume with the displayed service
   account email.
3. The user selects one Google Docs baseline resume by document ID. The app reads and
   snapshots its editable body bullets.
4. For a selected job, the app compares each existing baseline bullet with the
   job description and identifies a small set of lower-value swap candidates,
   explaining weaker job alignment or redundant signal.
5. For each candidate, the app proposes a grounded replacement draft with its
   source evidence, matching job requirement, and confidence. The page shows
   the original and proposed replacement side by side.
6. The user clicks **Generate PDF** and confirms the summary.
7. The app applies the approved replacements in memory, renders a local PDF,
   and stores it locally.
8. The Application Prep job card shows the variant status plus **Download PDF**.

The minimum viable release supports replacement of existing synced baseline
bullets only. The local renderer now preserves ordinary text blocks and simple
table content such as certifications/awards, but adding jobs, changing person
details, editing headers/footers, or reproducing complex Google Docs layout
exactly remains out of scope.

## Architecture

### Google integration

Prefer a service-account setup for the local personal workflow:

- The user creates a Google Cloud service account and downloads its JSON key.
- The key is pasted into the local Settings page and encrypted under
  `data/google_resume/`.
- The UI displays the service account email so the user can share the baseline
  Google Doc with that email.
- Sync operates on explicit document IDs rather than broad Drive search.
- Google Drive cloning/export is deferred because personal Gmail service
  accounts can hit Drive ownership/quota failures such as
  `storageQuotaExceeded` when calling `files.copy`.

OAuth is intentionally not supported for this workflow because Google's
consent-screen homepage/privacy requirements are too heavy for quick personal
applications. Do not commit service-account keys or downloaded Google
credential files.

### Document operations

1. Read the chosen baseline with the Google Docs API.
2. Extract eligible body-list paragraphs into stable, reviewable anchors:
   document tab/segment, start and end index, exact text, surrounding section
   heading, and a normalized text hash.
3. Evaluate each extracted baseline bullet against the selected job description
   and reviewed AI analysis. Record its relevance, strength, redundancy, and
   confidence from explicit resume evidence, along with a concise explanation.
4. Pair only lower-value eligible baseline bullets with grounded draft bullets
   that better address a specific job requirement. Do not recommend a swap just
   to maximize keyword overlap or when the proposal is less confident than the
   existing evidence.
5. At creation time, re-read the baseline and reject generation if its hash no
   longer matches the synced snapshot.
6. Apply approved replacements in memory against the synced block snapshot.
7. Render a local PDF from the resulting blocks and write it to the local
   artifact directory.
8. Record the successful variant only after the PDF bytes are written and
   hashed.

Avoid `replaceAllText`: identical bullets can appear more than once, and global
replacement could change an unintended role or project.

### Swap recommendation contract

The recommendation step must receive the current baseline-bullet snapshot, the
job description, reviewed job analysis, and grounded candidate evidence. It
returns zero to a small number of proposed swaps, rather than a mandate to
replace a fixed number of bullets. Each proposed swap records:

- the exact baseline bullet anchor and original text;
- why that bullet contributes relatively less to this job's required skills,
  responsibilities, or differentiation;
- the proposed replacement and its exact evidence source(s);
- the job requirement better supported by the replacement; and
- comparative confidence and an explanation of why the swap improves the
  resume's positioning without weakening overall role coverage.

The system must return no recommendation when the baseline bullets are already
the strongest available set. "Low value" means lower value for this specific
job, not poor experience or a poor resume bullet generally.

### Local artifact storage

Store files outside static assets, under:

```text
output/resume_variants/<safe-company>-<safe-title>-variant-<id>.pdf
```

Persist only a workspace-relative path. The download endpoint receives a variant
ID, loads the path from the database, resolves it under the approved artifact
root, and returns it as an attachment. It must not accept a client-supplied file
path.

## Data Model

Add a new `job_resume_variants` table. Keep it separate from
`job_application_preps`, whose status remains the status of AI prep generation.

Suggested fields:

| Field | Purpose |
| --- | --- |
| `id`, `job_pk` | Variant identity and job association. |
| `status`, `error_text` | `draft`, `creating`, `done`, `failed`, or `stale`. |
| `baseline_document_id` | Selected source Google Doc. |
| `baseline_version`, `baseline_hash` | Detect source edits before generation/reuse. |
| `copied_document_id`, `copied_document_url` | Reserved for a future Drive-copy flow; empty for local rendering. |
| `replacements_json` | User-approved original-anchor, job-specific value rationale, and replacement mappings. |
| `application_prep_hash` | Detect drafts that were regenerated after approval. |
| `pdf_relative_path`, `pdf_sha256` | Local, verified export artifact. |
| `created_at`, `generated_at` | Audit and UI timestamps. |

Also add a single-row resume-source settings record with the selected baseline
document ID, display name, last synced source version/hash, and sync timestamp.

## UI Flow and Interaction Design

### User goal and information architecture

The user should be able to move from a selected move-forward job to a reviewed,
downloadable tailored PDF without manually copying text between the dashboard
and Google Docs. Keep this inside the existing Application Prep job detail,
rather than creating a separate navigation area or a document-editor-like
screen.

Use a compact three-stage flow in the selected job panel:

```text
Job context and baseline status
        |
1. Match bullet swaps  ->  2. Review changes  ->  3. Generate PDF
```

The stage indicator is informative, not a blocking multi-page wizard. The user
stays in one familiar Application Prep panel, and the next stage becomes active
as soon as its prerequisite is complete. Persist in-progress matches server-side
as a variant draft so a refresh or job switch does not discard the work.

### Entry and preflight state

At the top of a selected Application Prep job, keep the existing job title,
company/site, match, and posting link. Directly beneath it, add one quiet
baseline status row:

- **Baseline ready — Resume name** with its last-synced time, or
- **Connect baseline resume** as the only primary action when Google is not yet
  connected or no baseline has been selected.

Do not show OAuth, document IDs, raw hashes, or setup detail during normal job
review. Put reconnect, change-baseline, and stale-source explanations behind a
small **Resume settings** disclosure. When the baseline has changed, show an
inline warning and a single **Sync before matching** action; do not let the user
make selections against an unseen document revision.

### Stage 1: Match bullet swaps

This is a quiz-inspired matching board, optimized for fast deliberate decisions
rather than freeform document editing. Use two labelled columns with a small,
always-visible match tray below them:

```text
Swap out                                      Bring forward
Less useful for this role                     Stronger fit for this role
┌──────────────────────────┐                  ┌──────────────────────────┐
│ Current resume bullet A  │                  │ Proposed bullet 1        │
│ Less useful: ...         │  select both ->  │ High confidence · ...    │
└──────────────────────────┘                  └──────────────────────────┘
┌──────────────────────────┐                  ┌──────────────────────────┐
│ Current resume bullet B  │                  │ Proposed bullet 2        │
└──────────────────────────┘                  └──────────────────────────┘

Your planned swaps:  A → 1,  B → 2                 Review 2 swaps
```

- Use **Swap out** and **Bring forward** as the visible labels. Reserve the
  internal term "low value" for rationale text; it should never imply that the
  person's experience is low value.
- Show only the small set of job-specific candidate bullets initially, normally
  up to four per column. Provide **Show other resume bullets** rather than
  overwhelming the user with the whole document.
- Each current-bullet card shows the full bullet, its resume section, and a
  one-sentence **Why less useful for this role** explanation. It can never be
  removed merely because it lacks a keyword.
- Each proposed-bullet card shows the proposed text, a separate **Strong match**
  or **High confidence** badge, and the job requirement it improves. Its exact
  evidence is available in an expandable **Why this is safe** detail, keeping
  the board scannable.
- A user selects one card on the left and one on the right; the selected cards
  receive a clear border, check icon, and short instruction such as **Now choose
  a replacement**. Once both are selected, create a reversible pair in the
  match tray. This is the primary interaction.
- Optional pointer drag-and-drop may be added later, but matching must not
  depend on drag-and-drop. Click/tap selection is faster to understand, works
  on touch screens, and is fully keyboard accessible.
- A recommended pair may be visually marked **Suggested**, but it is never
  preselected. The user can pair any eligible, grounded proposal with any
  eligible baseline bullet.
- Matched cards remain visible with a **Paired** label and become unavailable
  for a second match. The tray offers **Change** and **Remove** controls for
  every pair.
- If no improvement is justified, show **No swaps recommended for this job**,
  explain that the existing bullets are already the stronger set, and let the
  user return to the job deck without creating a variant.

### Stage 2: Review changes

Selecting **Review N swaps** opens an in-panel high-level review, not a new
page. Lead with a concise outcome summary: number of swaps, job requirements
now better supported, and any remaining meaningful gaps. Follow it with an
easy-to-scan before/after list:

```text
Replace                                      With
Current bullet A                             Proposed bullet 1
Why: less relevant to <requirement>          Evidence: <short source summary>
```

Use progressive disclosure for the full source evidence and AI rationale. The
default review should answer only: what will change, why it improves this job's
positioning, and whether the evidence is safe. Keep a **Back to matching**
action beside the primary **Create tailored resume and PDF** action.

Before enabling creation, validate that every pair has a current anchor, no
anchor is used twice, every proposed bullet is grounded, and the baseline and
Application Prep snapshots are still current. If validation fails, return the
user to the affected match with a specific explanation instead of a generic
error banner.

### Stage 3: Creation and handoff

After confirmation, replace the primary button with a compact visible progress
sequence: **Checking baseline**, **Applying 3 approved swaps**, **Rendering
PDF**, and **Saving your resume**. Disable duplicate submission but preserve
the reviewed pairs in the variant record.

On success, show a completion card at the top of the job detail:

- **Tailored resume ready** and the file name.
- Primary action: **Download PDF**.
- Secondary action: **View changes** or **Create another version**.

State plainly that the PDF is downloaded by the browser and must be attached to
the employer's application manually. If the application runs on a remote host,
the UI must distinguish the server-side saved artifact from the browser download.

### Responsive, accessibility, and feedback requirements

- On desktop, use the two-column matching board and a sticky match tray. On
  small screens, stack the columns as **1. Choose a bullet to swap out** then
  **2. Choose a bullet to bring forward**, while keeping the selected pair
  context and match tray fixed near the bottom.
- Use the existing slate, indigo, white-card language from Application Prep;
  do not introduce a new visual system. Use short cards, consistent spacing,
  and text labels in addition to color.
- Make every card a semantic button with an accessible name that includes its
  role and selection state. Support Tab, Enter, and Space for selection;
  announce pairing, removal, validation errors, and export progress through a
  polite live region.
- Keep keyboard focus in a predictable order: job context, left candidates,
  right candidates, match tray, review action. After pairing, move focus to the
  newly created tray item; after removal, return it to the originating card.
- Provide loading skeletons for evaluation, explicit empty states, and inline
  recovery actions for disconnected Google access, stale baseline, unsupported
  layout, failed export, and expired authorization.

## UI and API Changes

In Application Prep:

- Add the baseline status row, three-stage indicator, matching board, review
  panel, creation progress, completion card, and per-job variant history.
- Preserve the existing job deck and job header so the new flow feels like the
  next step of Application Prep rather than a second product.
- Store draft matches as they are created, then send the approved pair list,
  expected baseline version/hash, and prep hash when the user confirms export.

Suggested endpoints:

- `GET /api/resume-source/settings`
- `PUT /api/resume-source/settings`
- `GET /api/resume-source/google/status`
- `PUT /api/resume-source/google/config`
- `POST /api/resume-source/google/disconnect`
- `POST /api/resume-source/google/select`
- `POST /api/resume-source/sync`
- `GET /api/application-prep/jobs/<job_pk>/resume-variants/swap-analysis`
- `POST /api/application-prep/jobs/<job_pk>/resume-variants`
- `GET /api/application-prep/jobs/<job_pk>/resume-variants`
- `GET /api/resume-variants/<variant_id>/download`

The create request includes selected replacements plus the expected baseline
version/hash and prep hash. The server rejects stale input rather than creating
a resume from unseen source changes.

## Implementation Sequence

1. Done: repair and test the Application Prep JSON contract.
2. Done: add additive models, runtime schema helpers, source settings, variant
   draft APIs, and swap/pair validation contracts.
3. Done: add localhost-only service-account credential configuration,
   selected-document sync, encrypted local key storage, disconnect cleanup, and
   README setup notes.
4. Done for read-only sync: implement conservative body-list bullet snapshot
   extraction with document indices, section context, revision metadata, and
   baseline hash. Write-time anchor re-resolution remains part of export work.
5. Done for the simple UI: add deterministic baseline-bullet relevance preview,
   swap recommendations, click-to-pair matching board, review panel, and draft
   persistence. Draft saves rebuild the current server-side analysis and reject
   stale browser hashes.
5.5. Done: replace broad keyword-overlap recommendations with requirement-
   specific swap scoring that compares each baseline bullet to each grounded
   replacement's job requirement, accounts for evidence confidence and durable
   resume signals, omits already-strong baseline bullets, and requires an
   analysis hash for every draft save.
6. Done for Phase 4 UI: add the barebones service-account Google baseline setup
   workflow in Settings, surface baseline readiness on Application Prep,
   clarify the match-left-to-right swap flow, show saved draft history, and
   make disabled states explain the missing setup/status requirement.
7. Done for the local export pivot: verify the synced baseline is current,
   apply replacements in memory, render PDF bytes locally, write the PDF under
   `output/resume_variants/`, persist generated variant metadata, and expose a
   guarded download endpoint.
8. Done: add Application Prep variant history with download actions.
9. Done: update `README.md` with operation, recovery, privacy, dependency, and
   artifact-location guidance.
10. Stabilization cleanup: remove stale manual-copy UI code, update plan/README
    wording to match the swap-and-generate flow, keep generated PDFs out of the
    repo root, and run a targeted syntax/test pass.

## Stabilization Cleanup Tasks

- [x] Update this plan so it reflects the current service-account, local PDF,
  swap-board workflow instead of the older manual-copy/Drive-copy phases.
- [x] Remove dead frontend helpers from the old suggested-bullet copy flow.
- [x] Ensure generated resume PDFs are ignored outside `output/resume_variants/`
  so UAT artifacts are not accidentally committed.
- [x] Run a targeted frontend syntax check after cleanup.
- [x] Run the resume variant/parser regression tests if code cleanup touches
  generation, parsing, or API paths.

## Application Prep UX/UI Cleanup Plan

Goal: make the page read as a fast, guided application workflow: understand
the job fit, match resume bullet swaps, review changes, and generate a PDF.

- [x] Combine selected job context, prep readiness, and baseline status into a
  compact summary area so users do not pass through several operational cards
  before reaching the swap workflow.
- [x] Rename vague or stale labels: **Deck** to **Jobs**, **Auto-refreshing** to
  **Updating statuses**, **Saved drafts** to **Generated PDFs**, and **Load swap
  board** to **Start resume swap review**.
- [x] Reduce judgmental wording by replacing broad "lower-value" language with
  "less targeted for this job" where it appears in user-facing copy.
- [x] Add a lightweight step indicator for Review fit -> Match swaps -> Review
  changes -> Generate PDF.
- [x] Move generated PDF history below the active matching/review workflow so
  it does not interrupt first-time completion.
- [x] Improve pairing feedback so selecting one side gives a clear next
  instruction and completed pairs feel intentional.
- [x] Keep the planned swaps tray prominent during matching, especially on long
  boards and small screens.
- [x] Keep regenerate/re-run actions visually secondary and make their risk
  clearer when swaps may already exist.
- [x] Hide operational job/prep metadata behind a compact info disclosure so
  the selected job pane stays focused on the resume workflow.
- [x] Re-run the frontend syntax check after each UI cleanup slice.

## Verification

- Unit-test the parser with duplicate bullet text, Unicode, empty bullets, and
  unsupported structures.
- Unit-test value ranking and swap selection, including redundant bullets,
  high-value bullets that must not be replaced, weak proposed replacements, and
  keyword-only recommendations that must be rejected.
- Unit-test anchor re-resolution and descending-index update generation.
- API-test authorization failure, expired credentials, source changed after
  preview, missing/ambiguous targets, export failure, and safe download path
  validation using a fake Google client.
- UI-test the complete keyboard path: choose a left bullet, choose a right
  bullet, review the created pair, remove it, recreate it, confirm export, and
  reach the download action. Test focus restoration, live status announcements,
  small-screen stacking, loading states, and all recovery actions.
- Integration-test one small fixture document: baseline remains byte-for-byte
  unchanged; copied document contains only approved replacements; the PDF is
  readable and stored in the expected job directory.
- Visually inspect exported PDFs for the baseline's actual formatting before
  enabling broad use.
- Test that stale variants remain downloadable but cannot be silently reused.

## Known Risks and Required Decisions

- The Google Docs API exposes structured body content, but nonstandard resume
  layouts may use tables or other structures. The first release must detect and
  reject unsupported target bullets rather than risk formatting damage.
- Google service-account keys are sensitive credentials. Keep them encrypted at
  rest, support disconnect/delete locally, and rotate the key in Google Cloud if
  it is exposed.
- A single source bullet can support multiple suggested drafts. The UI must
  prevent two selected replacements from targeting the same baseline bullet.
- "Low value" is job-specific, not a claim that a bullet is universally weak.
  The explanation must distinguish poor alignment for this posting from weak
  experience, and the user must be able to keep every existing bullet.
- Relevance scoring can over-optimize for keywords and remove differentiating
  experience. Require a visible rationale and preserve role coverage, outcomes,
  seniority signals, and a configurable minimum diversity of evidence across
  employers or projects.
- Exported PDF layout can change as Google renders the copied document. A
  successful API response is not sufficient validation for a one-page resume.
- An AI draft can be grounded yet still be poorly worded or misleading in
  context. The user's approval remains the final safety gate.
- Decide whether generated copied Docs remain in the user's Google Drive forever
  or whether the app should offer an explicit, user-confirmed cleanup action.

## Dependencies

The Google implementation uses pinned `google-api-python-client`, `google-auth`,
and `cryptography` packages, with pins kept in both `requirements.txt` and
`pyproject.toml`.
