import unittest
from pathlib import Path

from qsync import translation_export


class TestTranslationExportFilenames(unittest.TestCase):
    def test_default_filename_uses_base_language(self):
        path = translation_export._resolve_output_docx_path(
            survey_id="SV_123",
            survey_name="Survey",
            export_dir=Path("export"),
            output_path=None,
            smart_name=False,
            render_language=None,
            compare_to_base=False,
            base_language="EN",
            format="docx",
        )
        self.assertEqual(
            path.as_posix(),
            "export/Survey__SV_123__EN.docx",
        )

    def test_language_filename_uses_target_language(self):
        path = translation_export._resolve_output_docx_path(
            survey_id="SV_123",
            survey_name="Survey",
            export_dir=Path("export"),
            output_path=None,
            smart_name=False,
            render_language="NL",
            compare_to_base=False,
            base_language="EN",
            format="docx",
        )
        self.assertEqual(
            path.as_posix(),
            "export/Survey__SV_123__NL.docx",
        )

    def test_compare_to_base_uses_base_and_target(self):
        path = translation_export._resolve_output_docx_path(
            survey_id="SV_123",
            survey_name="Survey",
            export_dir=Path("export"),
            output_path=None,
            smart_name=False,
            render_language="NL",
            compare_to_base=True,
            base_language="EN",
            format="docx",
        )
        self.assertEqual(
            path.as_posix(),
            "export/Survey__SV_123__EN-NL.docx",
        )

    def test_compare_to_base_pdf_keeps_target_only(self):
        path = translation_export._resolve_output_docx_path(
            survey_id="SV_123",
            survey_name="Survey",
            export_dir=Path("export"),
            output_path=None,
            smart_name=False,
            render_language="NL",
            compare_to_base=True,
            base_language="EN",
            format="pdf",
        )
        self.assertEqual(
            path.as_posix(),
            "export/Survey__SV_123__NL.pdf",
        )


if __name__ == "__main__":
    unittest.main()
