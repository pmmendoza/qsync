# Flow workflow (Survey Flow Synchronization)

This document explains how to version-control and synchronize survey branching logic, block ordering, and flow structure between local YAML files and the Qualtrics API.

Account scoping: if you run with `--account <name>` or set a workspace default via `qsync account use <name>`, `qsync` reads/writes the workflow surfaces under `.<name>/` subdirectories (see `../reference/accounts.md`). For flow, that means `surveys/.<name>/flow/{survey_id}/...`. The paths below assume the default account.

## 1. Overview

Survey flow defines the execution path through a survey: which blocks appear, in what order, with what branching conditions, randomization, and embedded data. The flow dimension lets you:

- **Pull** the current flow structure from Qualtrics as human-readable YAML
- **Edit** branching logic, block order, and flow structure locally
- **Preview** changes before pushing (semantic diff)
- **Stage** changes for coordinated deployment
- **Push** changes back to Qualtrics

## 2. File structure

After pulling flow for a survey, you'll have:

```
surveys/flow/{survey_id}/
  flow.yaml       # Editable flow definition (human-readable YAML)
  baseline.json   # Last-pulled API state (for drift detection)
```

- `flow.yaml` is the editing surface - modify this file to change flow
- `baseline.json` tracks what was last pulled from the API (don't edit manually)

## 3. Quick runbook

| Step | Command | Description |
| --- | --- | --- |
| Pull | `qsync flow pull --survey-id SV_xxx` | Downloads flow from Qualtrics for a single survey and saves as YAML |
| Pull (all focal) | `qsync flow pull --all-focal` | Pulls flow for all focal surveys without interactive prompts |
| Preview | `qsync flow preview --survey-id SV_xxx` | Shows semantic diff between YAML and baseline |
| Stage | `qsync flow stage --survey-id SV_xxx` | Stages changes for push |
| Push | `qsync flow push --survey-id SV_xxx --yes` | Pushes staged changes to Qualtrics |

## 4. YAML format

The YAML format is designed for readability while preserving all Qualtrics flow data:

```yaml
version: 1
survey_id: SV_abc123
flow_type: Root
flow:
  # Embedded data - set variables at survey start
  - type: EmbeddedData
    id: FL_1
    fields:
      - field: study_arm
        value: control
        type: Custom

  # Branch - conditional routing
  - type: Branch
    id: FL_2
    description: study_arm EqualTo treatment  # Auto-generated
    raw_logic:  # Preserved Qualtrics format
      Type: BooleanExpression
      "0":
        Type: If
        "0":
          LogicType: EmbeddedField
          LeftOperand: study_arm
          Operator: EqualTo
          RightOperand: treatment
    then:
      - type: Block
        id: BL_treatment
        name: Treatment Questions  # From survey blocks

  # Block - reference to a question block
  - type: Block
    id: BL_main
    flow_id: FL_5
    name: Main Survey Questions

  # Block randomizer - randomize block order
  - type: BlockRandomizer
    id: FL_3
    randomization:
      count: 2
      evenly_present: true
    blocks:
      - type: Block
        id: BL_variant_a
      - type: Block
        id: BL_variant_b

  # End survey
  - type: EndSurvey
    id: FL_4
    options:
      end_type: Redirect
      EOSRedirectURL: https://example.com/complete
```

### Node types

| YAML type | Qualtrics type | Description |
| --- | --- | --- |
| `Block` | Standard/Block | Reference to a question block |
| `Branch` | Branch | Conditional branching |
| `EmbeddedData` | EmbeddedData | Set embedded data fields |
| `BlockRandomizer` | BlockRandomizer | Randomize block presentation |
| `Group` | Group | Logical grouping of flow elements |
| `EndSurvey` | EndSurvey | Survey termination point |
| `WebService` | WebService | API call during survey |

### Branch logic format

Branch conditions are stored as `raw_logic` to preserve the exact Qualtrics structure. A human-readable `description` is auto-generated for reference:

```yaml
- type: Branch
  id: FL_2
  description: Question QID5 Selected  # Human-readable
  raw_logic:  # Exact Qualtrics format - edit this to change logic
    Type: BooleanExpression
    "0":
      Type: If
      "0":
        LogicType: Question
        QuestionID: QID5
        Operator: Selected
        ChoiceLocator: q://QID5/SelectableChoice/1
```

## 5. Making changes

### Reordering blocks

Simply move blocks up/down in the YAML to change their order:

```yaml
flow:
  - type: Block
    id: BL_intro       # Appears first
  - type: Block
    id: BL_main        # Appears second
  - type: Block
    id: BL_outro       # Appears last
```

### Adding embedded data

Add a new `EmbeddedData` node:

```yaml
- type: EmbeddedData
  id: FL_new_ed
  fields:
    - field: tracking_id
      value: "${e://Field/ResponseID}"
      type: Custom
```

### Modifying branch conditions

Edit the `raw_logic` structure. Common patterns:

**EmbeddedField condition:**
```yaml
raw_logic:
  Type: BooleanExpression
  "0":
    Type: If
    "0":
      LogicType: EmbeddedField
      LeftOperand: study_arm
      Operator: EqualTo
      RightOperand: treatment
```

**Question condition:**
```yaml
raw_logic:
  Type: BooleanExpression
  "0":
    Type: If
    "0":
      LogicType: Question
      QuestionID: QID5
      Operator: Selected
      ChoiceLocator: q://QID5/SelectableChoice/1
```

## 6. Preview output

The preview command shows a semantic diff:

```
$ qsync flow preview --survey-id SV_abc123
[sync:flow] 3 change(s) detected:
  + Block [BL_new_module]: Added after FL_3
  ~ Branch [FL_2]: Then branch content changed
  - EmbeddedData [FL_5]: Removed
  ↕ Flow [FL_1]: Moved from position 0 to 2
```

Symbols:
- `+` Added node
- `-` Removed node
- `~` Modified node
- `↕` Reordered node

## 7. Validation

Before push, the flow is validated for:

1. **Structural validity** - correct node types, required fields
2. **Reference validity** - block IDs exist, QIDs in branch logic exist
3. **Semantic validity** - no duplicate IDs, valid randomizer counts

If validation fails, you'll see specific errors:

```
[sync:flow] Validation error:
flow[2]: Block 'BL_nonexistent' does not exist in survey
flow[3].BranchLogic: Question 'QID999' referenced in condition does not exist
```

## 8. Drift detection

If someone edits the flow in the Qualtrics UI after you pulled:

```
$ qsync flow stage --survey-id SV_abc123
[sync:flow] Drift detected: baseline differs from live API
  Run 'qsync flow pull --survey-id SV_abc123' to refresh baseline
  Or use --allow-drift to proceed anyway
```

Use `--allow-drift` to push anyway (your changes will overwrite remote edits).

## 9. Push safeguards

- **Validation required**: Flow must pass validation before push
- **Drift detection**: Warns if remote flow changed since pull
- **Verification**: After push, verifies API state matches what was sent
- **Baseline update**: Updates baseline.json to match pushed state

## 10. Handling deleted questions

If a branch references a question (QID) that has been deleted from the survey, validation will fail:

```
[sync:flow] Validation error:
flow[2].BranchLogic: Question 'QID123' referenced in condition does not exist
```

To fix:
1. Remove or update the branch condition in YAML
2. Or restore the deleted question in Qualtrics

## 11. Integration with `qsync sync`

Flow is integrated with the unified sync workflow:

```bash
$ qsync sync --survey-id SV_abc123
# Shows flow alongside items, JS, translations, EOS
```

## 12. Known limitations

### Deep nesting

Deeply nested structures (e.g., Branch inside Branch inside Group at depth > 3) should work but have not been extensively validated. If you encounter issues with complex nested flows, please report them.

### Less common node types

The following node types are handled via passthrough (preserved as raw JSON but not human-readable):
- `ReferenceSurvey` - Links to other surveys
- `Quota` - Quota tracking in flow
- `SupplementalData` - External data integration

Any unrecognized node type is preserved as `type: Unknown` with full raw JSON, ensuring lossless round-trip even for future Qualtrics features.

## 13. Roundtrip checklist

1. `qsync flow pull --survey-id SV_...` - get current state
2. Edit `surveys/flow/SV_.../flow.yaml` as needed
3. `qsync flow preview --survey-id SV_...` - verify changes
4. `qsync flow stage --survey-id SV_...` - stage for push
5. `qsync flow push --survey-id SV_... --yes` - push to Qualtrics
6. `qsync flow preview --survey-id SV_...` - confirm no changes

Commit `flow.yaml` to Git to track flow changes alongside other survey modifications.
