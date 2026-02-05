import unittest


class SurveyInventoryOptimizationTests(unittest.TestCase):
    def test_compose_inventory_record_does_not_export_response_counts(self) -> None:
        from qsync.survey_inventory import compose_inventory_record

        record = compose_inventory_record(
            {"id": "SV_TEST", "name": "Test", "ownerId": "UR_X", "isActive": True},
            {},
            current_user_id="UR_X",
            existing_locks={},
        )
        self.assertIsNone(record.get("preview_count"))
        self.assertIsNone(record.get("response_count"))

    def test_compose_inventory_record_uses_response_counts_from_payload(self) -> None:
        from qsync.survey_inventory import compose_inventory_record

        payload = {"responseCounts": {"generated": 7, "auditable": 11}}
        record = compose_inventory_record(
            {"id": "SV_TEST", "name": "Test", "ownerId": "UR_X", "isActive": True},
            {},
            current_user_id="UR_X",
            existing_locks={},
            payload=payload,
        )
        self.assertEqual(record.get("preview_count"), 7)
        self.assertEqual(record.get("response_count"), 11)

    def test_compose_inventory_record_derives_cntry_from_country_embedded_field(
        self,
    ) -> None:
        from qsync.survey_inventory import compose_inventory_record

        flow_payload = {
            "result": {
                "SurveyFlow": {
                    "Flow": [
                        {
                            "Type": "EmbeddedData",
                            "EmbeddedData": [{"Field": "country", "Value": "IE"}],
                        }
                    ]
                }
            }
        }
        record = compose_inventory_record(
            {"id": "SV_TEST", "name": "Test", "ownerId": "UR_X", "isActive": True},
            {},
            current_user_id="UR_X",
            existing_locks={},
            flow_payload=flow_payload,
            include_counts=False,
        )
        self.assertEqual(record.get("cntry"), "IE")

    def test_compose_inventory_record_falls_back_to_surveylang_for_cntry(self) -> None:
        from qsync.survey_inventory import compose_inventory_record

        flow_payload = {
            "result": {
                "SurveyFlow": {
                    "Flow": [
                        {
                            "Type": "EmbeddedData",
                            "EmbeddedData": [{"Field": "surveylang", "Value": "NL"}],
                        }
                    ]
                }
            }
        }
        record = compose_inventory_record(
            {"id": "SV_TEST", "name": "Test", "ownerId": "UR_X", "isActive": True},
            {},
            current_user_id="UR_X",
            existing_locks={},
            flow_payload=flow_payload,
            include_counts=False,
        )
        self.assertEqual(record.get("cntry"), "NL")

    def test_compose_inventory_record_collects_legacy_surveylang_warning(self) -> None:
        from qsync.survey_inventory import compose_inventory_record

        flow_payload = {
            "result": {
                "SurveyFlow": {
                    "Flow": [
                        {
                            "Type": "EmbeddedData",
                            "EmbeddedData": [{"Field": "surveylang", "Value": "NL"}],
                        }
                    ]
                }
            }
        }
        warnings: dict[str, list[tuple[str, str | None]]] = {}
        record = compose_inventory_record(
            {"id": "SV_TEST", "name": "Test", "ownerId": "UR_X", "isActive": True},
            {},
            current_user_id="UR_X",
            existing_locks={},
            flow_payload=flow_payload,
            include_counts=False,
            flow_routing_warnings=warnings,
        )
        self.assertEqual(record.get("cntry"), "NL")
        self.assertEqual(warnings.get("legacy_surveylang"), [("SV_TEST", "Test")])

    def test_compose_inventory_record_collects_missing_country_warning(self) -> None:
        from qsync.survey_inventory import compose_inventory_record

        flow_payload = {
            "result": {
                "SurveyFlow": {
                    "Flow": [
                        {
                            "Type": "EmbeddedData",
                            "EmbeddedData": [{"Field": "unrelated", "Value": "x"}],
                        }
                    ]
                }
            }
        }
        warnings: dict[str, list[tuple[str, str | None]]] = {}
        record = compose_inventory_record(
            {"id": "SV_TEST", "name": "Test", "ownerId": "UR_X", "isActive": True},
            {},
            current_user_id="UR_X",
            existing_locks={},
            flow_payload=flow_payload,
            include_counts=False,
            flow_routing_warnings=warnings,
        )
        self.assertEqual(record.get("cntry"), "US")
        self.assertEqual(warnings.get("missing_country"), [("SV_TEST", "Test")])


if __name__ == "__main__":
    unittest.main()
