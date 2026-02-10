"""Excel workbook IO for qsync wording sync.

This module owns the schema and transformations for the qsync workbook used to
preview changes and push wording/options/subitems back to Qualtrics.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.worksheet.formula import ArrayFormula
from openpyxl.styles import Alignment, PatternFill, Font
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import FormulaRule

from .markdown_codec import (
    html_to_md,
    normalize_text,
    should_treat_as_html,
    md_to_html,
    is_markdown_safe_html,
)

QUESTION_SHEET = "Questions"
OPTIONS_SHEET = "Options"
SUBITEMS_SHEET = "Subitems"
EMBEDDED_DATA_SHEET = "Embedded_Data"
SYSTEM_SHEET = "System"
INSTRUCTIONS_SHEET = "Instructions"
TRANSLATION_KEY_SHEET = "TranslationKeyMap"
SURVEY_METADATA_SHEET = "Survey_Metadata"

EMBEDDED_EMPTY_VALUE = "---"
SURVEY_METADATA_KEYS = [
    "SurveyTitle",
    "SurveyDescription",
    "SurveyMetaDescription",
]


def _normalize_language_code(lang: str) -> str:
    raw = str(lang or "").strip().replace("_", "-")
    if not raw:
        return ""
    parts = [part.strip() for part in raw.split("-") if part.strip()]
    return "-".join(part.upper() for part in parts)


def _language_suffix(lang: str) -> str:
    code = _normalize_language_code(lang)
    if not code:
        return ""
    return code.lower().replace("-", "_")


def _language_from_suffix(suffix: str) -> str:
    raw = str(suffix or "").strip()
    if not raw:
        return ""
    return raw.replace("_", "-").upper()


def _lookup_language_block(language_blocks: object, lang_code: str) -> dict:
    if not isinstance(language_blocks, dict):
        return {}
    code = _normalize_language_code(lang_code)
    if not code:
        return {}

    candidates: list[str] = []
    for variant in (code, code.replace("-", "_")):
        candidates.extend([variant, variant.upper(), variant.lower()])

    seen: set[str] = set()
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        block = language_blocks.get(key)
        if isinstance(block, dict):
            return block
    return {}


def _normalize_subitem_field(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Answer"
    lowered = raw.lower()
    if lowered == "label":
        return "Label"
    if lowered == "answer":
        return "Answer"
    return raw


def _normalize_language_list(languages: Sequence[str] | None) -> List[str]:
    cleaned: List[str] = []
    seen: set[str] = set()
    for lang in languages or []:
        code = _normalize_language_code(lang)
        if not code or code in seen:
            continue
        seen.add(code)
        cleaned.append(code)
    return cleaned


def _ordered_languages(
    languages: Sequence[str] | None,
    *,
    base_language: str | None = None,
) -> List[str]:
    ordered = _normalize_language_list(languages)
    if base_language:
        base = _normalize_language_code(base_language)
        if base in ordered:
            ordered = [base] + [lang for lang in ordered if lang != base]
        else:
            ordered = [base] + ordered
    else:
        # Legacy fallback: inject EN at front
        if "EN" in ordered:
            ordered = ["EN"] + [lang for lang in ordered if lang != "EN"]
        else:
            ordered = ["EN"] + ordered
    return ordered


def _translation_columns(
    prefix: str,
    languages: Sequence[str] | None,
    *,
    base_language: str | None = None,
) -> List[str]:
    columns: List[str] = []
    for lang in _ordered_languages(languages, base_language=base_language):
        suffix = _language_suffix(lang)
        if not suffix:
            continue
        columns.append(f"{prefix}_{suffix}_MD")
        columns.append(f"{prefix}_{suffix}_IsHTML")
    return columns


def _metadata_columns(keys: Sequence[str] | None = None) -> List[str]:
    columns: List[str] = []
    for key in keys or SURVEY_METADATA_KEYS:
        columns.append(f"{key}_MD")
        columns.append(f"{key}_IsHTML")
    return columns


def _column_guide(base_language: str = "EN") -> dict:
    """Build the column guide dict, using *base_language* for the base text columns."""
    base_suffix = _language_suffix(base_language) or "en"
    base_upper = _normalize_language_code(base_language) or "EN"
    text_md = f"Text_{base_suffix}_MD"
    text_html = f"Text_{base_suffix}_IsHTML"
    label_md = f"Label_{base_suffix}_MD"
    label_html = f"Label_{base_suffix}_IsHTML"
    return {
        QUESTION_SHEET: [
            ("SurveyID", "System", "Qualtrics Survey ID. Read-only."),
            ("QID", "System", "Qualtrics Question ID. Read-only."),
            ("BlockName", "System", "Qualtrics block name. Read-only."),
            ("QuestionType", "System", "Qualtrics question type (MC, TE, etc.)."),
            ("DataExportTag", "System", "Qualtrics DataExportTag / variable name."),
            (
                "QuestionKey",
                "Editable",
                "Optional human-friendly key for internal tracking.",
            ),
            (text_md, "Editable", f"{base_upper} wording in restricted Markdown."),
            (
                text_html,
                "Flag",
                f"TRUE when {text_md} should be treated as raw HTML.",
            ),
            ("OptionsPreview", "System", "Read-only preview of answer options."),
            (
                "SubitemsPreview",
                "System",
                "Read-only preview of subitems / statements.",
            ),
            ("InPre", "Flag", "TRUE if included in the pre-treatment survey."),
            ("InPost", "Flag", "TRUE if included in the post-treatment survey."),
            (
                "Dirty",
                "System",
                "Auto-flag set by qsync preview/apply when a row has pending pushes.",
            ),
        ],
        OPTIONS_SHEET: [
            ("SurveyID", "System", "Qualtrics Survey ID. Read-only."),
            ("QID", "System", "Qualtrics Question ID. Read-only."),
            ("ChoiceId", "System", "Qualtrics choice ID for this option."),
            ("QuestionType", "System", "Qualtrics question type (MC, Matrix, etc.)."),
            ("Code", "System", "Choice code or recode value."),
            (
                label_md,
                "Editable",
                f"{base_upper} option label in restricted Markdown.",
            ),
            (
                label_html,
                "Flag",
                f"TRUE when {label_md} should be treated as raw HTML.",
            ),
            (
                "MetaComment",
                "Note",
                "Auto-generated or manual notes (e.g., externally managed scripts).",
            ),
            (
                "Dirty",
                "System",
                "Auto-flag set by qsync preview/apply when a row has pending pushes.",
            ),
        ],
        SUBITEMS_SHEET: [
            ("SurveyID", "System", "Qualtrics Survey ID. Read-only."),
            ("QID", "System", "Qualtrics Question ID. Read-only."),
            ("AnswerId", "System", "Qualtrics sub-item / statement ID."),
            (
                "Field",
                "System",
                "Disambiguator for subitem meaning (Answer | Label).",
            ),
            ("QuestionType", "System", "Qualtrics question type."),
            (
                label_md,
                "Editable",
                f"{base_upper} sub-item text in restricted Markdown.",
            ),
            (
                label_html,
                "Flag",
                f"TRUE when {label_md} should be treated as raw HTML.",
            ),
        ],
        EMBEDDED_DATA_SHEET: [
            ("SurveyID", "System", "Qualtrics Survey ID. Read-only."),
            (
                "FlowID",
                "System",
                "SurveyFlow node ID (disambiguates duplicate fields).",
            ),
            ("FlowOrder", "System", "Survey flow order (0 for JS-only fields)."),
            ("Field", "System", "Embedded data field name. Read-only."),
            ("Value", "Editable", "Default value (--- for fields without defaults)."),
            ("Type", "System", "Embedded data type (Custom, Recipient, JS-only)."),
            ("WrittenByQIDs", "System", "Comma-separated QIDs that set this field."),
            (
                "Dirty",
                "System",
                "Auto-flag set by qsync preview/apply when a row has pending pushes.",
            ),
        ],
        SYSTEM_SHEET: [
            ("SurveyID", "System", "Qualtrics Survey ID for Timing/meta items."),
            ("QID", "System", "Timing question ID."),
            ("QuestionType", "System", "Qualtrics question type."),
            ("DataExportTag", "System", "DataExportTag for the Timing item."),
            ("ChoiceId", "System", "Choice ID inside the Timing question."),
            (
                "Display",
                "System",
                "HTML returned by Qualtrics for the Timing display.",
            ),
        ],
        SURVEY_METADATA_SHEET: [
            ("SurveyID", "System", "Qualtrics Survey ID. Read-only."),
            ("Language", "System", "Language code for this metadata row."),
            ("SurveyTitle_MD", "Editable", "Survey title in restricted Markdown."),
            (
                "SurveyTitle_IsHTML",
                "Flag",
                "TRUE when SurveyTitle_MD should be treated as raw HTML.",
            ),
            (
                "SurveyDescription_MD",
                "Editable",
                "Survey description in restricted Markdown.",
            ),
            (
                "SurveyDescription_IsHTML",
                "Flag",
                "TRUE when SurveyDescription_IsHTML should be treated as raw HTML.",
            ),
            (
                "SurveyMetaDescription_MD",
                "Editable",
                "Survey meta description in restricted Markdown.",
            ),
            (
                "SurveyMetaDescription_IsHTML",
                "Flag",
                "TRUE when SurveyMetaDescription_MD should be treated as raw HTML.",
            ),
        ],
    }


# Backward-compatible alias — default EN base.
COLUMN_GUIDE = _column_guide()


def _make_options_preview_formula(
    cell_ref: str, *, base_language: str = "EN"
) -> ArrayFormula:
    """Generate array formula for dynamic option preview from OptionsTable."""
    label_col = f"Label_{_language_suffix(base_language) or 'en'}_MD"
    formula = (
        "=_xlfn.LET(_xlpm.q,QuestionsTable[[#This Row],[QID]],\n"
        "     IFERROR(_xlfn.TEXTJOIN(CHAR(10), TRUE,\n"
        '         "[" & _xlfn._xlws.FILTER(OptionsTable[ChoiceId], OptionsTable[QID]=_xlpm.q) & "] " &\n'
        f"         _xlfn._xlws.FILTER(OptionsTable[{label_col}], OptionsTable[QID]=_xlpm.q)\n"
        '     ), "")\n'
        ")"
    )
    return ArrayFormula(ref=cell_ref, text=formula)


def _make_subitems_preview_formula(
    cell_ref: str, *, base_language: str = "EN"
) -> ArrayFormula:
    """Generate array formula for dynamic subitem preview from SubitemsTable."""
    label_col = f"Label_{_language_suffix(base_language) or 'en'}_MD"
    formula = (
        "=_xlfn.LET(_xlpm.q,QuestionsTable[[#This Row],[QID]],\n"
        "     IFERROR(_xlfn.TEXTJOIN(CHAR(10), TRUE,\n"
        '         "[" & _xlfn._xlws.FILTER(SubitemsTable[AnswerId], SubitemsTable[QID]=_xlpm.q) & "] " &\n'
        f"         _xlfn._xlws.FILTER(SubitemsTable[{label_col}], SubitemsTable[QID]=_xlpm.q)\n"
        '     ), "")\n'
        ")"
    )
    return ArrayFormula(ref=cell_ref, text=formula)


def _update_instructions_sheet(
    wb: Workbook,
    languages: Sequence[str] | None = None,
    *,
    base_language: str | None = None,
) -> None:
    """Build the Instructions sheet with column ownership + descriptions."""

    if INSTRUCTIONS_SHEET in wb.sheetnames:
        ws = wb[INSTRUCTIONS_SHEET]
        wb.remove(ws)
    ws = wb.create_sheet(title=INSTRUCTIONS_SHEET)
    ws.append(["Sheet", "Column", "Ownership", "Description"])

    base = _normalize_language_code(base_language or "") or "EN"
    guide = _column_guide(base)
    for sheet_name, columns in guide.items():
        for column_name, ownership, description in columns:
            ws.append([sheet_name, column_name, ownership, description])

    extra_langs = [
        lang
        for lang in _ordered_languages(languages, base_language=base_language)
        if lang != base
    ]
    for lang in extra_langs:
        suffix = _language_suffix(lang)
        if not suffix:
            continue
        ws.append(
            [
                QUESTION_SHEET,
                f"Text_{suffix}_MD",
                "Editable",
                f"{lang} wording in restricted Markdown.",
            ]
        )
        ws.append(
            [
                QUESTION_SHEET,
                f"Text_{suffix}_IsHTML",
                "Flag",
                f"TRUE when Text_{suffix}_MD should be treated as raw HTML.",
            ]
        )
        ws.append(
            [
                OPTIONS_SHEET,
                f"Label_{suffix}_MD",
                "Editable",
                f"{lang} option label in restricted Markdown.",
            ]
        )
        ws.append(
            [
                OPTIONS_SHEET,
                f"Label_{suffix}_IsHTML",
                "Flag",
                f"TRUE when Label_{suffix}_MD should be treated as raw HTML.",
            ]
        )
        ws.append(
            [
                SUBITEMS_SHEET,
                f"Label_{suffix}_MD",
                "Editable",
                f"{lang} sub-item text in restricted Markdown.",
            ]
        )
        ws.append(
            [
                SUBITEMS_SHEET,
                f"Label_{suffix}_IsHTML",
                "Flag",
                f"TRUE when Label_{suffix}_MD should be treated as raw HTML.",
            ]
        )

    # Make header row bold
    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    for cell in header_row:
        _make_bold(cell)

    # Apply alignment to all cells
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for cell in row:
            cell.alignment = Alignment(
                vertical="center", horizontal="left", wrap_text=True
            )

    # Set widths
    widths = {
        "Sheet": 18.0,
        "Column": 26.0,
        "Ownership": 14.0,
        "Description": 90.0,
    }
    headers_list = [
        h[0] for h in [("Sheet",), ("Column",), ("Ownership",), ("Description",)]
    ]
    for idx, header in enumerate(headers_list, start=1):
        w = widths.get(header, 20.0)
        ws.column_dimensions[get_column_letter(idx)].width = w

    # Create table
    max_row = ws.max_row
    max_col = ws.max_column
    if max_row >= 2 and max_col >= 1:
        table = Table(
            displayName="InstructionsTable",
            ref=f"A1:{get_column_letter(max_col)}{max_row}",
        )
        style = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        table.tableStyleInfo = style
        # Always refresh the table so its ref covers all rows/columns.
        if "InstructionsTable" in ws._tables:
            del ws._tables["InstructionsTable"]
        ws.add_table(table)


def _translation_key_for_question(qid: str) -> str:
    return f"{qid}_QuestionText"


def _translation_key_for_option(row: "OptionRow") -> tuple[str, str]:
    if row.question_type == "Matrix":
        return "Answer", f"{row.qid}_Answer{row.choice_id}"
    return "Choice", f"{row.qid}_Choice{row.choice_id}"


def _translation_key_for_subitem(row: "SubitemRow") -> tuple[str, str]:
    if row.field == "Label":
        return "Label", f"{row.qid}_Label{row.answer_id}"
    if row.question_type == "Matrix":
        return "Choice", f"{row.qid}_Choice{row.answer_id}"
    return "Answer", f"{row.qid}_Answer{row.answer_id}"


def _update_translation_key_map(
    wb: Workbook,
    questions_map: Dict[str, "QuestionRow"],
    options_map: Dict[Tuple[str, str], "OptionRow"],
    subitems_map: Dict[Tuple[str, str, str], "SubitemRow"],
) -> None:
    if TRANSLATION_KEY_SHEET in wb.sheetnames:
        ws = wb[TRANSLATION_KEY_SHEET]
        wb.remove(ws)
    ws = wb.create_sheet(title=TRANSLATION_KEY_SHEET)

    headers = [
        "Sheet",
        "QuestionType",
        "QID",
        "ChoiceId",
        "AnswerId",
        "Field",
        "TranslationKey",
        "BaseText",
    ]
    ws.append(headers)

    for qid, row in questions_map.items():
        ws.append(
            [
                QUESTION_SHEET,
                row.question_type,
                qid,
                "",
                "",
                "QuestionText",
                _translation_key_for_question(qid),
                row.text_en_md or "",
            ]
        )

    for (qid, choice_id), row in options_map.items():
        field, key = _translation_key_for_option(row)
        ws.append(
            [
                OPTIONS_SHEET,
                row.question_type,
                qid,
                choice_id,
                "",
                field,
                key,
                row.label_en_md or "",
            ]
        )

    for row in subitems_map.values():
        field, key = _translation_key_for_subitem(row)
        ws.append(
            [
                SUBITEMS_SHEET,
                row.question_type,
                row.qid,
                "",
                row.answer_id,
                field,
                key,
                row.label_en_md or "",
            ]
        )

    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    for cell in header_row:
        _make_bold(cell)

    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for cell in row:
            cell.alignment = Alignment(
                vertical="center", horizontal="left", wrap_text=True
            )

    widths = {
        "Sheet": 14.0,
        "QuestionType": 12.0,
        "QID": 10.0,
        "ChoiceId": 10.0,
        "AnswerId": 10.0,
        "Field": 12.0,
        "TranslationKey": 36.0,
        "BaseText": 80.0,
    }
    for idx, name in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(name, 20.0)

    ws.sheet_state = "hidden"


@dataclass
class QuestionRow:
    """Typed representation of a row in the `Questions` sheet."""

    survey_id: str
    qid: str
    block_name: str
    question_type: str
    data_export_tag: str
    question_key: str | None
    text_en_md: str | None
    text_en_is_html: bool
    in_pre: bool
    in_post: bool
    externally_managed_by: str | None = None


@dataclass
class OptionRow:
    """Typed representation of a row in the `Options` sheet."""

    survey_id: str
    qid: str
    choice_id: str
    question_type: str
    export_tag: str
    code: str | None
    label_en_md: str | None
    label_en_is_html: bool
    externally_managed_by: str | None = None


@dataclass
class SubitemRow:
    """Typed representation of a row in the `Subitems` sheet."""

    survey_id: str
    qid: str
    answer_id: str
    field: str
    question_type: str
    export_tag: str
    label_en_md: str | None
    label_en_is_html: bool


@dataclass
class EmbeddedDataRow:
    """Typed representation of a row in the `Embedded_Data` sheet."""

    survey_id: str
    flow_id: str | None
    flow_order: int
    field: str
    value: str | None
    ed_type: str
    written_by_qids: str | None


_SET_EMBEDDED_RE = re.compile(
    r"\bsetEmbeddedData\s*\(\s*(['\"])(?P<field>(?:\\.|(?!\1).)*?)\1\s*,",
    re.IGNORECASE,
)
_SET_EMBEDDED_EXPR_RE = re.compile(
    r"\bsetEmbeddedData\s*\(\s*(?P<expr>.+?)\s*,",
    re.IGNORECASE | re.S,
)


def _strip_js_comments(js_text: str) -> str:
    """Remove JS comments for simple regex scanning."""

    without_block = re.sub(r"/\*.*?\*/", "", js_text, flags=re.S)
    return re.sub(r"//.*", "", without_block)


def _unescape_js_string(value: str) -> str:
    """Unescape simple JS string literals (best-effort)."""

    return value.replace("\\\\", "\\").replace("\\'", "'").replace('\\"', '"').strip()


def _detect_dynamic_field(expr: str) -> str | None:
    expr = (expr or "").strip()
    if not expr:
        return None
    if expr[0] in {"'", '"'}:
        match = re.match(
            r"^(['\"])(?P<prefix>(?:\\.|(?!\1).)*?)\1\s*\+",
            expr,
        )
        if match:
            prefix = _unescape_js_string(match.group("prefix") or "")
            return f"{prefix}*" if prefix else None
        return None
    if expr[0] == "`":
        match = re.match(r"^`(?P<prefix>[^`$]*)\$\{", expr)
        if match:
            prefix = match.group("prefix") or ""
            return f"{prefix}*" if prefix else None
    return None


def _collect_js_embedded_data_fields(survey_payload: dict) -> Dict[str, List[str]]:
    """Map embedded data fields to QIDs that set them via QuestionJS."""

    result = survey_payload.get("result", {})
    questions = result.get("Questions") or {}
    valid_qids = _get_valid_qids(survey_payload)
    field_to_qids: Dict[str, set[str]] = {}

    for qid, details in questions.items():
        if valid_qids and qid not in valid_qids:
            continue
        js_text = (
            details.get("QuestionJS") or details.get("QuestionJSContent") or ""
        ).strip()
        if not js_text:
            continue
        cleaned = _strip_js_comments(js_text)
        for match in _SET_EMBEDDED_RE.finditer(cleaned):
            raw = match.group("field") or ""
            field = _unescape_js_string(raw)
            if not field:
                continue
            field_to_qids.setdefault(field, set()).add(qid)
        for match in _SET_EMBEDDED_EXPR_RE.finditer(cleaned):
            expr = match.group("expr") or ""
            dynamic_field = _detect_dynamic_field(expr)
            if not dynamic_field:
                continue
            field_to_qids.setdefault(dynamic_field, set()).add(qid)

    return {field: sorted(qids) for field, qids in field_to_qids.items()}


def _iter_embedded_data_nodes(survey_payload: dict) -> List[Tuple[int, dict]]:
    """Return ordered (flow_order, node) pairs for EmbeddedData SurveyFlow nodes."""

    flow = survey_payload.get("result", {}).get("SurveyFlow", {}).get("Flow", [])
    nodes: List[Tuple[int, dict]] = []
    order = 0

    def walk(flow_list):
        nonlocal order
        if not isinstance(flow_list, list):
            return
        for node in flow_list:
            if not isinstance(node, dict):
                continue
            if node.get("Type") == "EmbeddedData":
                order += 1
                nodes.append((order, node))
            subflow = node.get("Flow")
            if isinstance(subflow, list):
                walk(subflow)

    walk(flow)
    return nodes


def build_embedded_data_rows(
    survey_id: str, survey_payload: dict
) -> List[EmbeddedDataRow]:
    """Build EmbeddedDataRow objects from SurveyFlow + QuestionJS."""

    js_map = _collect_js_embedded_data_fields(survey_payload)
    rows: List[EmbeddedDataRow] = []
    fields_in_flow: set[str] = set()

    for flow_order, node in _iter_embedded_data_nodes(survey_payload):
        flow_id = str(node.get("FlowID") or "").strip() or None
        for entry in node.get("EmbeddedData", []) or []:
            field = str(entry.get("Field") or "").strip()
            if not field:
                continue
            fields_in_flow.add(field)
            value = entry.get("Value")
            value_str = str(value) if value is not None else None
            ed_type = str(entry.get("Type") or "").strip() or "Custom"
            written_by = js_map.get(field) or []
            written_by_qids = ",".join(written_by) if written_by else ""
            rows.append(
                EmbeddedDataRow(
                    survey_id=survey_id,
                    flow_id=flow_id,
                    flow_order=flow_order,
                    field=field,
                    value=value_str,
                    ed_type=ed_type,
                    written_by_qids=written_by_qids,
                )
            )

    for field in sorted(js_map.keys()):
        if field in fields_in_flow:
            continue
        written_by = js_map.get(field) or []
        written_by_qids = ",".join(written_by) if written_by else ""
        rows.append(
            EmbeddedDataRow(
                survey_id=survey_id,
                flow_id=None,
                flow_order=0,
                field=field,
                value=None,
                ed_type="JS-only",
                written_by_qids=written_by_qids,
            )
        )

    rows.sort(
        key=lambda r: (
            r.flow_order == 0,
            r.flow_order,
            (r.flow_id or ""),
            r.field,
        )
    )
    return rows


def _get_or_create_sheet(wb: Workbook, name: str) -> Worksheet:
    if name in wb.sheetnames:
        return wb[name]
    return wb.create_sheet(title=name)


def _strip_all_comments(wb: Workbook) -> None:
    """Remove legacy Excel comments so the workbook saves cleanly."""

    for ws in wb.worksheets:
        max_row = ws.max_row or 0
        max_col = ws.max_column or 0
        if max_row == 0 or max_col == 0:
            continue
        for row in ws.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_col):
            for cell in row:
                if cell.comment is not None:
                    cell.comment = None


def _iter_sheet_rows(ws: Worksheet):
    rows = list(ws.iter_rows(values_only=False))
    if not rows:
        return [], []
    header_row = rows[0]
    headers = [cell.value or "" for cell in header_row]
    data_rows = rows[1:]
    return headers, data_rows


def _reorder_columns(ws: Worksheet, ordered_headers: List[str]) -> None:
    """Reorder worksheet columns to match ordered_headers, preserving values."""

    headers, data_rows = _iter_sheet_rows(ws)
    if not headers:
        ws.append(ordered_headers)
        return
    if headers == ordered_headers:
        return

    index_map = {name: idx for idx, name in enumerate(headers)}
    new_rows: List[List[object]] = [ordered_headers]
    for row in data_rows:
        new_rows.append(
            [
                row[index_map[name]].value if name in index_map else None
                for name in ordered_headers
            ]
        )

    if ws.max_row:
        ws.delete_rows(1, ws.max_row)
    for row_values in new_rows:
        ws.append(row_values)


def _ensure_columns(ws: Worksheet, required: List[str]) -> Dict[str, int]:
    """Ensure required columns exist in order; return mapping name -> 0-based index."""

    header_cells = list(ws.iter_rows(min_row=1, max_row=1, values_only=False))
    if header_cells:
        header_cells = header_cells[0]
        headers = [c.value or "" for c in header_cells]
        # If the header row is effectively empty (all blanks), treat as no header.
        if not any(headers):
            headers = []
    else:
        headers = []

    if not headers:
        for col in required:
            ws.cell(row=1, column=len(headers) + 1, value=col)
            headers.append(col)
        return {name: idx for idx, name in enumerate(headers)}

    extras = [h for h in headers if h not in required]
    ordered_headers = list(dict.fromkeys(required + extras))
    _reorder_columns(ws, ordered_headers)
    return {name: idx for idx, name in enumerate(ordered_headers)}


EXTERNALLY_MANAGED_TAGS: Dict[str, str] = {
    # DataExportTag -> controlling script
    "newsmem_recognition": "scripts/update_newsmem_recognition.py",
    "newsmem_salience": "scripts/update_salience_items.py",
    "newsmem_recall_cued": "scripts/update_salience_items.py",
}
_EXTERNALLY_MANAGED_NOTE_PREFIX = "Externally managed – edit via "


def _is_externally_managed_question(data_export_tag: str | None) -> Optional[str]:
    if not data_export_tag:
        return None
    return EXTERNALLY_MANAGED_TAGS.get(data_export_tag)


def _externally_managed_note(script: str) -> str:
    return f"{_EXTERNALLY_MANAGED_NOTE_PREFIX}{script}, not directly in Excel."


def _iter_block_ids_in_flow(survey_payload: dict) -> List[str]:
    """Return block IDs in survey-flow order."""

    result = survey_payload.get("result", {})
    flow = result.get("SurveyFlow", {})
    ordered_block_ids: List[str] = []

    def walk(node):
        if isinstance(node, dict):
            # Qualtrics uses both `Standard` and nested `Block` nodes in SurveyFlow.
            # Treat both as block references for ordering.
            if node.get("Type") in {"Standard", "Block"} and "ID" in node:
                bid = node["ID"]
                if bid not in ordered_block_ids:
                    ordered_block_ids.append(bid)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, (dict, list)):
                    walk(v)

    walk(flow.get("Flow", []))
    return ordered_block_ids


def _build_option_previews(survey_payload: dict) -> Dict[str, str]:
    """Map QID -> newline-joined option labels (Markdown).

    For Matrix questions, uses Answers (response scale).
    For MC/SC questions, uses Choices.
    """

    result: Dict[str, List[str]] = {}
    questions = survey_payload.get("result", {}).get("Questions", {})
    for qid, q in questions.items():
        qtype = q.get("QuestionType") or ""

        if qtype == "Matrix":
            # For Matrix, options are the Answers (response scale)
            answers = q.get("Answers") or {}
            if not answers:
                continue
            order: List[str] = []
            answer_order = q.get("AnswerOrder")
            if isinstance(answer_order, list):
                order.extend([str(aid) for aid in answer_order if str(aid) in answers])
            for aid in answers.keys():
                said = str(aid)
                if said not in order:
                    order.append(said)
            lines: List[str] = []
            for aid in order:
                answer = answers.get(aid)
                if not answer:
                    continue
                display = answer.get("Display") or ""
                text = html_to_md(display)
                if text:
                    lines.append(text)
            if lines:
                result[qid] = lines
        else:
            # For MC/SC, options are Choices
            choices = q.get("Choices") or {}
            if not choices:
                continue
            order: List[str] = []
            choice_order = q.get("ChoiceOrder")
            if isinstance(choice_order, list):
                order.extend([str(cid) for cid in choice_order if str(cid) in choices])
            for cid in choices.keys():
                scid = str(cid)
                if scid not in order:
                    order.append(scid)
            lines: List[str] = []
            for cid in order:
                choice = choices.get(cid)
                if not choice:
                    continue
                display = choice.get("Display") or ""
                text = html_to_md(display)
                if text:
                    lines.append(text)
            if lines:
                result[qid] = lines

    return {qid: "\n".join(lines) for qid, lines in result.items()}


def _build_subitem_previews(survey_payload: dict) -> Dict[str, str]:
    """Map QID -> newline-joined subitem labels (Markdown).

    For Matrix questions, uses Choices (matrix rows/statements).
    For other questions, uses Answers.
    """

    result: Dict[str, List[str]] = {}
    questions = survey_payload.get("result", {}).get("Questions", {})
    for qid, q in questions.items():
        qtype = q.get("QuestionType") or ""

        if qtype == "Matrix":
            # For Matrix, subitems are Choices (the statements/headlines)
            choices = q.get("Choices") or {}
            if not choices:
                continue
            order: List[str] = []
            choice_order = q.get("ChoiceOrder")
            if isinstance(choice_order, list):
                order.extend([str(cid) for cid in choice_order if str(cid) in choices])
            for cid in choices.keys():
                scid = str(cid)
                if scid not in order:
                    order.append(scid)
            lines: List[str] = []
            for cid in order:
                choice = choices.get(cid)
                if not choice:
                    continue
                display = choice.get("Display") or ""
                text = html_to_md(display)
                if text:
                    lines.append(text)
            if lines:
                result[qid] = lines
        else:
            # For other questions, subitems are Answers
            answers = q.get("Answers") or {}
            if not answers:
                continue
            lines: List[str] = []
            for answer_id, answer in answers.items():
                display = answer.get("Display")
                text = html_to_md(str(display) if display is not None else "")
                if text:
                    lines.append(text)
            if lines:
                result[qid] = lines

    return {qid: "\n".join(lines) for qid, lines in result.items()}


def _get_valid_qids(survey_payload: dict) -> set[str]:
    """
    Extract QIDs from non-Trash blocks to ensure referential integrity.

    Returns a set of QIDs that appear in the Questions sheet (i.e., questions
    from non-Trash blocks only). This is used to filter Options and Subitems
    sheets so they don't contain orphaned rows for Trash questions.
    """
    result = survey_payload.get("result", {})
    questions = result.get("Questions", {})
    blocks = result.get("Blocks", {})

    valid_qids: set[str] = set()

    # Iterate all non-Trash blocks
    for block_id, block in blocks.items():
        if block.get("Type") == "Trash":
            continue

        # Extract QIDs from BlockElements
        for be in block.get("BlockElements", []):
            if be.get("Type") != "Question":
                continue
            qid = be.get("QuestionID")
            if qid and qid in questions:
                valid_qids.add(qid)

    return valid_qids


def build_question_rows(
    survey_id: str,
    survey_payload: dict,
) -> Dict[str, QuestionRow]:
    """Build QuestionRow objects from a survey JSON payload.

    Questions are ordered by SurveyFlow block order and BlockElements order
    to mirror the Qualtrics survey flow (excluding Trash blocks).
    """

    result = survey_payload.get("result", {})
    questions = result.get("Questions", {})
    blocks = result.get("Blocks", {})

    rows: Dict[str, QuestionRow] = {}

    block_ids_in_flow = _iter_block_ids_in_flow(survey_payload)
    seen_block_ids = set()

    def handle_block(block_id: str):
        block = blocks.get(block_id)
        if not block:
            return
        if block.get("Type") == "Trash":
            return
        block_name = block.get("Description") or ""
        for be in block.get("BlockElements", []):
            if be.get("Type") != "Question":
                continue
            qid = be.get("QuestionID")
            if not qid or qid not in questions:
                continue
            if qid in rows:
                continue
            q = questions[qid]
            qtype = q.get("QuestionType") or ""
            tag = q.get("DataExportTag") or ""
            text_html = q.get("QuestionText") or ""

            if is_markdown_safe_html(text_html):
                text_md = html_to_md(text_html)
                is_html = False
            elif should_treat_as_html(text_html):
                text_md = normalize_text(text_html)
                is_html = True
            else:
                # Plaintext or odd edge cases – treat as Markdown.
                text_md = html_to_md(text_html)
                is_html = False

            rows[qid] = QuestionRow(
                survey_id=survey_id,
                qid=qid,
                block_name=block_name,
                question_type=qtype,
                data_export_tag=tag,
                question_key=None,
                text_en_md=text_md,
                text_en_is_html=is_html,
                in_pre=False,
                in_post=False,
                externally_managed_by=None,
            )

    # 1) Blocks that appear in SurveyFlow
    for bid in block_ids_in_flow:
        handle_block(bid)
        seen_block_ids.add(bid)

    # 2) Any remaining non-Trash blocks not referenced in SurveyFlow
    for bid, block in blocks.items():
        if bid in seen_block_ids:
            continue
        if block.get("Type") == "Trash":
            continue
        handle_block(bid)

    return rows


def build_option_rows(
    survey_id: str,
    survey_payload: dict,
) -> Dict[Tuple[str, str], OptionRow]:
    """Build OptionRow objects from a survey JSON payload.

    Mapping rules (must stay in sync with sync_core.py):
    - For MC/SC questions: options come from `Choices` (one row per choice).
    - For Matrix questions: options come from `Answers` (the response scale).
    Options for each question are ordered by `ChoiceOrder` or `AnswerOrder`
    where available so they match the respondent-facing order.

    Only includes options for questions that appear in non-Trash blocks to
    maintain referential integrity with the Questions sheet.
    """

    questions = survey_payload.get("result", {}).get("Questions", {})
    valid_qids = _get_valid_qids(survey_payload)

    rows: Dict[Tuple[str, str], OptionRow] = {}
    for qid, q in questions.items():
        # Skip questions from Trash blocks (not in Questions sheet)
        if qid not in valid_qids:
            continue

        qtype = q.get("QuestionType") or ""
        tag = q.get("DataExportTag") or ""
        externally_by = _is_externally_managed_question(tag)

        # For Matrix questions, options come from Answers (the response scale)
        if qtype == "Matrix":
            answers = q.get("Answers")
            if not answers:
                continue

            ordered_ids: List[str] = []
            answer_order = q.get("AnswerOrder")
            if isinstance(answer_order, list) and answer_order:
                ordered_ids.extend(
                    [str(aid) for aid in answer_order if str(aid) in answers]
                )

            for aid in answers.keys():
                if str(aid) not in ordered_ids:
                    ordered_ids.append(str(aid))

            for answer_id in ordered_ids:
                answer = answers.get(answer_id)
                if not answer:
                    continue
                display = answer.get("Display") or ""

                if is_markdown_safe_html(display):
                    label_md = html_to_md(display)
                    is_html = False
                elif should_treat_as_html(display):
                    label_md = normalize_text(display)
                    is_html = True
                else:
                    label_md = html_to_md(display)
                    is_html = False

                key = (qid, answer_id)
                rows[key] = OptionRow(
                    survey_id=survey_id,
                    qid=qid,
                    choice_id=str(answer_id),
                    question_type=qtype,
                    export_tag=tag,
                    code=None,
                    label_en_md=label_md,
                    label_en_is_html=is_html,
                    externally_managed_by=externally_by,
                )
        else:
            # For MC/SC questions, options come from Choices
            choices = q.get("Choices")
            if not choices:
                continue

            # Determine iteration order for choices.
            ordered_ids: List[str] = []
            choice_order = q.get("ChoiceOrder")
            if isinstance(choice_order, list) and choice_order:
                # ChoiceOrder may be numeric; normalise to strings for lookup.
                ordered_ids.extend(
                    [str(cid) for cid in choice_order if str(cid) in choices]
                )

            # Append any remaining choices not in ChoiceOrder, preserving dict order.
            for cid in choices.keys():
                if str(cid) not in ordered_ids:
                    ordered_ids.append(str(cid))

            for choice_id in ordered_ids:
                choice = choices.get(choice_id)
                if not choice:
                    continue
                display = choice.get("Display") or ""
                code = choice.get("Recode") or None

                if is_markdown_safe_html(display):
                    label_md = html_to_md(display)
                    is_html = False
                elif should_treat_as_html(display):
                    label_md = normalize_text(display)
                    is_html = True
                else:
                    label_md = html_to_md(display)
                    is_html = False

                key = (qid, choice_id)
                rows[key] = OptionRow(
                    survey_id=survey_id,
                    qid=qid,
                    choice_id=str(choice_id),
                    question_type=qtype,
                    export_tag=tag,
                    code=code,
                    label_en_md=label_md,
                    label_en_is_html=is_html,
                    externally_managed_by=externally_by,
                )
    return rows


def build_subitem_rows(
    survey_id: str,
    survey_payload: dict,
) -> Dict[Tuple[str, str, str], SubitemRow]:
    """Build SubitemRow objects from a survey JSON payload.

    Mapping rules (must stay in sync with sync_core.py):
    - For Matrix questions: subitems come from `Choices` (matrix rows/statements).
    - For other question types: subitems come from `Answers` (if present).
    - Label rows (Field=Label) come from `Labels` when present.

    Only includes subitems for questions that appear in non-Trash blocks to
    maintain referential integrity with the Questions sheet.
    """

    questions = survey_payload.get("result", {}).get("Questions", {})
    valid_qids = _get_valid_qids(survey_payload)

    rows: Dict[Tuple[str, str, str], SubitemRow] = {}
    for qid, q in questions.items():
        # Skip questions from Trash blocks (not in Questions sheet)
        if qid not in valid_qids:
            continue

        qtype = q.get("QuestionType") or ""
        tag = q.get("DataExportTag") or ""

        # For Matrix questions, subitems come from Choices (the matrix rows)
        if qtype == "Matrix":
            choices = q.get("Choices")
            if not choices:
                continue

            ordered_ids: List[str] = []
            choice_order = q.get("ChoiceOrder")
            if isinstance(choice_order, list) and choice_order:
                ordered_ids.extend(
                    [str(cid) for cid in choice_order if str(cid) in choices]
                )

            for cid in choices.keys():
                if str(cid) not in ordered_ids:
                    ordered_ids.append(str(cid))

            for choice_id in ordered_ids:
                choice = choices.get(choice_id)
                if not choice:
                    continue
                display = choice.get("Display") or ""

                if is_markdown_safe_html(display):
                    label_md = html_to_md(display)
                    is_html = False
                elif should_treat_as_html(display):
                    label_md = normalize_text(display)
                    is_html = True
                else:
                    label_md = html_to_md(display)
                    is_html = False

                rows[(qid, "Answer", str(choice_id))] = SubitemRow(
                    survey_id=survey_id,
                    qid=qid,
                    answer_id=str(choice_id),
                    field="Answer",
                    question_type=qtype,
                    export_tag=tag,
                    label_en_md=label_md,
                    label_en_is_html=is_html,
                )
        else:
            # For other question types, subitems come from Answers
            answers = q.get("Answers")
            if answers:
                for answer_id, answer in answers.items():
                    raw_display = answer.get("Display")
                    display = str(raw_display) if raw_display is not None else ""

                    if is_markdown_safe_html(display):
                        label_md = html_to_md(display)
                        is_html = False
                    elif should_treat_as_html(display):
                        label_md = normalize_text(display)
                        is_html = True
                    else:
                        label_md = html_to_md(display)
                        is_html = False

                    rows[(qid, "Answer", str(answer_id))] = SubitemRow(
                        survey_id=survey_id,
                        qid=qid,
                        answer_id=str(answer_id),
                        field="Answer",
                        question_type=qtype,
                        export_tag=tag,
                        label_en_md=label_md,
                        label_en_is_html=is_html,
                    )

        labels = q.get("Labels") or {}
        if labels:
            for label_id, label in labels.items():
                raw_display = label.get("Display")
                display = str(raw_display) if raw_display is not None else ""

                if is_markdown_safe_html(display):
                    label_md = html_to_md(display)
                    is_html = False
                elif should_treat_as_html(display):
                    label_md = normalize_text(display)
                    is_html = True
                else:
                    label_md = html_to_md(display)
                    is_html = False

                rows[(qid, "Label", str(label_id))] = SubitemRow(
                    survey_id=survey_id,
                    qid=qid,
                    answer_id=str(label_id),
                    field="Label",
                    question_type=qtype,
                    export_tag=tag,
                    label_en_md=label_md,
                    label_en_is_html=is_html,
                )

    return rows


def _extract_base_language(survey_payload: dict) -> str:
    """Extract the base survey language from the payload, defaulting to EN."""
    result = survey_payload.get("result", {})
    if not isinstance(result, dict):
        result = survey_payload
    options = result.get("SurveyOptions") or {}
    lang = _normalize_language_code(options.get("SurveyLanguage") or "")
    return lang or "EN"


def init_workbook_from_survey(
    survey_id: str,
    survey_payload: dict,
    xlsx_path: Path,
    *,
    languages: Sequence[str] | None = None,
) -> None:
    """Create or update an Excel workbook from a survey JSON payload.

    This is the workbook “source-of-truth” initializer used by `qsync init`.

    - Creates Questions/Options/Subitems sheets if missing.
    - Adds rows for new QIDs/choices/subitems.
    - Does not overwrite existing edited Markdown cells (`*_en_MD`).
    - Flags externally managed rows via the Options `MetaComment` column.
    - Rebuilds the Instructions sheet with column guidance.

    Args:
        survey_id: Qualtrics survey ID (e.g., `SV_xxx`).
        survey_payload: Survey JSON payload (as returned by the Qualtrics API).
        xlsx_path: Where to write the workbook.
        languages: Optional list of language codes to add as translation columns.

    Example:
        >>> from pathlib import Path
        >>> from qsync.excel_io import init_workbook_from_survey
        >>> from qsync.qualtrics_client import load_cached_survey
        >>> survey = load_cached_survey("SV_xxx")  # requires surveys/SV_xxx.json
        >>> init_workbook_from_survey("SV_xxx", survey.payload, Path("excel/SV_xxx.xlsx"))
    """

    xlsx_path = Path(xlsx_path)
    if xlsx_path.exists():
        wb = load_workbook(xlsx_path)
    else:
        wb = Workbook()
        # Remove default sheet if present; we will create our own.
        default_sheet = wb.active
        wb.remove(default_sheet)

    # Ensure no lingering Excel comments survive from older exports.
    _strip_all_comments(wb)

    base_language = _extract_base_language(survey_payload)

    questions_map = build_question_rows(survey_id, survey_payload)
    options_map = build_option_rows(survey_id, survey_payload)
    subitems_map = build_subitem_rows(survey_id, survey_payload)
    embedded_rows = build_embedded_data_rows(survey_id, survey_payload)
    option_previews = _build_option_previews(survey_payload)
    subitem_previews = _build_subitem_previews(survey_payload)

    _init_questions_sheet(
        wb,
        questions_map,
        option_previews,
        subitem_previews,
        survey_payload,
        languages=languages,
        base_language=base_language,
    )
    _init_options_sheet(
        wb,
        options_map,
        survey_payload,
        languages=languages,
        base_language=base_language,
    )
    _init_subitems_sheet(
        wb,
        subitems_map,
        survey_payload,
        languages=languages,
        base_language=base_language,
    )
    _init_survey_metadata_sheet(wb, survey_payload, languages=languages)
    _init_embedded_data_sheet(wb, embedded_rows)

    # Normalise ordering in the options/subitems sheets so rows are grouped
    # deterministically by (QID, ChoiceId/AnswerId). This keeps previews and
    # manual inspection predictable, even after multiple init runs.
    _sort_sheet_by_qid_and_id(wb[OPTIONS_SHEET], "ChoiceId")
    _sort_sheet_by_qid_and_id(wb[SUBITEMS_SHEET], "AnswerId")
    _sort_sheet_by_flow_order(wb[EMBEDDED_DATA_SHEET])

    # Apply table styles, wrapping, colours, and validations.
    _format_questions_sheet(wb[QUESTION_SHEET])
    _format_options_sheet(wb[OPTIONS_SHEET])
    _format_subitems_sheet(wb[SUBITEMS_SHEET])
    _format_survey_metadata_sheet(wb[SURVEY_METADATA_SHEET])
    _format_embedded_data_sheet(wb[EMBEDDED_DATA_SHEET])

    # Optional: add a System sheet for inspection of Timing/meta options.
    _populate_system_sheet(wb, survey_id, survey_payload)

    # Document the workbook layout so we no longer rely on Excel comments.
    _update_translation_key_map(wb, questions_map, options_map, subitems_map)
    _update_instructions_sheet(wb, languages=languages, base_language=base_language)

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(xlsx_path)


def _init_questions_sheet(
    wb: Workbook,
    questions_map: Dict[str, QuestionRow],
    option_previews: Dict[str, str],
    subitem_previews: Dict[str, str],
    survey_payload: dict,
    *,
    languages: Sequence[str] | None = None,
    base_language: str = "EN",
) -> None:
    ws = _get_or_create_sheet(wb, QUESTION_SHEET)

    base_suffix = _language_suffix(base_language) or "en"
    base_text_col = f"Text_{base_suffix}_MD"
    base_html_col = f"Text_{base_suffix}_IsHTML"

    text_columns = _translation_columns("Text", languages, base_language=base_language)
    required_cols = [
        "SurveyID",
        "QID",
        "BlockName",
        "QuestionType",
        "DataExportTag",
        "QuestionKey",
        *text_columns,
        "OptionsPreview",
        "SubitemsPreview",
        "InPre",
        "InPost",
    ]
    col_index = _ensure_columns(ws, required_cols)

    # Build index of existing rows by QID
    headers, data_rows = _iter_sheet_rows(ws)
    qid_col = col_index["QID"]
    existing_rows: Dict[str, int] = {}
    for idx, row in enumerate(data_rows, start=2):
        cell = row[qid_col]
        qid = str(cell.value).strip() if cell.value is not None else ""
        if qid:
            existing_rows[qid] = idx

    questions = survey_payload.get("result", {}).get("Questions", {})
    text_lang_columns: dict[str, tuple[str, str | None]] = {}
    for name in headers:
        header = str(name or "")
        if not header.startswith("Text_") or not header.endswith("_MD"):
            continue
        suffix = header[len("Text_") : -len("_MD")]
        lang_code = _language_from_suffix(suffix)
        is_html_name = f"Text_{suffix}_IsHTML"
        text_lang_columns[lang_code] = (
            header,
            is_html_name if is_html_name in col_index else None,
        )

    for qid, row_data in questions_map.items():
        q_json = questions.get(qid, {}) if isinstance(questions, dict) else {}
        language_blocks = q_json.get("Language") or {}

        if qid in existing_rows:
            row_idx = existing_rows[qid]
            # Update read-only metadata; do not touch user-managed cells unless blank.
            ws.cell(
                row=row_idx, column=col_index["SurveyID"] + 1, value=row_data.survey_id
            )
            ws.cell(row=row_idx, column=col_index["QID"] + 1, value=row_data.qid)
            ws.cell(
                row=row_idx,
                column=col_index["BlockName"] + 1,
                value=row_data.block_name,
            )
            ws.cell(
                row=row_idx,
                column=col_index["QuestionType"] + 1,
                value=row_data.question_type,
            )
            ws.cell(
                row=row_idx,
                column=col_index["DataExportTag"] + 1,
                value=row_data.data_export_tag,
            )

            text_cell = ws.cell(row=row_idx, column=col_index[base_text_col] + 1)
            is_html_cell = ws.cell(row=row_idx, column=col_index[base_html_col] + 1)
            if (
                text_cell.value is None or str(text_cell.value).strip() == ""
            ) and row_data.text_en_md:
                text_cell.value = row_data.text_en_md
            is_html_cell.value = bool(row_data.text_en_is_html)

            for lang_code, (md_col, html_col) in text_lang_columns.items():
                if lang_code == _normalize_language_code(base_language):
                    continue
                lang_block = _lookup_language_block(language_blocks, lang_code)
                if not lang_block:
                    continue
                lang_text = lang_block.get("QuestionText")
                text_md, is_html = _metadata_cell_value(lang_text)
                if not text_md:
                    continue
                lang_cell = ws.cell(row=row_idx, column=col_index[md_col] + 1)
                if lang_cell.value is not None and str(lang_cell.value).strip() != "":
                    continue
                lang_cell.value = text_md
                if html_col:
                    ws.cell(
                        row=row_idx,
                        column=col_index[html_col] + 1,
                        value=bool(is_html),
                    )
            if "OptionsPreview" in col_index:
                col_letter = get_column_letter(col_index["OptionsPreview"] + 1)
                cell_ref = f"{col_letter}{row_idx}"
                ws.cell(
                    row=row_idx,
                    column=col_index["OptionsPreview"] + 1,
                    value=_make_options_preview_formula(
                        cell_ref, base_language=base_language
                    ),
                )
            if "SubitemsPreview" in col_index:
                col_letter = get_column_letter(col_index["SubitemsPreview"] + 1)
                cell_ref = f"{col_letter}{row_idx}"
                ws.cell(
                    row=row_idx,
                    column=col_index["SubitemsPreview"] + 1,
                    value=_make_subitems_preview_formula(
                        cell_ref, base_language=base_language
                    ),
                )
        else:
            # Append new row
            new_row_idx = ws.max_row + 1
            ws.cell(
                row=new_row_idx,
                column=col_index["SurveyID"] + 1,
                value=row_data.survey_id,
            )
            ws.cell(row=new_row_idx, column=col_index["QID"] + 1, value=row_data.qid)
            ws.cell(
                row=new_row_idx,
                column=col_index["BlockName"] + 1,
                value=row_data.block_name,
            )
            ws.cell(
                row=new_row_idx,
                column=col_index["QuestionType"] + 1,
                value=row_data.question_type,
            )
            ws.cell(
                row=new_row_idx,
                column=col_index["DataExportTag"] + 1,
                value=row_data.data_export_tag,
            )
            # QuestionKey left blank by default

            text_cell = ws.cell(
                row=new_row_idx,
                column=col_index[base_text_col] + 1,
                value=row_data.text_en_md,
            )
            is_html_cell = ws.cell(
                row=new_row_idx,
                column=col_index[base_html_col] + 1,
                value=bool(row_data.text_en_is_html),
            )

            for lang_code, (md_col, html_col) in text_lang_columns.items():
                if lang_code == _normalize_language_code(base_language):
                    continue
                lang_block = _lookup_language_block(language_blocks, lang_code)
                if not lang_block:
                    continue
                lang_text = lang_block.get("QuestionText")
                text_md, is_html = _metadata_cell_value(lang_text)
                if not text_md:
                    continue
                lang_cell = ws.cell(row=new_row_idx, column=col_index[md_col] + 1)
                if lang_cell.value is not None and str(lang_cell.value).strip() != "":
                    continue
                lang_cell.value = text_md
                if html_col:
                    ws.cell(
                        row=new_row_idx,
                        column=col_index[html_col] + 1,
                        value=bool(is_html),
                    )
            # Routing flags start empty/False

            if "OptionsPreview" in col_index:
                col_letter = get_column_letter(col_index["OptionsPreview"] + 1)
                cell_ref = f"{col_letter}{new_row_idx}"
                ws.cell(
                    row=new_row_idx,
                    column=col_index["OptionsPreview"] + 1,
                    value=_make_options_preview_formula(
                        cell_ref, base_language=base_language
                    ),
                )
            if "SubitemsPreview" in col_index:
                col_letter = get_column_letter(col_index["SubitemsPreview"] + 1)
                cell_ref = f"{col_letter}{new_row_idx}"
                ws.cell(
                    row=new_row_idx,
                    column=col_index["SubitemsPreview"] + 1,
                    value=_make_subitems_preview_formula(
                        cell_ref, base_language=base_language
                    ),
                )


def _init_options_sheet(
    wb: Workbook,
    options_map: Dict[Tuple[str, str], OptionRow],
    survey_payload: dict,
    *,
    languages: Sequence[str] | None = None,
    base_language: str = "EN",
) -> None:
    ws = _get_or_create_sheet(wb, OPTIONS_SHEET)
    base_suffix = _language_suffix(base_language) or "en"
    base_label_col = f"Label_{base_suffix}_MD"
    base_label_html_col = f"Label_{base_suffix}_IsHTML"
    label_columns = _translation_columns(
        "Label", languages, base_language=base_language
    )
    required_cols = [
        "SurveyID",
        "QID",
        "ChoiceId",
        "QuestionType",
        "ExportTag",
        "Code",
        *label_columns,
        "MetaComment",
    ]
    # Legacy clean-up: drop deprecated DisplayPreview column if it exists.
    headers, _ = _iter_sheet_rows(ws)
    if headers and "DisplayPreview" in headers:
        disp_idx = headers.index("DisplayPreview") + 1
        ws.delete_cols(disp_idx)

    col_index = _ensure_columns(ws, required_cols)

    headers, data_rows = _iter_sheet_rows(ws)
    label_lang_columns: dict[str, tuple[str, str | None]] = {}
    for name in headers:
        header = str(name or "")
        if not header.startswith("Label_") or not header.endswith("_MD"):
            continue
        suffix = header[len("Label_") : -len("_MD")]
        lang_code = _language_from_suffix(suffix)
        is_html_name = f"Label_{suffix}_IsHTML"
        label_lang_columns[lang_code] = (
            header,
            is_html_name if is_html_name in col_index else None,
        )

    qid_col = col_index["QID"]
    choice_col = col_index["ChoiceId"]
    existing_rows: Dict[Tuple[str, str], int] = {}
    for idx, row in enumerate(data_rows, start=2):
        qid_val = row[qid_col].value
        choice_val = row[choice_col].value
        if qid_val is None or choice_val is None:
            continue
        key = (str(qid_val).strip(), str(choice_val).strip())
        existing_rows[key] = idx

    # Build a lookup of question types for system-only routing later
    questions = survey_payload.get("result", {}).get("Questions", {})

    for key, row_data in options_map.items():
        qid, choice_id = key
        # Skip options for pure Timing/meta questions; they go to System sheet.
        q_json = questions.get(qid, {})
        qtype = q_json.get("QuestionType")
        if qtype == "Timing":
            continue
        if key in existing_rows:
            row_idx = existing_rows[key]
            ws.cell(
                row=row_idx, column=col_index["SurveyID"] + 1, value=row_data.survey_id
            )
            ws.cell(row=row_idx, column=col_index["QID"] + 1, value=row_data.qid)
            ws.cell(
                row=row_idx, column=col_index["ChoiceId"] + 1, value=row_data.choice_id
            )
            ws.cell(
                row=row_idx,
                column=col_index["QuestionType"] + 1,
                value=row_data.question_type,
            )
            ws.cell(
                row=row_idx,
                column=col_index["ExportTag"] + 1,
                value=row_data.export_tag,
            )
            if row_data.code is not None:
                ws.cell(row=row_idx, column=col_index["Code"] + 1, value=row_data.code)

            label_cell = ws.cell(row=row_idx, column=col_index[base_label_col] + 1)
            is_html_cell = ws.cell(
                row=row_idx, column=col_index[base_label_html_col] + 1
            )
            if (
                label_cell.value is None or str(label_cell.value).strip() == ""
            ) and row_data.label_en_md:
                label_cell.value = row_data.label_en_md
            is_html_cell.value = bool(row_data.label_en_is_html)

            meta_cell = ws.cell(row=row_idx, column=col_index["MetaComment"] + 1)
            if row_data.externally_managed_by:
                meta_cell.value = _externally_managed_note(
                    row_data.externally_managed_by
                )
            else:
                current = str(meta_cell.value or "").strip()
                if current.startswith(_EXTERNALLY_MANAGED_NOTE_PREFIX):
                    meta_cell.value = ""
        else:
            new_row_idx = ws.max_row + 1
            ws.cell(
                row=new_row_idx,
                column=col_index["SurveyID"] + 1,
                value=row_data.survey_id,
            )
            ws.cell(row=new_row_idx, column=col_index["QID"] + 1, value=row_data.qid)
            ws.cell(
                row=new_row_idx,
                column=col_index["ChoiceId"] + 1,
                value=row_data.choice_id,
            )
            ws.cell(
                row=new_row_idx,
                column=col_index["QuestionType"] + 1,
                value=row_data.question_type,
            )
            ws.cell(
                row=new_row_idx,
                column=col_index["ExportTag"] + 1,
                value=row_data.export_tag,
            )
            if row_data.code is not None:
                ws.cell(
                    row=new_row_idx, column=col_index["Code"] + 1, value=row_data.code
                )

            label_cell = ws.cell(
                row=new_row_idx,
                column=col_index[base_label_col] + 1,
                value=row_data.label_en_md,
            )
            is_html_cell = ws.cell(
                row=new_row_idx,
                column=col_index[base_label_html_col] + 1,
                value=bool(row_data.label_en_is_html),
            )
            meta_cell = ws.cell(row=new_row_idx, column=col_index["MetaComment"] + 1)
            if row_data.externally_managed_by:
                meta_cell.value = _externally_managed_note(
                    row_data.externally_managed_by
                )

            row_idx = new_row_idx

        q_json = questions.get(qid, {}) if isinstance(questions, dict) else {}
        language_blocks = q_json.get("Language") or {}
        section = "Choices"
        if str(row_data.question_type or "").strip().lower() == "matrix":
            section = "Answers"

        for lang_code, (md_col, html_col) in label_lang_columns.items():
            if lang_code == _normalize_language_code(base_language):
                continue
            lang_block = _lookup_language_block(language_blocks, lang_code)
            if not lang_block:
                continue
            items = lang_block.get(section) if isinstance(lang_block, dict) else None
            if not isinstance(items, dict):
                continue
            entry = items.get(str(choice_id))
            if not isinstance(entry, dict):
                continue
            lang_display = entry.get("Display")
            text_md, is_html = _metadata_cell_value(lang_display)
            if not text_md:
                continue
            lang_cell = ws.cell(row=row_idx, column=col_index[md_col] + 1)
            if lang_cell.value is not None and str(lang_cell.value).strip() != "":
                continue
            lang_cell.value = text_md
            if html_col:
                ws.cell(
                    row=row_idx,
                    column=col_index[html_col] + 1,
                    value=bool(is_html),
                )


def _init_subitems_sheet(
    wb: Workbook,
    subitems_map: Dict[Tuple[str, str, str], SubitemRow],
    survey_payload: dict,
    *,
    languages: Sequence[str] | None = None,
    base_language: str = "EN",
) -> None:
    ws = _get_or_create_sheet(wb, SUBITEMS_SHEET)
    survey_id = str((survey_payload.get("result") or {}).get("SurveyID") or "").strip()
    base_suffix = _language_suffix(base_language) or "en"
    base_label_col = f"Label_{base_suffix}_MD"
    base_label_html_col = f"Label_{base_suffix}_IsHTML"
    label_columns = _translation_columns(
        "Label", languages, base_language=base_language
    )
    required_cols = [
        "SurveyID",
        "QID",
        "AnswerId",
        "Field",
        "QuestionType",
        "ExportTag",
        *label_columns,
    ]
    col_index = _ensure_columns(ws, required_cols)

    headers, data_rows = _iter_sheet_rows(ws)
    label_lang_columns: dict[str, tuple[str, str | None]] = {}
    for name in headers:
        header = str(name or "")
        if not header.startswith("Label_") or not header.endswith("_MD"):
            continue
        suffix = header[len("Label_") : -len("_MD")]
        lang_code = _language_from_suffix(suffix)
        is_html_name = f"Label_{suffix}_IsHTML"
        label_lang_columns[lang_code] = (
            header,
            is_html_name if is_html_name in col_index else None,
        )

    qid_col = col_index["QID"]
    answer_col = col_index["AnswerId"]
    field_col = col_index.get("Field")
    existing_rows: Dict[Tuple[str, str, str], int] = {}
    for idx, row in enumerate(data_rows, start=2):
        qid_val = row[qid_col].value
        answer_val = row[answer_col].value
        if qid_val is None or answer_val is None:
            continue
        field_val = row[field_col].value if field_col is not None else "Answer"
        field = _normalize_subitem_field(field_val)
        key = (str(qid_val).strip(), field, str(answer_val).strip())
        existing_rows[key] = idx

    questions = survey_payload.get("result", {}).get("Questions", {})

    for key, row_data in subitems_map.items():
        qid, field_key, answer_id = key
        field_key = _normalize_subitem_field(field_key)
        row_key = (qid, field_key, answer_id)
        if row_key in existing_rows:
            row_idx = existing_rows[row_key]
            ws.cell(
                row=row_idx, column=col_index["SurveyID"] + 1, value=row_data.survey_id
            )
            ws.cell(row=row_idx, column=col_index["QID"] + 1, value=row_data.qid)
            ws.cell(
                row=row_idx, column=col_index["AnswerId"] + 1, value=row_data.answer_id
            )
            if field_col is not None:
                field_cell = ws.cell(row=row_idx, column=col_index["Field"] + 1)
                if field_cell.value is None or str(field_cell.value).strip() == "":
                    field_cell.value = field_key
            ws.cell(
                row=row_idx,
                column=col_index["QuestionType"] + 1,
                value=row_data.question_type,
            )
            ws.cell(
                row=row_idx,
                column=col_index["ExportTag"] + 1,
                value=row_data.export_tag,
            )
            label_cell = ws.cell(row=row_idx, column=col_index[base_label_col] + 1)
            is_html_cell = ws.cell(
                row=row_idx, column=col_index[base_label_html_col] + 1
            )
            if (
                label_cell.value is None or str(label_cell.value).strip() == ""
            ) and row_data.label_en_md:
                label_cell.value = row_data.label_en_md
            is_html_cell.value = bool(row_data.label_en_is_html)
        else:
            new_row_idx = ws.max_row + 1
            existing_rows[row_key] = new_row_idx
            ws.cell(
                row=new_row_idx,
                column=col_index["SurveyID"] + 1,
                value=row_data.survey_id,
            )
            ws.cell(row=new_row_idx, column=col_index["QID"] + 1, value=row_data.qid)
            ws.cell(
                row=new_row_idx,
                column=col_index["AnswerId"] + 1,
                value=row_data.answer_id,
            )
            ws.cell(
                row=new_row_idx,
                column=col_index["Field"] + 1,
                value=field_key,
            )
            ws.cell(
                row=new_row_idx,
                column=col_index["QuestionType"] + 1,
                value=row_data.question_type,
            )
            ws.cell(
                row=new_row_idx,
                column=col_index["ExportTag"] + 1,
                value=row_data.export_tag,
            )
            ws.cell(
                row=new_row_idx,
                column=col_index[base_label_col] + 1,
                value=row_data.label_en_md,
            )
            ws.cell(
                row=new_row_idx,
                column=col_index[base_label_html_col] + 1,
                value=bool(row_data.label_en_is_html),
            )

            row_idx = new_row_idx

        q_json = questions.get(qid, {}) if isinstance(questions, dict) else {}
        language_blocks = q_json.get("Language") or {}
        section = "Answers"
        if field_key == "Label":
            section = "Labels"
        elif str(row_data.question_type or "").strip().lower() == "matrix":
            section = "Choices"

        for lang_code, (md_col, html_col) in label_lang_columns.items():
            if lang_code == _normalize_language_code(base_language):
                continue
            lang_block = _lookup_language_block(language_blocks, lang_code)
            if not lang_block:
                continue
            items = lang_block.get(section) if isinstance(lang_block, dict) else None
            if not isinstance(items, dict):
                continue
            entry = items.get(str(answer_id))
            if not isinstance(entry, dict):
                continue
            lang_display = entry.get("Display")
            text_md, is_html = _metadata_cell_value(lang_display)
            if not text_md:
                continue
            lang_cell = ws.cell(row=row_idx, column=col_index[md_col] + 1)
            if lang_cell.value is not None and str(lang_cell.value).strip() != "":
                continue
            lang_cell.value = text_md
            if html_col:
                ws.cell(
                    row=row_idx,
                    column=col_index[html_col] + 1,
                    value=bool(is_html),
                )

    # Backfill label endpoints for slider/scale questions (Field=Label)
    for qid, q in questions.items():
        labels = q.get("Labels") or {}
        if not labels:
            continue
        tag = q.get("DataExportTag") or ""
        qtype = q.get("QuestionType") or ""
        language_blocks = q.get("Language") or {}

        for label_id, label in labels.items():
            label_key = (qid, "Label", str(label_id))
            if label_key in existing_rows:
                row_idx = existing_rows[label_key]
            else:
                row_idx = ws.max_row + 1
                existing_rows[label_key] = row_idx
                ws.cell(row=row_idx, column=col_index["SurveyID"] + 1, value=survey_id)
                ws.cell(row=row_idx, column=col_index["QID"] + 1, value=qid)
                ws.cell(
                    row=row_idx,
                    column=col_index["AnswerId"] + 1,
                    value=str(label_id),
                )
                ws.cell(
                    row=row_idx,
                    column=col_index["Field"] + 1,
                    value="Label",
                )
                ws.cell(
                    row=row_idx,
                    column=col_index["QuestionType"] + 1,
                    value=qtype,
                )
                ws.cell(
                    row=row_idx,
                    column=col_index["ExportTag"] + 1,
                    value=tag,
                )

            # Always enforce Field=Label for label rows
            if "Field" in col_index:
                ws.cell(row=row_idx, column=col_index["Field"] + 1, value="Label")

            base_display = label.get("Display")
            base_text = str(base_display) if base_display is not None else ""
            if base_text:
                if is_markdown_safe_html(base_text):
                    base_md, base_is_html = html_to_md(base_text), False
                elif should_treat_as_html(base_text):
                    base_md, base_is_html = normalize_text(base_text), True
                else:
                    base_md, base_is_html = html_to_md(base_text), False

                base_cell = ws.cell(row=row_idx, column=col_index[base_label_col] + 1)
                base_html_cell = ws.cell(
                    row=row_idx, column=col_index[base_label_html_col] + 1
                )
                if base_cell.value is None or str(base_cell.value).strip() == "":
                    base_cell.value = base_md
                    base_html_cell.value = bool(base_is_html)

            # Non-base languages: fill only when columns exist + cell empty
            for lang_code, (md_col, html_col) in label_lang_columns.items():
                if lang_code == _normalize_language_code(base_language):
                    continue
                lang_block = _lookup_language_block(language_blocks, lang_code)
                if not lang_block:
                    continue
                lang_labels = lang_block.get("Labels") or {}
                lang_entry = lang_labels.get(str(label_id)) or {}
                lang_display = lang_entry.get("Display")
                if lang_display is None:
                    continue
                lang_text = str(lang_display)
                lang_cell = ws.cell(row=row_idx, column=col_index[md_col] + 1)
                if lang_cell.value is not None and str(lang_cell.value).strip() != "":
                    continue
                if is_markdown_safe_html(lang_text):
                    lang_md, lang_is_html = html_to_md(lang_text), False
                elif should_treat_as_html(lang_text):
                    lang_md, lang_is_html = normalize_text(lang_text), True
                else:
                    lang_md, lang_is_html = html_to_md(lang_text), False
                lang_cell.value = lang_md
                if html_col:
                    ws.cell(
                        row=row_idx,
                        column=col_index[html_col] + 1,
                        value=bool(lang_is_html),
                    )


def _metadata_language_list(
    survey_payload: dict,
    languages: Sequence[str] | None,
) -> List[str]:
    result = survey_payload.get("result", {})
    options = result.get("SurveyOptions", {}) or {}
    base = _normalize_language_code(options.get("SurveyLanguage") or "")
    available = options.get("AvailableLanguages") or []
    meta = options.get("MetaDataTranslations") or {}

    lang_list = _normalize_language_list(languages or [])
    if not lang_list:
        lang_list = _normalize_language_list(available)
    if isinstance(meta, dict):
        meta_langs = [str(k) for k in meta.keys()]
        lang_list = _normalize_language_list(list(lang_list) + meta_langs)
    if base and base not in lang_list:
        lang_list = [base] + lang_list
    return lang_list


def _metadata_cell_value(text_html: str | None) -> tuple[str | None, bool]:
    if text_html is None or str(text_html).strip() == "":
        return None, False
    raw = str(text_html)
    if is_markdown_safe_html(raw):
        return html_to_md(raw), False
    if should_treat_as_html(raw):
        return normalize_text(raw), True
    return html_to_md(raw), False


def _init_survey_metadata_sheet(
    wb: Workbook,
    survey_payload: dict,
    *,
    languages: Sequence[str] | None = None,
) -> None:
    ws = _get_or_create_sheet(wb, SURVEY_METADATA_SHEET)
    result = survey_payload.get("result", {}) or {}
    options = result.get("SurveyOptions", {}) or {}
    base_lang = _normalize_language_code(options.get("SurveyLanguage") or "")
    meta_translations = options.get("MetaDataTranslations") or {}

    metadata_cols = _metadata_columns()
    required_cols = ["SurveyID", "Language", *metadata_cols]
    col_index = _ensure_columns(ws, required_cols)

    headers, data_rows = _iter_sheet_rows(ws)
    lang_idx = col_index["Language"]
    existing_rows: Dict[str, int] = {}
    for idx, row in enumerate(data_rows, start=2):
        lang_val = row[lang_idx].value
        lang = _normalize_language_code(str(lang_val or ""))
        if lang:
            existing_rows[lang] = idx

    languages = _metadata_language_list(survey_payload, languages)
    survey_id = str(result.get("SurveyID") or "")

    for lang in languages:
        row_idx = existing_rows.get(lang)
        if row_idx is None:
            row_idx = ws.max_row + 1
            ws.cell(row=row_idx, column=col_index["SurveyID"] + 1, value=survey_id)
            ws.cell(row=row_idx, column=col_index["Language"] + 1, value=lang)
        else:
            ws.cell(row=row_idx, column=col_index["SurveyID"] + 1, value=survey_id)
            ws.cell(row=row_idx, column=col_index["Language"] + 1, value=lang)

        entry = {}
        if isinstance(meta_translations, dict):
            entry = (
                meta_translations.get(lang)
                or meta_translations.get(lang.lower())
                or meta_translations.get(lang.upper())
                or {}
            )

        for key in SURVEY_METADATA_KEYS:
            if lang == base_lang:
                raw_value = result.get(key)
            else:
                raw_value = entry.get(key) if isinstance(entry, dict) else None
                if (
                    raw_value is None
                    and key == "SurveyDescription"
                    and isinstance(entry, dict)
                ):
                    raw_value = entry.get("SurveyMetaDescription")
            text_md, is_html = _metadata_cell_value(raw_value)

            md_col = f"{key}_MD"
            html_col = f"{key}_IsHTML"
            md_idx = col_index.get(md_col)
            html_idx = col_index.get(html_col)
            if md_idx is not None:
                cell = ws.cell(row=row_idx, column=md_idx + 1)
                if (cell.value is None or str(cell.value).strip() == "") and text_md:
                    cell.value = text_md
            if html_idx is not None:
                cell = ws.cell(row=row_idx, column=html_idx + 1)
                if cell.value is None or str(cell.value).strip() == "":
                    cell.value = bool(is_html)


def _init_embedded_data_sheet(wb: Workbook, rows: List[EmbeddedDataRow]) -> None:
    ws = _get_or_create_sheet(wb, EMBEDDED_DATA_SHEET)
    required_cols = [
        "SurveyID",
        "FlowID",
        "FlowOrder",
        "Field",
        "Value",
        "Type",
        "WrittenByQIDs",
    ]
    col_index = _ensure_columns(ws, required_cols)

    headers, data_rows = _iter_sheet_rows(ws)
    field_col = col_index["Field"]
    flow_col = col_index["FlowID"]

    seen_keys: set[Tuple[str, str]] = set()
    duplicate_rows: List[int] = []
    for idx, row in enumerate(data_rows, start=2):
        field_val = row[field_col].value
        if field_val is None:
            continue
        field = str(field_val).strip()
        if not field:
            continue
        flow_val = row[flow_col].value
        flow_id = str(flow_val).strip() if flow_val is not None else ""
        key = (flow_id, field)
        if key in seen_keys:
            duplicate_rows.append(idx)
            continue
        seen_keys.add(key)

    for row_idx in sorted(duplicate_rows, reverse=True):
        ws.delete_rows(row_idx, 1)

    headers, data_rows = _iter_sheet_rows(ws)
    existing_rows: Dict[Tuple[str, str], int] = {}
    fallback_rows: Dict[str, int] = {}
    for idx, row in enumerate(data_rows, start=2):
        field_val = row[field_col].value
        if field_val is None:
            continue
        field = str(field_val).strip()
        if not field:
            continue
        flow_val = row[flow_col].value
        flow_id = str(flow_val).strip() if flow_val is not None else ""
        key = (flow_id, field)
        existing_rows[key] = idx
        if not flow_id and field not in fallback_rows:
            fallback_rows[field] = idx

    for row_data in rows:
        flow_id = row_data.flow_id or ""
        key = (flow_id, row_data.field)
        row_idx = existing_rows.get(key)
        if row_idx is None and flow_id and row_data.field in fallback_rows:
            row_idx = fallback_rows[row_data.field]
        if row_idx is not None:
            ws.cell(
                row=row_idx, column=col_index["SurveyID"] + 1, value=row_data.survey_id
            )
            ws.cell(row=row_idx, column=col_index["FlowID"] + 1, value=flow_id or None)
            ws.cell(
                row=row_idx,
                column=col_index["FlowOrder"] + 1,
                value=row_data.flow_order,
            )
            ws.cell(row=row_idx, column=col_index["Field"] + 1, value=row_data.field)
            ws.cell(row=row_idx, column=col_index["Type"] + 1, value=row_data.ed_type)
            ws.cell(
                row=row_idx,
                column=col_index["WrittenByQIDs"] + 1,
                value=row_data.written_by_qids or "",
            )
            value_cell = ws.cell(row=row_idx, column=col_index["Value"] + 1)
            if value_cell.value is None or str(value_cell.value).strip() == "":
                value_cell.value = (
                    row_data.value
                    if row_data.value is not None
                    else EMBEDDED_EMPTY_VALUE
                )
        else:
            new_row_idx = ws.max_row + 1
            ws.cell(
                row=new_row_idx,
                column=col_index["SurveyID"] + 1,
                value=row_data.survey_id,
            )
            ws.cell(
                row=new_row_idx, column=col_index["FlowID"] + 1, value=flow_id or None
            )
            ws.cell(
                row=new_row_idx,
                column=col_index["FlowOrder"] + 1,
                value=row_data.flow_order,
            )
            ws.cell(
                row=new_row_idx, column=col_index["Field"] + 1, value=row_data.field
            )
            ws.cell(
                row=new_row_idx, column=col_index["Value"] + 1, value=row_data.value
            )
            if row_data.value is None:
                ws.cell(
                    row=new_row_idx,
                    column=col_index["Value"] + 1,
                    value=EMBEDDED_EMPTY_VALUE,
                )
            ws.cell(
                row=new_row_idx, column=col_index["Type"] + 1, value=row_data.ed_type
            )
            ws.cell(
                row=new_row_idx,
                column=col_index["WrittenByQIDs"] + 1,
                value=row_data.written_by_qids or "",
            )


_HTML_FILL = PatternFill(fill_type="solid", fgColor="FFFFF4B2")
_DIRTY_FILL = PatternFill(fill_type="solid", fgColor="FFF4B084")


def _make_bold(cell) -> None:
    """Make a cell bold."""
    if cell.font:
        cell.font = Font(
            bold=True, name=cell.font.name, size=cell.font.size, color=cell.font.color
        )
    else:
        cell.font = Font(bold=True)


def _wrap_column(ws: Worksheet, header_name: str) -> None:
    headers, _ = _iter_sheet_rows(ws)
    if not headers:
        return
    if header_name not in headers:
        return
    col_idx = headers.index(header_name) + 1
    for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
        cell = row[0]
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _autofit_rows(ws: Worksheet) -> None:
    """Enable auto-fit for all rows by clearing height settings."""
    # Clear all row heights to enable auto-fit
    for row_idx in range(1, ws.max_row + 1):
        ws.row_dimensions[row_idx].height = None


def _apply_boolean_validation(ws: Worksheet, header_name: str) -> None:
    headers, _ = _iter_sheet_rows(ws)
    if not headers or header_name not in headers:
        return
    col_idx = headers.index(header_name) + 1
    col_letter = get_column_letter(col_idx)
    max_row = ws.max_row
    if max_row <= 1:
        return
    dv = DataValidation(type="list", formula1='"TRUE,FALSE"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"{col_letter}2:{col_letter}{max_row}")


def _sort_sheet_by_qid_and_id(ws: Worksheet, id_header: str) -> None:
    """Sort a sheet by (QID, id_header) in-place.

    This keeps the header row, reorders all data rows, and preserves cell values.
    """

    headers, data_rows = _iter_sheet_rows(ws)
    if not headers:
        return
    if "QID" not in headers or id_header not in headers:
        return

    qid_idx = headers.index("QID")
    id_idx = headers.index(id_header)
    field_idx = headers.index("Field") if "Field" in headers else None

    def sort_key(row):
        qid_val = row[qid_idx].value
        id_val = row[id_idx].value
        qid = str(qid_val or "")
        field_val = row[field_idx].value if field_idx is not None else "Answer"
        field = _normalize_subitem_field(field_val)
        id_str = str(id_val or "")
        try:
            id_num = int(id_str)
        except ValueError:
            id_num = None
        # Numeric IDs first, then non-numeric, all grouped by QID.
        id_key = (0, id_num) if id_num is not None else (1, id_str)
        if field == "Answer":
            field_order = 0
        elif field == "Label":
            field_order = 1
        else:
            field_order = 2
        return (qid, field_order, field, id_key)

    # Extract current values so we can rewrite the rows after sorting.
    rows_with_values = [[cell.value for cell in row] for row in data_rows]
    if not rows_with_values:
        return

    # Pair values with their original sort key.
    paired = [
        (sort_key(data_rows[i]), rows_with_values[i])
        for i in range(len(rows_with_values))
    ]
    paired.sort(key=lambda x: x[0])

    # Clear existing data rows (keep header).
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)

    # Append rows back in sorted order.
    for _, values in paired:
        ws.append(values)


def _sort_sheet_by_flow_order(ws: Worksheet) -> None:
    """Sort Embedded_Data by FlowOrder and Field (JS-only rows last)."""

    headers, data_rows = _iter_sheet_rows(ws)
    if not headers:
        return
    if "FlowOrder" not in headers or "Field" not in headers:
        return

    order_idx = headers.index("FlowOrder")
    field_idx = headers.index("Field")
    flow_idx = headers.index("FlowID") if "FlowID" in headers else None

    def sort_key(row):
        order_val = row[order_idx].value
        try:
            order = int(order_val)
        except (TypeError, ValueError):
            order = 0
        field = str(row[field_idx].value or "")
        flow_id = str(row[flow_idx].value or "") if flow_idx is not None else ""
        return (order == 0, order, flow_id, field)

    rows_with_values = [[cell.value for cell in row] for row in data_rows]
    if not rows_with_values:
        return

    paired = [
        (sort_key(data_rows[i]), rows_with_values[i])
        for i in range(len(rows_with_values))
    ]
    paired.sort(key=lambda x: x[0])

    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)

    for _, values in paired:
        ws.append(values)


def _format_questions_sheet(ws: Worksheet) -> None:
    # Header & system columns
    headers, _ = _iter_sheet_rows(ws)
    if not headers:
        return
    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    system_headers = {
        "SurveyID",
        "QID",
        "BlockName",
        "QuestionType",
        "DataExportTag",
        "OptionsPreview",
        "SubitemsPreview",
    }

    for cell in header_row:
        name = cell.value or ""
        if name in system_headers:
            _make_bold(cell)

    # Make system-owned columns bold in body rows
    system_indices = [headers.index(h) + 1 for h in system_headers if h in headers]
    for row in ws.iter_rows(min_row=2):
        for idx in system_indices:
            if idx <= len(row):
                _make_bold(row[idx - 1])

    # Apply vertical center and horizontal left alignment to all cells
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for cell in row:
            # Default alignment: vertically centered, horizontally left
            cell.alignment = Alignment(
                vertical="center", horizontal="left", wrap_text=False
            )

    # Wrap long text (question text and options/subitems preview)
    for name in headers:
        if str(name).startswith("Text_") and str(name).endswith("_MD"):
            _wrap_column(ws, str(name))
    _wrap_column(ws, "OptionsPreview")
    _wrap_column(ws, "SubitemsPreview")

    # Auto-fit row heights
    _autofit_rows(ws)

    # Boolean validations
    for name in headers:
        if str(name).startswith("Text_") and str(name).endswith("_IsHTML"):
            _apply_boolean_validation(ws, str(name))
    _apply_boolean_validation(ws, "InPre")
    _apply_boolean_validation(ws, "InPost")

    # Clear any existing conditional formatting (we'll reapply ours)
    try:
        ws.conditional_formatting.clear()
    except AttributeError:
        # Older openpyxl: recreate a fresh container by assigning an empty list
        ws.conditional_formatting._cf_rules = {}

    max_row = ws.max_row
    # Conditional formatting: highlight HTML question text when Text_*_IsHTML is TRUE
    for name in headers:
        if str(name).startswith("Text_") and str(name).endswith("_IsHTML"):
            suffix = str(name)[len("Text_") : -len("_IsHTML")]
            text_name = f"Text_{suffix}_MD"
            if text_name not in headers or max_row < 2:
                continue
            html_idx = headers.index(name) + 1
            text_idx = headers.index(text_name) + 1
            html_col = get_column_letter(html_idx)
            text_col = get_column_letter(text_idx)
            formula = f"=${html_col}2=TRUE"
            rule = FormulaRule(formula=[formula], fill=_HTML_FILL)
            ws.conditional_formatting.add(f"{text_col}2:{text_col}{max_row}", rule)

    # Conditional formatting: highlight dirty question text when Dirty == 'Y'
    if "Dirty" in headers and max_row >= 2:
        dirty_idx = headers.index("Dirty") + 1
        dirty_col = get_column_letter(dirty_idx)
        for name in headers:
            if str(name).startswith("Text_") and str(name).endswith("_MD"):
                text_idx = headers.index(name) + 1
                text_col = get_column_letter(text_idx)
                formula = f'=${dirty_col}2="Y"'
                rule = FormulaRule(formula=[formula], fill=_DIRTY_FILL)
                ws.conditional_formatting.add(f"{text_col}2:{text_col}{max_row}", rule)

    # Add a simple Excel table style
    max_row = ws.max_row
    max_col = ws.max_column
    if max_row >= 2 and max_col >= 1:
        table = Table(
            displayName="QuestionsTable",
            ref=f"A1:{get_column_letter(max_col)}{max_row}",
        )
        style = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        table.tableStyleInfo = style
        # Always refresh the table so its ref/columns cover all rows/columns.
        if "QuestionsTable" in ws._tables:
            del ws._tables["QuestionsTable"]
        ws.add_table(table)

    # Fixed column widths based on header names (tuned for NEWSFLOWS)
    widths = {
        "SurveyID": 18.0,
        "QID": 7.0,
        "BlockName": 19.0,
        "QuestionType": 14.5,
        "DataExportTag": 19.0,
        "QuestionKey": 14.0,
        "OptionsPreview": 60.0,
        "SubitemsPreview": 60.0,
        "InPre": 8.0,
        "InPost": 8.0,
    }
    for idx, name in enumerate(headers, start=1):
        key = str(name or "")
        w = widths.get(key)
        if w:
            ws.column_dimensions[get_column_letter(idx)].width = w
            continue
        if key.startswith("Text_") and key.endswith("_MD"):
            ws.column_dimensions[get_column_letter(idx)].width = 76.0
        elif key.startswith("Text_") and key.endswith("_IsHTML"):
            ws.column_dimensions[get_column_letter(idx)].width = 16.0


def _format_options_sheet(ws: Worksheet) -> None:
    headers, _ = _iter_sheet_rows(ws)
    if not headers:
        return
    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    system_headers = {
        "SurveyID",
        "QID",
        "ChoiceId",
        "QuestionType",
        "ExportTag",
        "Code",
    }

    for cell in header_row:
        name = cell.value or ""
        if name in system_headers:
            _make_bold(cell)

    system_indices = [headers.index(h) + 1 for h in system_headers if h in headers]
    for row in ws.iter_rows(min_row=2):
        for idx in system_indices:
            if idx <= len(row):
                _make_bold(row[idx - 1])

    # Apply vertical center and horizontal left alignment to all cells
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for cell in row:
            cell.alignment = Alignment(
                vertical="center", horizontal="left", wrap_text=False
            )

    # Wrap long text (option labels)
    for name in headers:
        if str(name).startswith("Label_") and str(name).endswith("_MD"):
            _wrap_column(ws, str(name))

    # Auto-fit row heights
    _autofit_rows(ws)

    for name in headers:
        if str(name).startswith("Label_") and str(name).endswith("_IsHTML"):
            _apply_boolean_validation(ws, str(name))

    # Clear any existing conditional formatting
    try:
        ws.conditional_formatting.clear()
    except AttributeError:
        ws.conditional_formatting._cf_rules = {}

    max_row = ws.max_row
    # Conditional formatting: highlight HTML labels
    for name in headers:
        if str(name).startswith("Label_") and str(name).endswith("_IsHTML"):
            suffix = str(name)[len("Label_") : -len("_IsHTML")]
            text_name = f"Label_{suffix}_MD"
            if text_name not in headers or max_row < 2:
                continue
            html_idx = headers.index(name) + 1
            text_idx = headers.index(text_name) + 1
            html_col = get_column_letter(html_idx)
            text_col = get_column_letter(text_idx)
            formula = f"=${html_col}2=TRUE"
            rule = FormulaRule(formula=[formula], fill=_HTML_FILL)
            ws.conditional_formatting.add(f"{text_col}2:{text_col}{max_row}", rule)

    # Conditional formatting: highlight dirty option labels
    if "Dirty" in headers and max_row >= 2:
        dirty_idx = headers.index("Dirty") + 1
        dirty_col = get_column_letter(dirty_idx)
        for name in headers:
            if str(name).startswith("Label_") and str(name).endswith("_MD"):
                text_idx = headers.index(name) + 1
                text_col = get_column_letter(text_idx)
                formula = f'=${dirty_col}2="Y"'
                rule = FormulaRule(formula=[formula], fill=_DIRTY_FILL)
                ws.conditional_formatting.add(f"{text_col}2:{text_col}{max_row}", rule)

    max_row = ws.max_row
    max_col = ws.max_column
    if max_row >= 2 and max_col >= 1:
        table = Table(
            displayName="OptionsTable",
            ref=f"A1:{get_column_letter(max_col)}{max_row}",
        )
        style = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        table.tableStyleInfo = style
        # Always refresh the table so its ref covers all rows/columns.
        if "OptionsTable" in ws._tables:
            del ws._tables["OptionsTable"]
        ws.add_table(table)
    # Fixed widths based on header names
    widths = {
        "SurveyID": 20.0,
        "QID": 7.0,
        "ChoiceId": 10.0,
        "QuestionType": 14.0,
        "ExportTag": 19.0,
        "Code": 6.0,
        "MetaComment": 42.0,
    }
    for idx, name in enumerate(headers, start=1):
        key = str(name or "")
        w = widths.get(key)
        if w:
            ws.column_dimensions[get_column_letter(idx)].width = w
            continue
        if key.startswith("Label_") and key.endswith("_MD"):
            ws.column_dimensions[get_column_letter(idx)].width = 40.0
        elif key.startswith("Label_") and key.endswith("_IsHTML"):
            ws.column_dimensions[get_column_letter(idx)].width = 17.0


def _format_subitems_sheet(ws: Worksheet) -> None:
    headers, _ = _iter_sheet_rows(ws)
    if not headers:
        return
    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    system_headers = {
        "SurveyID",
        "QID",
        "AnswerId",
        "Field",
        "QuestionType",
        "ExportTag",
    }

    for cell in header_row:
        name = cell.value or ""
        if name in system_headers:
            _make_bold(cell)

    system_indices = [headers.index(h) + 1 for h in system_headers if h in headers]
    for row in ws.iter_rows(min_row=2):
        for idx in system_indices:
            if idx <= len(row):
                _make_bold(row[idx - 1])

    # Apply vertical center and horizontal left alignment to all cells
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for cell in row:
            cell.alignment = Alignment(
                vertical="center", horizontal="left", wrap_text=False
            )

    for name in headers:
        if str(name).startswith("Label_") and str(name).endswith("_MD"):
            _wrap_column(ws, str(name))

    # Auto-fit row heights
    _autofit_rows(ws)

    for name in headers:
        if str(name).startswith("Label_") and str(name).endswith("_IsHTML"):
            _apply_boolean_validation(ws, str(name))

    max_row = ws.max_row
    max_col = ws.max_column
    if max_row >= 2 and max_col >= 1:
        table = Table(
            displayName="SubitemsTable",
            ref=f"A1:{get_column_letter(max_col)}{max_row}",
        )
        style = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        table.tableStyleInfo = style
        # Always refresh the table so its ref covers all rows/columns.
        if "SubitemsTable" in ws._tables:
            del ws._tables["SubitemsTable"]
        ws.add_table(table)

    widths = {
        "SurveyID": 20.0,
        "QID": 7.0,
        "AnswerId": 10.0,
        "Field": 9.0,
        "QuestionType": 14.0,
        "ExportTag": 19.0,
    }
    for idx, name in enumerate(headers, start=1):
        key = str(name or "")
        w = widths.get(key)
        if w:
            ws.column_dimensions[get_column_letter(idx)].width = w
            continue
        if key.startswith("Label_") and key.endswith("_MD"):
            ws.column_dimensions[get_column_letter(idx)].width = 40.0
        elif key.startswith("Label_") and key.endswith("_IsHTML"):
            ws.column_dimensions[get_column_letter(idx)].width = 17.0


def _format_survey_metadata_sheet(ws: Worksheet) -> None:
    headers, _ = _iter_sheet_rows(ws)
    if not headers:
        return
    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    system_headers = {"SurveyID", "Language"}

    for cell in header_row:
        name = cell.value or ""
        if name in system_headers:
            _make_bold(cell)

    system_indices = [headers.index(h) + 1 for h in system_headers if h in headers]
    for row in ws.iter_rows(min_row=2):
        for idx in system_indices:
            if idx <= len(row):
                _make_bold(row[idx - 1])

    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for cell in row:
            cell.alignment = Alignment(
                vertical="center", horizontal="left", wrap_text=False
            )

    for name in headers:
        if str(name).endswith("_MD"):
            _wrap_column(ws, str(name))

    _autofit_rows(ws)

    for name in headers:
        if str(name).endswith("_IsHTML"):
            _apply_boolean_validation(ws, str(name))

    max_row = ws.max_row
    max_col = ws.max_column
    if max_row >= 2 and max_col >= 1:
        table = Table(
            displayName="SurveyMetadataTable",
            ref=f"A1:{get_column_letter(max_col)}{max_row}",
        )
        style = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        table.tableStyleInfo = style
        if "SurveyMetadataTable" in ws._tables:
            del ws._tables["SurveyMetadataTable"]
        ws.add_table(table)

    widths = {
        "SurveyID": 18.0,
        "Language": 10.0,
    }
    for idx, name in enumerate(headers, start=1):
        key = str(name or "")
        w = widths.get(key)
        if w:
            ws.column_dimensions[get_column_letter(idx)].width = w
        elif key.endswith("_MD"):
            ws.column_dimensions[get_column_letter(idx)].width = 60.0
        elif key.endswith("_IsHTML"):
            ws.column_dimensions[get_column_letter(idx)].width = 18.0


def _format_embedded_data_sheet(ws: Worksheet) -> None:
    headers, _ = _iter_sheet_rows(ws)
    if not headers:
        return
    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    system_headers = {
        "SurveyID",
        "FlowID",
        "FlowOrder",
        "Field",
        "Type",
        "WrittenByQIDs",
    }

    for cell in header_row:
        name = cell.value or ""
        if name in system_headers:
            _make_bold(cell)

    system_indices = [headers.index(h) + 1 for h in system_headers if h in headers]
    for row in ws.iter_rows(min_row=2):
        for idx in system_indices:
            if idx <= len(row):
                _make_bold(row[idx - 1])

    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for cell in row:
            cell.alignment = Alignment(
                vertical="center", horizontal="left", wrap_text=False
            )

    _wrap_column(ws, "Value")
    _wrap_column(ws, "WrittenByQIDs")
    _autofit_rows(ws)

    # Clear any existing conditional formatting (we'll reapply ours)
    try:
        ws.conditional_formatting.clear()
    except AttributeError:
        ws.conditional_formatting._cf_rules = {}

    max_row = ws.max_row
    if "Dirty" in headers and "Value" in headers and max_row >= 2:
        dirty_idx = headers.index("Dirty") + 1
        value_idx = headers.index("Value") + 1
        dirty_col = get_column_letter(dirty_idx)
        value_col = get_column_letter(value_idx)
        formula = f'=${dirty_col}2="Y"'
        rule = FormulaRule(formula=[formula], fill=_DIRTY_FILL)
        ws.conditional_formatting.add(f"{value_col}2:{value_col}{max_row}", rule)

    max_col = ws.max_column
    if max_row >= 2 and max_col >= 1:
        table = Table(
            displayName="EmbeddedDataTable",
            ref=f"A1:{get_column_letter(max_col)}{max_row}",
        )
        style = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        table.tableStyleInfo = style
        if "EmbeddedDataTable" in ws._tables:
            del ws._tables["EmbeddedDataTable"]
        ws.add_table(table)

    widths = {
        "SurveyID": 20.0,
        "FlowID": 12.0,
        "FlowOrder": 10.0,
        "Field": 26.0,
        "Value": 30.0,
        "Type": 12.0,
        "WrittenByQIDs": 26.0,
        "Dirty": 8.0,
    }
    for idx, name in enumerate(headers, start=1):
        w = widths.get(name)
        if w:
            ws.column_dimensions[get_column_letter(idx)].width = w


def _populate_system_sheet(wb: Workbook, survey_id: str, survey_payload: dict) -> None:
    """Populate a System sheet with non-editable info such as Timing/meta options."""

    ws = _get_or_create_sheet(wb, SYSTEM_SHEET)
    ws.title = SYSTEM_SHEET

    questions = survey_payload.get("result", {}).get("Questions", {})
    headers = [
        "SurveyID",
        "QID",
        "QuestionType",
        "DataExportTag",
        "ChoiceId",
        "Display",
    ]
    ws.delete_rows(1, ws.max_row)
    ws.append(headers)

    for qid, q in questions.items():
        qtype = q.get("QuestionType")
        if qtype != "Timing":
            continue
        tag = q.get("DataExportTag") or ""
        choices = q.get("Choices") or {}
        for choice_id, choice in choices.items():
            display = choice.get("Display") or ""
            ws.append(
                [
                    survey_id,
                    qid,
                    qtype,
                    tag,
                    choice_id,
                    display,
                ]
            )

    # Make header row bold
    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    for cell in header_row:
        _make_bold(cell)

    max_row = ws.max_row
    max_col = ws.max_column
    if max_row >= 2 and max_col >= 1:
        table = Table(
            displayName="SystemTable",
            ref=f"A1:{get_column_letter(max_col)}{max_row}",
        )
        style = TableStyleInfo(
            name="TableStyleLight9",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        table.tableStyleInfo = style
        # Always refresh the table so its ref covers all rows/columns.
        if "SystemTable" in ws._tables:
            del ws._tables["SystemTable"]
        ws.add_table(table)


def _find_base_text_col(headers: List[str], prefix: str) -> tuple[str, str]:
    """Find the first ``{prefix}_*_MD`` column and its ``_IsHTML`` companion.

    Returns ``(md_col, html_col)`` — e.g. ``("Text_cs_MD", "Text_cs_IsHTML")``.
    Falls back to ``{prefix}_en_MD`` when no match is found.
    """
    fallback_md = f"{prefix}_en_MD"
    fallback_html = f"{prefix}_en_IsHTML"
    for h in headers:
        if h.startswith(f"{prefix}_") and h.endswith("_MD"):
            suffix = h[len(prefix) + 1 : -len("_MD")]
            return h, f"{prefix}_{suffix}_IsHTML"
    return fallback_md, fallback_html


def load_questions_from_workbook(xlsx_path: Path) -> Dict[str, QuestionRow]:
    """Read QuestionRow objects from an existing workbook.

    Parses the Questions sheet and returns a dictionary mapping each QID to its
    corresponding QuestionRow dataclass. Used by `preview_changes` and
    `apply_changes` to compare Excel wording against the cached survey JSON.

    Args:
        xlsx_path: Path to the workbook created by `qsync init`.

    Returns:
        Mapping of `QID -> QuestionRow`. Each QuestionRow contains:
        - `survey_id`: The Qualtrics survey ID.
        - `qid`: The question ID.
        - `block_name`: The Qualtrics block name.
        - `question_type`: Question type (MC, TE, Matrix, etc.).
        - `data_export_tag`: The DataExportTag / variable name.
        - `question_key`: Optional human-friendly key.
        - `text_en_md`: English wording in Markdown or raw HTML.
        - `text_en_is_html`: True if `text_en_md` is raw HTML.
        - `in_pre`: True if included in pre-treatment survey.
        - `in_post`: True if included in post-treatment survey.

    Raises:
        FileNotFoundError: If the workbook does not exist.
        KeyError: If the Questions sheet is missing.

    Example:
        >>> from pathlib import Path
        >>> from qsync.excel_io import load_questions_from_workbook
        >>> questions = load_questions_from_workbook(Path("excel/SV_xxx.xlsx"))
        >>> for qid, row in questions.items():
        ...     print(f"{qid}: {row.data_export_tag}")
    """

    wb = load_workbook(xlsx_path, data_only=True)
    ws = wb[QUESTION_SHEET]
    headers, data_rows = _iter_sheet_rows(ws)
    idx = {name: i for i, name in enumerate(headers)}

    text_md_col, text_html_col = _find_base_text_col(headers, "Text")

    def _get(row, name, default=None):
        col = idx.get(name)
        if col is None or col >= len(row):
            return default
        cell = row[col]
        return cell.value if cell is not None else default

    result: Dict[str, QuestionRow] = {}
    for row in data_rows:
        qid_val = _get(row, "QID")
        if qid_val is None:
            continue
        qid = str(qid_val).strip()
        if not qid:
            continue
        qr = QuestionRow(
            survey_id=str(_get(row, "SurveyID") or "").strip(),
            qid=qid,
            block_name=str(_get(row, "BlockName") or "").strip(),
            question_type=str(_get(row, "QuestionType") or "").strip(),
            data_export_tag=str(_get(row, "DataExportTag") or "").strip(),
            question_key=str(_get(row, "QuestionKey") or "").strip() or None,
            text_en_md=str(_get(row, text_md_col) or ""),
            text_en_is_html=bool(_get(row, text_html_col) or False),
            in_pre=bool(_get(row, "InPre") or False),
            in_post=bool(_get(row, "InPost") or False),
            # Question wording is always editable via Excel; only options/subitems
            # are treated as externally managed via EXTERNALLY_MANAGED_TAGS.
            externally_managed_by=None,
        )
        result[qid] = qr
    return result


def load_options_from_workbook(xlsx_path: Path) -> Dict[Tuple[str, str], OptionRow]:
    """Read OptionRow objects from an existing workbook.

    Parses the Options sheet and returns a dictionary mapping each (QID, ChoiceId)
    tuple to its corresponding OptionRow dataclass. For Matrix questions, options
    represent the response scale (Answers); for MC/SC questions, options represent
    the choices.

    Args:
        xlsx_path: Path to the workbook created by `qsync init`.

    Returns:
        Mapping of `(QID, ChoiceId) -> OptionRow`. Each OptionRow contains:
        - `survey_id`: The Qualtrics survey ID.
        - `qid`: The question ID.
        - `choice_id`: The choice/answer ID within the question.
        - `question_type`: Question type (MC, Matrix, etc.).
        - `code`: The recode value (if set).
        - `label_en_md`: English label in Markdown or raw HTML.
        - `label_en_is_html`: True if `label_en_md` is raw HTML.
        - `externally_managed_by`: Script path if managed externally (e.g., recognition).

    Raises:
        FileNotFoundError: If the workbook does not exist.

    Example:
        >>> from pathlib import Path
        >>> from qsync.excel_io import load_options_from_workbook
        >>> options = load_options_from_workbook(Path("excel/SV_xxx.xlsx"))
        >>> for (qid, choice_id), row in options.items():
        ...     print(f"{qid}/{choice_id}: {row.label_en_md[:30]}...")
    """

    wb = load_workbook(xlsx_path, data_only=True)
    if OPTIONS_SHEET not in wb.sheetnames:
        return {}
    ws = wb[OPTIONS_SHEET]
    headers, data_rows = _iter_sheet_rows(ws)
    idx = {name: i for i, name in enumerate(headers)}

    label_md_col, label_html_col = _find_base_text_col(headers, "Label")

    def _get(row, name, default=None):
        col = idx.get(name)
        if col is None or col >= len(row):
            return default
        cell = row[col]
        return cell.value if cell is not None else default

    result: Dict[Tuple[str, str], OptionRow] = {}
    for row in data_rows:
        qid_val = _get(row, "QID")
        choice_val = _get(row, "ChoiceId")
        if qid_val is None or choice_val is None:
            continue
        qid = str(qid_val).strip()
        choice_id = str(choice_val).strip()
        if not qid or not choice_id:
            continue
        export_tag = str(_get(row, "ExportTag") or "").strip()
        result[(qid, choice_id)] = OptionRow(
            survey_id=str(_get(row, "SurveyID") or "").strip(),
            qid=qid,
            choice_id=choice_id,
            question_type=str(_get(row, "QuestionType") or "").strip(),
            export_tag=export_tag,
            code=str(_get(row, "Code") or "").strip() or None,
            label_en_md=str(_get(row, label_md_col) or ""),
            label_en_is_html=bool(_get(row, label_html_col) or False),
            externally_managed_by=_is_externally_managed_question(export_tag),
        )
    return result


def load_subitems_from_workbook(xlsx_path: Path) -> Dict[Tuple[str, str], SubitemRow]:
    """Read SubitemRow objects from an existing workbook.

    Parses the Subitems sheet and returns a dictionary mapping each (QID, AnswerId)
    tuple to its corresponding SubitemRow dataclass. For Matrix questions, subitems
    represent the row statements (Choices); for other questions, they represent
    the Answers structure.

    Args:
        xlsx_path: Path to the workbook created by `qsync init`.

    Returns:
        Mapping of `(QID, AnswerId) -> SubitemRow`. Each SubitemRow contains:
        - `survey_id`: The Qualtrics survey ID.
        - `qid`: The question ID.
        - `answer_id`: The subitem/statement ID within the question.
        - `question_type`: Question type (Matrix, Slider, etc.).
        - `label_en_md`: English label in Markdown or raw HTML.
        - `label_en_is_html`: True if `label_en_md` is raw HTML.
        - `field`: Field disambiguator (Answer | Label). Label rows are ignored here.

    Raises:
        FileNotFoundError: If the workbook does not exist.

    Example:
        >>> from pathlib import Path
        >>> from qsync.excel_io import load_subitems_from_workbook
        >>> subitems = load_subitems_from_workbook(Path("excel/SV_xxx.xlsx"))
        >>> for (qid, answer_id), row in subitems.items():
        ...     print(f"{qid}/{answer_id}: {row.label_en_md[:30]}...")
    """

    wb = load_workbook(xlsx_path, data_only=True)
    if SUBITEMS_SHEET not in wb.sheetnames:
        return {}
    ws = wb[SUBITEMS_SHEET]
    headers, data_rows = _iter_sheet_rows(ws)
    idx = {name: i for i, name in enumerate(headers)}

    label_md_col, label_html_col = _find_base_text_col(headers, "Label")

    def _get(row, name, default=None):
        col = idx.get(name)
        if col is None or col >= len(row):
            return default
        cell = row[col]
        return cell.value if cell is not None else default

    result: Dict[Tuple[str, str], SubitemRow] = {}
    for row in data_rows:
        qid_val = _get(row, "QID")
        answer_val = _get(row, "AnswerId")
        if qid_val is None or answer_val is None:
            continue
        field_val = _get(row, "Field")
        field = _normalize_subitem_field(field_val)
        if field == "Label":
            continue
        qid = str(qid_val).strip()
        answer_id = str(answer_val).strip()
        if not qid or not answer_id:
            continue
        result[(qid, answer_id)] = SubitemRow(
            survey_id=str(_get(row, "SurveyID") or "").strip(),
            qid=qid,
            answer_id=answer_id,
            field=field,
            question_type=str(_get(row, "QuestionType") or "").strip(),
            export_tag=str(_get(row, "ExportTag") or "").strip(),
            label_en_md=str(_get(row, label_md_col) or ""),
            label_en_is_html=bool(_get(row, label_html_col) or False),
        )
    return result


def load_embedded_data_from_workbook(xlsx_path: Path) -> List[EmbeddedDataRow]:
    """Read EmbeddedDataRow objects from an existing workbook."""

    wb = load_workbook(xlsx_path, data_only=True)
    if EMBEDDED_DATA_SHEET not in wb.sheetnames:
        return []
    ws = wb[EMBEDDED_DATA_SHEET]
    headers, data_rows = _iter_sheet_rows(ws)
    idx = {name: i for i, name in enumerate(headers)}

    def _get(row, name, default=None):
        col = idx.get(name)
        if col is None or col >= len(row):
            return default
        cell = row[col]
        return cell.value if cell is not None else default

    result: List[EmbeddedDataRow] = []
    for row in data_rows:
        field_val = _get(row, "Field")
        if field_val is None:
            continue
        field = str(field_val).strip()
        if not field:
            continue
        flow_val = _get(row, "FlowID")
        flow_id = str(flow_val).strip() if flow_val is not None else ""
        order_val = _get(row, "FlowOrder")
        try:
            flow_order = int(order_val)
        except (TypeError, ValueError):
            flow_order = 0
        value_val = _get(row, "Value")
        value = str(value_val) if value_val is not None else None
        ed_type = str(_get(row, "Type") or "").strip()
        written_by = str(_get(row, "WrittenByQIDs") or "").strip() or None
        result.append(
            EmbeddedDataRow(
                survey_id=str(_get(row, "SurveyID") or "").strip(),
                flow_id=flow_id or None,
                flow_order=flow_order,
                field=field,
                value=value,
                ed_type=ed_type,
                written_by_qids=written_by,
            )
        )
    return result


def question_row_to_html(q: QuestionRow) -> str:
    """Convert a QuestionRow's EN text to HTML according to the IsHTML flag."""

    text = q.text_en_md or ""
    if q.text_en_is_html:
        return normalize_text(text)
    return md_to_html(text)


def option_row_to_html(o: OptionRow) -> str:
    """Convert an OptionRow's EN label to HTML according to the IsHTML flag."""

    text = o.label_en_md or ""
    if o.label_en_is_html:
        return normalize_text(text)
    return md_to_html(text)


def subitem_row_to_html(s: SubitemRow) -> str:
    """Convert a SubitemRow's EN label to HTML according to the IsHTML flag."""

    text = s.label_en_md or ""
    if s.label_en_is_html:
        return normalize_text(text)
    return md_to_html(text)
