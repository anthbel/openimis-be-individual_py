"""The system checks refuse a second mask class registered for Task or HistoricalTask: core gives each model the
mask registered last, which would replace individual's task masks."""
from django.core.checks import run_checks
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import SimpleTestCase

from core.data_masking import DataMaskAbs, MaskingClassRegistryPoint
from individual.task_masking import HistoricalTaskMask, TaskMask


class OtherTaskMask(DataMaskAbs):
    masking_model = "Task"


class OtherHistoricalTaskMask(DataMaskAbs):
    masking_model = "HistoricalTask"


class TaskMaskGuardTest(SimpleTestCase):
    def setUp(self):
        registered = list(MaskingClassRegistryPoint.REGISTERED_MASKING_CLASS)
        self.addCleanup(MaskingClassRegistryPoint.REGISTERED_MASKING_CLASS.__setitem__, slice(None), registered)

    def guard_errors(self):
        return [message for message in run_checks() if message.id == "individual.E001"]

    def test_individuals_task_masks_alone_pass(self):
        self.assertEqual(self.guard_errors(), [])

    def test_individuals_task_masks_registered_twice_pass(self):
        MaskingClassRegistryPoint.register_masking_class([TaskMask(), HistoricalTaskMask()])
        self.assertEqual(self.guard_errors(), [])

    def test_a_second_task_mask_class_is_refused(self):
        MaskingClassRegistryPoint.register_masking_class([OtherTaskMask()])
        with self.assertRaises(SystemCheckError) as raised:
            call_command("check")
        self.assertIn("OtherTaskMask", str(raised.exception))
        self.assertIn("individual.E001", str(raised.exception))

    def test_a_second_historical_task_mask_class_is_refused(self):
        MaskingClassRegistryPoint.register_masking_class([OtherHistoricalTaskMask()])
        errors = self.guard_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("HistoricalTask", errors[0].msg)
        self.assertIn("OtherHistoricalTaskMask", errors[0].msg)
