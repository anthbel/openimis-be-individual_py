import importlib
import sys
from unittest import mock

import pandas as pd
from core import signals as core_signals
from django.test import SimpleTestCase

from individual.services import IndividualImportService

FIELD_PROPERTIES = {"validationCalculation": {"name": "ValidateEmail"}}
ROW = pd.Series({"email": "a@b.c"})


class _CalculationImportRecorder:
    """Meta path finder that records and refuses every import under `calculation`."""

    def __init__(self):
        self.attempts = []

    def find_spec(self, name, path=None, target=None):
        if name == "calculation" or name.startswith("calculation."):
            self.attempts.append(name)
            raise ImportError(f"import of '{name}' refused")
        return None


def _reimport_without_calculation(module_names):
    recorder = _CalculationImportRecorder()
    isolated = {
        name: module
        for name, module in sys.modules.items()
        if not (name == "calculation" or name.startswith("calculation."))
        and name not in module_names
    }
    with mock.patch.dict(sys.modules, isolated, clear=True), \
            mock.patch.dict(core_signals.REGISTERED_SERVICE_SIGNALS, clear=True), \
            mock.patch.object(sys, "meta_path", [recorder] + sys.meta_path):
        for name in module_names:
            try:
                importlib.import_module(name)
            except ImportError:
                break
        loaded = sorted(n for n in sys.modules if n == "calculation" or n.startswith("calculation."))
    return recorder.attempts, loaded


class OptionalCalculationTest(SimpleTestCase):
    def test_module_does_not_import_calculation_at_load(self):
        import individual.services as services

        self.assertFalse(hasattr(services, "get_calculation_object"))

    def test_fresh_import_does_not_touch_calculation(self):
        attempts, loaded = _reimport_without_calculation(["individual.services", "individual.import_pipeline"])
        self.assertEqual(attempts, [])
        self.assertEqual(loaded, [])

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
