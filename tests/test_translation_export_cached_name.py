from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _patch_common_filesystem(monkeypatch, tmp_path):
    monkeypatch.setattr("qsync.translation_export.resolve_root", lambda required=False: tmp_path)
    monkeypatch.setattr(
        "qsync.translation_export.resolve_scoped_dir",
        lambda dirname, *, root=None, account=None: tmp_path / Path(dirname),
    )


def _patch_export_stubs(monkeypatch, tmp_path, captured):
    def fake_resolve_output_docx_path(*, survey_name, format="docx", **_kwargs):
        captured[format] = survey_name
        ext = ".pdf" if format == "pdf" else ".docx"
        return tmp_path / f"export{ext}"

    monkeypatch.setattr(
        "qsync.translation_export._resolve_output_docx_path",
        fake_resolve_output_docx_path,
    )
    monkeypatch.setattr(
        "qsync.translation_export.export_survey_payload_to_word",
        lambda *_args, **_kwargs: tmp_path / "payload.docx",
    )
    monkeypatch.setattr(
        "qsync.translation_export.export_survey_payload_to_pdf",
        lambda *_args, **_kwargs: tmp_path / "payload.pdf",
    )


def test_export_filename_prefers_cached_survey_name(monkeypatch, tmp_path):
    payload = {"result": {"SurveyName": "Cached Export Name"}}
    monkeypatch.setattr(
        "qsync.translation_export.load_cached_survey",
        lambda survey_id, **_kwargs: SimpleNamespace(payload=payload),
    )

    captured: dict[str, str] = {}
    _patch_common_filesystem(monkeypatch, tmp_path)
    _patch_export_stubs(monkeypatch, tmp_path, captured)

    from qsync.translation_export import export_survey_to_pdf, export_survey_to_word

    export_survey_to_word("SV_TEST", skip_preflight=True)
    export_survey_to_pdf("SV_TEST", skip_preflight=True)

    assert captured.get("docx") == "Cached Export Name"
    assert captured.get("pdf") == "Cached Export Name"


def test_export_filename_falls_back_when_cached_name_missing(monkeypatch, tmp_path):
    payload = {"result": {}}
    monkeypatch.setattr(
        "qsync.translation_export.load_cached_survey",
        lambda survey_id, **_kwargs: SimpleNamespace(payload=payload),
    )

    captured: dict[str, str] = {}
    _patch_common_filesystem(monkeypatch, tmp_path)
    _patch_export_stubs(monkeypatch, tmp_path, captured)

    from qsync.translation_export import export_survey_to_word

    export_survey_to_word("SV_TEST", skip_preflight=True)

    assert captured.get("docx") == ""
