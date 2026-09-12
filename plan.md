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
- The Application Prep UI currently presents the drafts for manual copying.
- The app does not connect to Google, create document copies, edit resumes,
  export PDFs, or persist generated resume files.
- The future Google Doc baseline and current `resume.txt` are separate sources.
  Before swap recommendations are trusted, the app must verify that they are
  the same resume revision or make the synced Google Doc snapshot the canonical
  Application Prep source.
- `job_application_preps` represents AI-generation state, not a generated
  resume artifact. Resume variants need their own persisted records.
- Before building this feature, reconcile the existing Application Prep field
  contract: the prompt currently requests `source_resume_evidence`, the
  validator requires `evidence_sources`, and the UI reads
  `source_resume_evidence`. One canonical schema must be used end to end.

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
   copied Google Doc, and exported PDF.
7. Treat a baseline change as a reason to review or regenerate a variant, not
   as permission to silently overwrite it.

## User Flow

1. The user opens Application Prep and connects their Google account.
2. The user selects one Google Docs baseline resume. The app reads and
   snapshots its editable body bullets.
3. For a selected job, the app compares each existing baseline bullet with the
   job description and identifies a small set of lower-value swap candidates,
   explaining weaker job alignment or redundant signal.
4. For each candidate, the app proposes a grounded replacement draft with its
   source evidence, matching job requirement, and confidence. The page shows
   the original and proposed replacement side by side.
5. The user clicks **Create tailored resume** and confirms the summary.
6. The app copies the baseline Doc, applies the approved replacements to the
   copy, exports it to PDF, and stores the PDF locally.
7. The Application Prep job card shows the variant status plus **Download PDF**
   and **Open Google Doc** actions.

The minimum viable release supports replacement of existing normal body-list
bullets only. Adding bullets, editing tables, headers/footers, text boxes, or
multi-column layouts is explicitly out of scope until the replacement path is
proven safe.

## Architecture

### Google integration

Use a server-side Google OAuth flow, configured through environment variables:

- Google OAuth client ID and client secret.
- Exact authorized callback URL.
- An application secret for protected local credential storage, if credentials
  must persist across dashboard restarts.

Request the smallest scopes that support the selected document. Use a picker or
explicit document selection rather than broad Drive search. Do not commit client
secrets, refresh tokens, or downloaded Google credential files.

For a user-triggered export, a session-scoped credential is the safer initial
option. Persist a refresh token only if background regeneration is a product
requirement, and only after implementing encrypted at-rest storage and a
disconnect/revoke action.

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
5. At creation time, copy the baseline with Drive.
6. Re-read the copied document and re-resolve each selected anchor by exact
   text plus section context. Abort with a useful error if an anchor is absent
   or ambiguous.
7. Apply replacements with one Docs `batchUpdate`, in descending document-index
   order. Delete only the original bullet text and preserve its paragraph
   newline; then insert the replacement text at the same position.
8. Export the copied Google Doc as `application/pdf` and write it to the local
   artifact directory.
9. Record the successful variant only after the PDF bytes are written and
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
output/application_resumes/<job-id>/<safe-company>-<safe-title>-<date>.pdf
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
| `copied_document_id`, `copied_document_url` | Traceable Google Docs copy. |
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
1. Match bullet swaps  ->  2. Review changes  ->  3. Create PDF
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
sequence: **Copying baseline**, **Applying 3 approved swaps**, **Exporting PDF**,
and **Saving your resume**. Disable duplicate submission but preserve the
reviewed pairs in the draft record.

On success, show a completion card at the top of the job detail:

- **Tailored resume ready** and the file name.
- Primary action: **Download PDF**.
- Secondary action: **Open Google Doc copy**.
- Tertiary action: **View changes** or **Create another version**.

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

- `POST /api/resume-source/google/connect`
- `GET /api/resume-source/google/callback`
- `POST /api/resume-source/sync`
- `POST /api/application-prep/jobs/<job_pk>/resume-variants`
- `GET /api/application-prep/jobs/<job_pk>/resume-variants`
- `GET /api/resume-variants/<variant_id>/download`

The create request includes selected replacements plus the expected baseline
version/hash and prep hash. The server rejects stale input rather than creating
a resume from unseen source changes.

## Implementation Sequence

1. Repair and test the Application Prep JSON contract. Reconcile `resume.txt`
   with the Google Doc baseline, and finish the optional responsibilities-
   inventory work before relying on its evidence in variants.
2. Add the additive models and migrations for source settings and variants.
3. Add Google OAuth configuration, connect/disconnect, and baseline document
   selection/snapshot. Document all configuration in `README.md`.
4. Implement the conservative bullet parser and anchor resolution, with no
   writes to Google yet.
5. Add baseline-bullet relevance evaluation, swap recommendations, and a review
   UI. Validate that each selected replacement targets one eligible, current
   baseline bullet and is a job-specific improvement over it.
6. Implement copy, guarded Docs update, PDF export, secure local write, and
   variant persistence.
7. Add Application Prep variant history and download/open actions.
8. Complete automated and manual verification, then update `README.md` with
   operation, recovery, privacy, and artifact-location guidance.

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
- Google authorization adds secret and token-handling responsibility. A
  session-only credential avoids persistent token storage but requires a new
  connection after a restart; persistent credentials require encryption,
  rotation, and revoke/disconnect behavior.
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

No dependency changes are made by this plan. Implementation will need pinned
Google API/OAuth client packages, selected only after checking their current
security posture and adding matching pins to both `requirements.txt` and
`pyproject.toml`.
