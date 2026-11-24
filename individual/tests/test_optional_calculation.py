import sys
from unittest import mock

import pandas as pd
from django.test import SimpleTestCase

from individual.services import IndividualImportService

FIELD_PROPERTIES = {"validationCalculation": {"name": "ValidateEmail"}}
ROW = pd.Series({"email": "a@b.c"})


class OptionalCalculationTest(SimpleTestCase):
    def test_module_does_not_import_calculation_at_load(self):
        import individual.services as services

        self.assertFalse(hasattr(services, "get_calculation_object"))

    def test_validation_refused_when_calculation_not_installed(self):
        blocked = {"calculation": None, "calculation.services": None}
        with mock.patch("individual.services.apps.is_installed", return_value=False) as installed, \
                mock.patch.dict(sys.modules, blocked):
            result = IndividualImportService._handle_validation_calculation(ROW, "email", FIELD_PROPERTIES)
        installed.assert_called_with("calculation")
        self.assertFalse(result["success"])
        self.assertEqual(result["field_name"], "email")
        self.assertIn("'email'", result["note"])

    def test_validation_refused_when_no_rule_matches_uuid(self):
        with mock.patch("individual.services.apps.is_installed", return_value=True), \
                mock.patch("calculation.services.get_calculation_object", return_value=None) as getter:
            result = IndividualImportService._handle_validation_calculation(ROW, "email", FIELD_PROPERTIES)
        getter.assert_called_once()
        self.assertFalse(result["success"])
        self.assertEqual(result["field_name"], "email")
        self.assertIn("'email'", result["note"])

    def test_validation_delegates_to_matching_rule(self):
        rule = mock.Mock()
        rule.calculate_if_active_for_object.return_value = {"success": True, "field_name": "email"}
        with mock.patch("individual.services.apps.is_installed", return_value=True), \
                mock.patch("calculation.services.get_calculation_object", return_value=rule):
            result = IndividualImportService._handle_validation_calculation(ROW, "email", FIELD_PROPERTIES)
        self.assertEqual(result, {"success": True, "field_name": "email"})
