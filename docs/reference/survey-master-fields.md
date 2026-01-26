# Survey Master: Writable Fields Reference

This file is auto-generated from the Survey Master mapping CSV (`surveys/qualtrics_api_key_mapping.csv`).
In the standalone repo, treat it as a snapshot; if you override the mapping, update this reference accordingly.

**Auto-generated:** 2026-01-10
**Status:** MVP complete with 66+ writable fields

---

## Overview

This document lists all fields that can be edited via the survey master CSV workflow.
Fields are organized by endpoint and include type information and validation rules.
Datetime fields use ISO 8601 (e.g., `2026-01-10` or `2026-01-10T14:00:00Z`).

### Key Symbols
- 🔴 **DANGEROUS:** Requires `--allow-dangerous` flag to apply
- 📝 **METADATA:** Requires publish after changes
- ⚙️ **OPTIONS:** Requires publish after changes
- 🔄 **STATUS:** Does not require publish
- 🔒 **NULLABLE:** Can be set to empty/null

---

## Metadata Fields (metadata endpoint)

These fields are written via `PUT /survey-definitions/{surveyId}/metadata` and require a survey publish afterward.


| Field Name | Type | Allowed Values | Dangerous | Notes |
|---|---|---|---|---|
| SurveyDescription | string | - | No | Survey description (nullable) |
| SurveyExpirationDate | datetime | string | No | Expiration date (nullable); ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ) |
| SurveyLanguage | string | - | No | Base language code |
| SurveyName | string | - | No | Survey name |
| SurveyStartDate | datetime | string | No | Start date (nullable); ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ) |
| SurveyStatus | string | Active,  Inactive | 🔴 Yes | Survey status |

---

## Options Fields (options endpoint)

These fields are written via `PUT /survey-definitions/{surveyId}/options` using GET→merge→PUT semantics and require a survey publish afterward.


| Field Name | Type | Allowed Values | Dangerous | Notes |
|---|---|---|---|---|
| AnonymizeResponse | string | Yes,  No | No | Anonymize responses setting |
| AutoCloseSurvey | bool | - | No | Auto close survey toggle |
| AutoConfirmStart | bool | - | No | Auto confirm start toggle |
| AvailableLanguages | object(map) | - | No | Available languages map |
| BackButton | string | true,  false | No | Show back button |
| BallotBoxStuffingPreventionURL | string(url) | - | 🔴 Yes | Redirect URL (nullable); nullable |
| CollectGeoLocation | string | true,  false | No | Collect geolocation toggle |
| ConfirmStart | bool | - | No | Confirm start toggle |
| CustomStyles | object | - | No | Custom styles object |
| EOSMessage | string | - | No | End-of-survey message (nullable); nullable |
| EOSMessageLibrary | string | - | No | End-of-survey message library id (nullable); nullable |
| EOSRedirectURL | string(url) | - | 🔴 Yes | End-of-survey redirect URL (nullable); nullable |
| EmailThankYou | string | true,  false | No | Email thank-you toggle |
| Footer | string | - | No | Survey footer HTML |
| Header | string | - | No | Survey header HTML |
| InactiveMessage | string | - | No | Inactive message (nullable); nullable |
| InactiveMessageLibrary | string | - | No | Inactive message library id (nullable); nullable |
| InactiveSurvey | string | - | No | Inactive survey behavior (e.g., 'DefaultMessage') |
| NewScoring | int | - | No | New scoring flag |
| NextButton | string | - | No | Next button label |
| NoIndex | string | Yes,  No | No | Robots noindex setting |
| PartialData | string | - | No | Partial data retention (e.g., '+4 hour') |
| PartialDataCloseAfter | string | - | No | When to close partial data (e.g., 'SurveyStart') |
| PartialDeletion | string | - | No | Partial deletion behavior (nullable); nullable |
| PasswordProtection | string | Yes,  No | 🔴 Yes | Password protection toggle |
| PreviousButton | string | - | No | Previous button label |
| ProgressBarDisplay | string | None,  Text,  Full | No | Progress bar display mode |
| RecaptchaV3 | string | true,  false | No | Recaptcha V3 enabled |
| RefererCheck | string | Yes,  No | No | Referrer check toggle |
| RefererURL | string(url) | - | 🔴 Yes | Allowed referrer URL |
| RelevantID | string | true,  false | No | RelevantID enabled |
| RelevantIDLockoutPeriod | string | - | No | RelevantID lockout period (e.g., '+30 days') |
| ResponseSummary | string | Yes,  No | No | Response summary toggle |
| SaveAndContinue | string | true,  false | No | Save-and-continue toggle |
| SecureResponseFiles | string | true,  false | No | Secure response files toggle |
| ShowExportTags | string | true,  false | No | Include export tags |
| Skin | object | - | No | Skin/theme object |
| SkinLibrary | string | - | No | Skin library name |
| SkinType | string | - | No | Skin type |
| SurveyExpiration | string | - | No | Survey expiration mode (e.g., 'None') |
| SurveyExpirationDate | datetime | string | No | Expiration date (nullable); ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ); nullable |
| SurveyLanguage | string | - | No | Survey language (duplicated here; also in metadata) |
| SurveyLinkCompletedMessage | string | - | No | Survey link completed message (nullable); nullable |
| SurveyLinkCompletedMessageLibrary | string | - | No | Survey link completed message library id (nullable); nullable |
| SurveyMetaDescription | string | - | No | Survey meta description |
| SurveyName | string | - | No | Survey name (duplicated here; also in metadata) |
| SurveyProtection | string | PublicSurvey,  ByInvitation,  PasswordProtected | No | Survey protection mode |
| SurveyStartDate | datetime | string | No | Start date (nullable); ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ); nullable |
| SurveyTermination | string | - | No | Termination behavior (e.g., 'DefaultMessage') |
| SurveyTitle | string | - | No | Survey title |
| ThankYouEmailMessage | string | - | No | Thank you email message (nullable); nullable |
| ThankYouEmailMessageLibrary | string | - | No | Thank you email message library id (nullable); nullable |
| UseCustomSurveyLinkCompletedMessage | string | - | No | Use custom link completed message (nullable); nullable |
| ValidateMessage | string | true,  false | No | Validate message toggle |
| ValidationMessage | string | - | No | Validation message (nullable); nullable |
| ValidationMessageLibrary | string | - | No | Validation message library id (nullable); nullable |
| brandingId | string | - | No | Branding id |
| customCSS | string | - | No | Custom CSS |
| overrides | object | - | No | Overrides (nullable); nullable |
| templateId | string | - | No | Template id (e.g., '*base') |

---

## Status Fields (status endpoint)

These fields are written via `PUT /surveys/{surveyId}` and do **not** require a survey publish afterward.


| Field Name | Type | Allowed Values | Dangerous | Notes |
|---|---|---|---|---|
| isActive | bool | - | 🔴 Yes | Active state (activation separate from publishing) |

---

## Dangerous Fields Policy

The following fields require the `--allow-dangerous` flag to modify:

1. **BallotBoxStuffingPreventionURL**
2. **EOSRedirectURL**
3. **PasswordProtection**
4. **RefererURL**
5. **SurveyStatus**
6. **isActive**

### Risk Mitigation
- Preview will highlight dangerous changes with ⚠️ prefix
- Apply will refuse dangerous changes unless `--allow-dangerous` is provided
- Dangerous changes should typically be applied one survey at a time

---

## Workflow: Pull → Edit → Preview → Apply

### 1. Pull Master Data
```bash
qsync survey master pull
```

### 2. Edit CSV
Edit `surveys/qualtrics_master.csv` with your spreadsheet application.

### 3. Preview Changes
```bash
qsync survey master preview --detail
```

### 4. Apply Changes
```bash
qsync survey master apply [--allow-dangerous] [--force]
```

---

**For more details, see:** [Survey Master Workflow Guide](survey_master_workflow.md)
**Schema reference:** [Survey Master Mapping Schema](survey_master_mapping_schema.md)
