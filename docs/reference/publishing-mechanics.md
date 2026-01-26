# Qualtrics API Publishing Mechanics (Validated)

_Last updated: 2025-12-18_

_Migrated from `appendices/qualtrics_api_publishing_mechanics.md` (monorepo) so the standalone `qsync` repo can be self-contained._

This document describes **what “publish” and “activate” actually do** for Qualtrics surveys edited via the API, based on smoke tests run in a real Qualtrics tenant.

## Context: where this information comes from

Validated via direct API calls made on **December 18, 2025** against:

- **Datacenter / host:** `vuamsterdam.eu.qualtrics.com` (the value used for `QUALTRICS_BASE_URL`)
- **Survey:** `ZZZ_qsync_smoketest_20251213_160439_edited_api` (`SV_5BeVXRVDCgJCsPI`)
- **Question used for “content change” experiments:** `QID7`
  (The temporary exploration scripts used during discovery are intentionally not part of the package.)

## Glossary

- **Publish (survey definition):** Create a *published* survey-definition version so staged definition edits become the current “live” definition for new respondents.
- **Activate (response collection):** Allow the survey to accept responses (`isActive=true`).
- **VersionNumber vs VersionID:** Version history records both; in our environment, fetching a specific version definition requires the **VersionID** (not the VersionNumber).

## 1) Publishing: making staged definition edits live

### Endpoint (validated)

- `POST /API/v3/survey-definitions/{surveyId}/versions`  
  [Implemented in qsync: `qsync survey publish` and auto-publish after `qsync push` / `qsync js push` / `qsync survey push-question` (use `--no-publish` to skip).]

### Request body (validated)

The request body keys are **title-case** (case-sensitive):

```json
{
  "Description": "Human-readable label shown in Version History (max 140 chars)",
  "Published": true
}
```

Project requirement (not yet stress-tested against the API): keep `Description` **≤ 140 characters**.  
[Implemented in qsync: descriptions longer than 140 chars are rejected before calling the API.]

Observed failure mode (validated):

- Lowercase `{"description": "...", "published": true}` returned **400** with an error indicating required fields were missing (“Description, Published”).

### Publish does NOT activate the survey (validated)

Calling the publish endpoint did **not** flip `isActive` from `false` → `true` for the smoke-test survey. Activation must be done separately (see section 2).

### When does publish create a new version entry? (validated)

We ran three experiments to test whether publish creates a new version or reuses the latest one:

1) **No content changes + new Description**  
   - Result: **no new VersionID/VersionNumber** was created.  
   - The existing latest version was reused; its `description` was overwritten and `publishEvents` appended.

2) **A real content change present + publish with the SAME Description**  
   - Result: **a new version was created** (VersionNumber incremented; new VersionID).  
   - The Description string does **not** need to be unique.

3) **A real content change present + publish with a NEW Description**  
   - Result: **a new version was created** (VersionNumber incremented; new VersionID), with the new Description stored on that version.

Practical takeaway (validated): **version creation is driven by whether there are staged survey-definition changes**, not by whether the Description is “new”.

### Version metadata fields (validated)

From `GET /survey-definitions/{surveyId}/versions`, each element has `metadata` containing (at least):

- `versionNumber`, `versionID`, `creationDate`, `userID`, `description`
- `published`: `true` only for the **currently published** version
- `wasPublished`: `true` for versions that were published at least once (even if not currently published)
- `publishEvents`: list of publish events (each with at least a timestamp + user ID)

## 2) Activation: starting/stopping response collection

### Endpoint + payload (validated)

- `PUT /API/v3/surveys/{surveyId}` with body:

```json
{ "isActive": true }
```

and to deactivate:

```json
{ "isActive": false }
```

This successfully flipped `isActive` as observed via `GET /API/v3/surveys/{surveyId}`.  
(Not yet implemented in qsync.)

## 3) Version inspection: listing + fetching a version definition

### List versions (validated)

- `GET /API/v3/survey-definitions/{surveyId}/versions`

Returns `result.elements[]` with `metadata` per version (see section 1).

### Fetch a specific version definition (validated)

- `GET /API/v3/survey-definitions/{surveyId}/versions/{versionId}`

This returned a **full survey definition payload** in `result` (including `SurveyID`/`Questions`).

Observed mismatch (validated):

- `GET /.../versions/{versionNumber}` returned **404** in our environment; use **VersionID** instead.

## 4) Rollback mechanics (what works so far)

### Full survey-definition rollback via PUT (NOT validated / failed)

We attempted to “rollback by cloning” an older version definition by `PUT`-ing it back to the live survey definition endpoint:

- Attempted: `PUT /API/v3/survey-definitions/{surveyId}` with the fetched version `result` as the JSON body  
- Observed: **404 Not Found** (“The requested resource does not exist.”)

So, **full-survey rollback via full-definition PUT is not currently confirmed** in this environment.

### Question-level rollback (validated; aligns with qsync scope)

Because qsync’s current write surface is primarily **question payloads**, we validated a rollback strategy at the question level:

1) Fetch a historical version definition: `GET /survey-definitions/{surveyId}/versions/{versionId}`
2) Extract a question payload: `definition["Questions"][questionId]`
3) Restore it to current: `PUT /survey-definitions/{surveyId}/questions/{questionId}`  
   [Implemented in qsync (as a write primitive): `qsync push`, `qsync js push`, and `qsync survey push-question` all ultimately update question definitions.]
4) Publish: `POST /survey-definitions/{surveyId}/versions` with `Published=true`

We successfully rolled `QID7` “back” to an older version’s QuestionText and then “forward” again by repeating the process with a newer version.

## Not yet verified / open questions

- Whether Qualtrics supports **creating an unpublished snapshot version** (e.g., `Published: false`) and what effect that has on VersionHistory.
- Whether any of these work (or are recommended) for activation/status changes in this environment:
  - `PUT /survey-definitions/{surveyId}/metadata` with `isActive`
  - `PUT /survey-definitions/{surveyId}/metadata` with `SurveyStatus`
- Why `PUT /survey-definitions/{surveyId}` returned **404** in our test (endpoint removed vs permissions vs payload shape).
- Whether there is an official, supported **full-survey rollback/restore** mechanism without per-question restores.
- Timing / eventual-consistency: how long it takes between publish and respondents seeing the new version.
- Respondent impact details (in-progress sessions, preview links, etc.) under API-driven publish events.
- Rate limits and retry/backoff behavior for version publishing + activation endpoints.
