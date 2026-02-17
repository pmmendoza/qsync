# qsync Editing Surfaces (Canonical Overview)

This page maps each edit type to its primary local editing surface and CLI flow.

Use this as the source of truth when deciding where to edit versus where to only inspect.

## Surface matrix

| Edit type | Primary local editing surface | Non-editable cues in that surface | Stage/push commands |
|---|---|---|---|
| Question wording + item labels | `excel/<slug>-<SurveyID>.xlsx` (`Questions`, `Options`, `Subitems`, `SBS_*`) | Light-gray cells for system/read-only columns; `RequiredResponse` is derived/read-only | `qsync items stage` → `qsync items push` |
| Question-level response settings (required/validation) | Same items workbook (`Questions.ForceResponseMode`, `ValidationType`, `ValidationSettingsJSON`) | `RequiredResponse` is derived/read-only; formula/system columns are gray | `qsync items stage` → `qsync items push` |
| Embedded data defaults | Same items workbook (`Embedded_Data.Value`) | `SurveyID`, `FlowID`, `FlowOrder`, `Field`, `Type`, `WrittenByQIDs` are gray/read-only | `qsync items stage` → `qsync items push` |
| Survey metadata/options/status (bulk) | `surveys/qualtrics_master.xlsx` (preferred) or `surveys/qualtrics_master.csv` | Workbook: columns mapped as `survey_master=read` are gray/read-only | `qsync survey master stage` → `qsync survey master push` |
| Question JS | `survey_js/core/*.js` | N/A (code surface) | `qsync js stage` → `qsync js push` |
| Flow/routing | `surveys/flow/<SurveyID>/flow.yaml` | N/A (YAML surface) | `qsync flow stage` → `qsync flow push` |
| Translations (non-base) | `excel/<slug>-<SurveyID>.xlsx` language columns (`Text_<lang>_MD`, `Label_<lang>_MD`) | System/context columns remain read-only | `qsync translations stage` → `qsync translations push` |
| EOS library messages | `contents/qualtrics_library_messages/<LibraryID>/<MessageID>` | N/A (text file surface) | `qsync eos stage` → `qsync eos push` |

## Notes

- `qsync survey prepare` can hydrate these local surfaces (pull-only).
- If multiple surfaces exist for the same dimension (for example Survey Master workbook + CSV), qsync loads the most recently modified one.
- Cached survey JSON files under `surveys/*.json` are baseline/reference inputs, not primary authoring surfaces.
