# QSF Format vs JSON Definition Format

## Overview
When exporting a survey from Qualtrics via the **QSF file format** (for backup/import), it uses a very different structure than the **JSON Definition** returned by the `/API/v3/survey-definitions/{id}` endpoint.

### Key Finding
- **JSON Definition** = API representation (flattened, direct keys)
- **QSF Format** = Portable format (nested SurveyEntry + SurveyElements array)

### API Support (as of 2026-02-14)

**QSF-shaped JSON via API**: ✅ **Supported**
- `GET /survey-definitions/{surveyId}?format=qsf` returns a **QSF-shaped JSON payload**
  (`SurveyEntry` + `SurveyElements`) suitable for programmatic copy/import workflows.
- `GET /survey-definitions/{surveyId}/versions/{versionId}?format=qsf` returns the same shape for a specific version.
- This is not a “download a `.qsf` file” endpoint, but you can save the JSON to disk as `*.qsf` and re-import it.

**QSF Import via API**: ✅ **Supported**
- `POST /surveys` with `multipart/form-data` and mime type `application/vnd.qualtrics.survey.qsf`
- Used by `qsync survey copy` and `qsync survey copy-cross-account`

**Notes**
- The QSF-shaped JSON returned by `format=qsf` may not be byte-identical to the UI-exported `.qsf`,
  but it preserves the same conceptual structure and is sufficient for qsync’s import workflows.

---

## Structure Comparison

### JSON Definition (API Format)
**Source**: `GET /API/v3/survey-definitions/{survey_id}`

**Top-level keys** (direct):
```
QuestionCount (str)
SurveyID (str)
SurveyName (str)
SurveyStatus (str)
LastModified (str)
LastAccessed (NoneType/str)
CreatorID (str)
LastActivated (str)
BrandID (str)
OwnerID (str)
DivisionID (str)
BrandBaseURL (str)
```

**Top-level objects**:
```
SurveyOptions (dict, 27 keys)
Questions (dict)
Blocks (dict)
ResponseSets (dict)
SurveyFlow (dict)
Scoring (dict)
ProjectInfo (dict)
```

### QSF Format (Portable Format)
**Source**: Manual QSF export or import file

**Top-level keys** (only 2):
```
SurveyEntry (dict)
SurveyElements (array)
```

**SurveyEntry structure**:
```
SurveyID
SurveyName
SurveyDescription
SurveyOwnerID
SurveyBrandID
DivisionID
SurveyLanguage
SurveyActiveResponseSet
SurveyStatus
SurveyStartDate
SurveyExpirationDate
SurveyCreationDate
CreatorID
LastModified
LastAccessed
LastActivated
Deleted
```

**SurveyElements structure** (array of element objects):

Each element has this structure:
```
{
  "SurveyID": "SV_...",
  "Element": "[TYPE]",
  "PrimaryAttribute": "...",
  "SecondaryAttribute": "...",
  "TertiaryAttribute": "...",
  "Payload": {...}
}
```

**Element types found** (10 total in payout survey):
- `BL` - Blocks
- `FL` - Flow
- `PL` - Preview/Looks
- `PROJ` - Project
- `QC` - Question Count
- `RS` - Response Sets
- `SCO` - Scoring
- `SO` - Survey Options
- `SQ` - Survey Questions
- `STAT` - Statistics/Metadata

---

## Mapping from JSON Definition to QSF Format

### Metadata Mapping

| JSON Key | QSF Location | Notes |
|----------|-------------|-------|
| `SurveyID` | `SurveyEntry.SurveyID` | Direct copy |
| `SurveyName` | `SurveyEntry.SurveyName` | Direct copy |
| `SurveyStatus` | `SurveyEntry.SurveyStatus` | Direct copy |
| `CreatorID` | `SurveyEntry.CreatorID` | Direct copy |
| `LastModified` | `SurveyEntry.LastModified` | Direct copy |
| `LastAccessed` | `SurveyEntry.LastAccessed` | Direct copy |
| `LastActivated` | `SurveyEntry.LastActivated` | Direct copy |
| `OwnerID` | `SurveyEntry.SurveyOwnerID` | Renamed |
| `BrandID` | `SurveyEntry.SurveyBrandID` | Renamed |
| `DivisionID` | `SurveyEntry.DivisionID` | Direct copy |
| `BrandBaseURL` | `SurveyEntry.?` | Not found in test |
| `QuestionCount` | `?` | Not directly in SurveyEntry |

### Content Mapping

| JSON Key | QSF Element Type | Notes |
|----------|------------------|-------|
| `Questions` (dict) | `SQ` element(s) | One `SQ` element per question |
| `Blocks` (dict) | `BL` element | Single element with all blocks in Payload |
| `ResponseSets` (dict) | `RS` element | Single element with response sets |
| `SurveyFlow` (dict) | `FL` element | Flow logic and branching |
| `SurveyOptions` (dict) | `SO` element | Survey settings/options |
| `Scoring` (dict) | `SCO` element | Scoring configuration |
| `ProjectInfo` (dict) | `PROJ` element | Project category, schema version |
| `QuestionCount` (str) | `QC` element | Question count metadata |
| (metadata) | `STAT` element | Mobile compatibility, ID |
| (metadata) | `PL` element | Preview/looks configuration |

---

## Transformation Algorithm

### To Convert JSON Definition → QSF Format:

```python
1. Create SurveyEntry dict:
   - Copy direct metadata fields from JSON definition
   - Rename BrandID → SurveyBrandID, OwnerID → SurveyOwnerID
   - Set SurveyLanguage (default: "EN")
   - Set SurveyActiveResponseSet (from ResponseSets)
   - Add timestamps (SurveyStartDate, SurveyExpirationDate, SurveyCreationDate)

2. Create SurveyElements array with one element per type:
   - BL element: Payload contains Blocks dict
   - FL element: Payload contains SurveyFlow dict
   - PL element: Payload contains PreviewType, PreviewID
   - PROJ element: Payload contains ProjectInfo + SchemaVersion
   - QC element: Payload is null or contains QuestionCount
   - RS element: Payload contains ResponseSets dict
   - SCO element: Payload contains Scoring dict
   - SO element: Payload contains SurveyOptions dict
   - SQ element: One element per question (Payload = question object)
   - STAT element: Payload contains MobileCompatible, ID

3. For each SurveyElement, set:
   - SurveyID from JSON definition
   - Element = type code
   - PrimaryAttribute = semantic name
   - SecondaryAttribute = optional detail
   - TertiaryAttribute = optional detail
   - Payload = content for that element type
```

---

## Reverse Transformation

### To Convert QSF Format → JSON Definition:

```python
1. Start with SurveyEntry as base for metadata:
   - SurveyID, SurveyName, SurveyStatus, CreatorID, etc.
   - Rename SurveyBrandID → BrandID, SurveyOwnerID → OwnerID

2. Extract content from SurveyElements array:
   - BL element Payload → Blocks
   - FL element Payload → SurveyFlow
   - PROJ element Payload → ProjectInfo
   - RS element Payload → ResponseSets
   - SCO element Payload → Scoring
   - SO element Payload → SurveyOptions
   - All SQ elements Payload → aggregate into Questions dict
   
3. Add calculated fields:
   - QuestionCount = len(Questions) or from QC element
   - BrandBaseURL (may need to construct from BrandID)
```

---

## Implementation Notes

### Why This Matters
- The **JSON Definition API** returns a flat structure suitable for programmatic manipulation
- The **QSF Format** wraps everything in `SurveyEntry` + `SurveyElements` structure for portable backup/import
- To **copy a survey programmatically**, we have two approaches:
  1. **Template-based** (recommended): Use a downloaded QSF as template, update the name
  2. **Programmatic** (complex): Transform JSON definition → QSF format (requires careful field mapping)

### Current Implementation (Template-Based)
The `qsync survey create` and `qsync survey copy` commands use the template/QSF-based approach:
1. Fetch the source survey definition in **QSF format** from the API, load a local QSF via `--from-qsf`, or use the bundled minimal QSF seed for `survey create`
2. Update the name/language fields and clear the `SurveyID`
3. POST the QSF to `/API/v3/surveys` as a multipart upload (with `name` in form data)

The legacy script entry point that previously implemented similar logic is now archived under `archive/scripts/copy_survey.py`.

**Why this works better:**
- The QSF format has many subtle fields and edge cases that vary per survey
- Some fields (like PreviewID, SchemaVersion, skin customizations) can't be reliably inferred from JSON
- Using an actual downloaded QSF preserves all original survey configuration exactly

### Why Direct Conversion Fails
Attempting to POST the raw JSON Definition (or converted QSF) fails because:
- Qualtrics API expects specific field structures in QSF that the JSON Definition doesn't fully represent
- Some QSF fields (PreviewID, exact SchemaVersion) are not stored in the JSON Definition endpoint
- SurveyOptions payload has additional metadata (SurveyName, ActiveResponseSet, etc.) not in definition
- Block ordering and nested structures may differ between formats
- Read-only fields or calculated fields cause validation errors

### Future Work
See: `src/qsync/qsf_converter.py` for bidirectional conversion functions if needed for other use cases:
- `json_definition_to_qsf(definition: dict) -> dict` - JSON → QSF
- `qsf_to_json_definition(qsf: dict) -> dict` - QSF → JSON
- These are useful for analysis/transformation but not recommended for survey copying

### Field Mapping Reference

For reference, here's how fields map between formats (even if not used for copying):

| JSON Key | QSF Location | Special Notes |
|----------|-------------|---------------|
| `SurveyID` | `SurveyEntry.SurveyID` | Direct copy |
| `SurveyName` | `SurveyEntry.SurveyName` + `SurveyElements.SO.Payload.SurveyName` | Must update in multiple places |
| `SurveyStatus` | `SurveyEntry.SurveyStatus` | Direct copy |
| `CreatorID` | `SurveyEntry.CreatorID` | Direct copy |
| `LastModified` | `SurveyEntry.LastModified` | Timestamp format: "YYYY-MM-DD HH:MM:SS" |
| `BrandID` | `SurveyEntry.SurveyBrandID` | Renamed field |
| `OwnerID` | `SurveyEntry.SurveyOwnerID` | Renamed field |
| `Blocks` | `SurveyElements.BL.Payload` | Array of block objects |
| `Questions` | `SurveyElements.SQ.Payload` | One element per question |
| `SurveyFlow` | `SurveyElements.FL.Payload` | Contains flow logic |
| `SurveyOptions` | `SurveyElements.SO.Payload` | Contains survey settings, duplicates some metadata |
| `Scoring` | `SurveyElements.SCO.Payload` | Scoring configuration |
| `ProjectInfo` | `SurveyElements.PROJ.Payload` | Project category, schema version |
| `ResponseSets` | `SurveyElements.RS.Payload` | Usually `null` in export, full dict in definition |
