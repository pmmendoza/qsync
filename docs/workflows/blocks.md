# Blocks workflow (Block-internal question/page-break order)

This workflow manages `SurveyDefinition.Blocks[*].BlockElements` via a local YAML editing surface.

Ownership boundary:
- `blocks` owns question/page-break ordering **inside** blocks.
- `flow` owns routing/branching and block traversal graph.

## Local surfaces

After pull, qsync writes:

```
surveys/flow/<survey-slug>-<survey-id>/
  blocks.yaml
  blocks_baseline.json
```

`blocks.yaml` is the editable surface.

## Runbook

1. Pull
- `qsync blocks pull --survey-id SV_xxx`

2. Edit locally
- Edit `blocks.yaml` directly, or use helper commands:
- `qsync blocks move-qid --survey-id SV_xxx --question-id QID64 --target-block-id BL_xxx --after-qid QID70`
- `qsync blocks add-page-break --survey-id SV_xxx --target-block-id BL_xxx --insert-index 3`
- `qsync blocks remove-page-break --survey-id SV_xxx --target-block-id BL_xxx --element-index 4`
- `qsync blocks remove-qid --survey-id SV_xxx --question-id QID80`

3. Preview
- `qsync blocks preview --survey-id SV_xxx --detailed`

4. Stage
- `qsync blocks stage --survey-id SV_xxx`

5. Push
- `qsync blocks push --survey-id SV_xxx --yes`

## Notes

- `pull` will not overwrite local edits unless you pass `--force`.
- Stage validates that only block-element structure changed (not non-element block metadata).
- Push uses standard safeguards (responses/live checks) and supports `--force-live`, `--force-preview`, and `--no-publish`.
- `qsync survey move-question/remove-question/add-page-break/remove-page-break` use the same blocks mutation internals for compatibility.
