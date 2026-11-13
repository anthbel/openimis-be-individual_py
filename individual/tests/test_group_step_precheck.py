"""The ``group_service.precheck_upload`` signal sent before the group step of an upload."""
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.service_signals import ServiceSignalBindType
from core.signals import REGISTERED_SERVICE_SIGNALS, bind_service_signal
from core.test_helpers import create_test_interactive_user
from individual import import_pipeline
from individual.apps import IndividualConfig
from individual.models import (
    Group, GroupDataSource, Individual, IndividualDataSource, IndividualDataSourceUpload,
    IndividualDataUploadRecords,
)
from individual.services import GroupService, IndividualImportService
from individual.signals.on_validation_import_valid_items import IndividualItemsImportTaskCompletionEvent
from individual.tasks import finish_import_task
from tasks_management.models import Task, TaskExecutor, TaskGroup
from tasks_management.services import TaskService

SIGNAL = "group_service.precheck_upload"


class GroupStepPrecheckTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="precheck_admin")

    def setUp(self):
        self.calls = []
        self.refusal = None
        self.refused_code = None
        bind_service_signal(SIGNAL, self.receiver, bind_type=ServiceSignalBindType.BEFORE)
        self.addCleanup(self.disconnect)

    def receiver(self, **kwargs):
        args, named = kwargs["data"]
        sources = args[0] if args else named["data_sources"]
        self.calls.append({
            "sender": kwargs["cls_"],
            "codes": sorted(source.json_ext["code"] for source in sources),
            "groups": Group.objects.filter(code__startswith="PRECHECK-").count(),
        })
        if self.refusal and (self.refused_code is None or self.refused_code in self.calls[-1]["codes"]):
            raise ValidationError(self.refusal)

    def disconnect(self):
        signal = REGISTERED_SERVICE_SIGNALS[SIGNAL]
        if signal.is_signal_registered():
            signal.before_service_signal.disconnect(self.receiver)

    def upload(self, *codes, workflow="test"):
        upload = IndividualDataSourceUpload(source_name="precheck", source_type="test")
        upload.save(user=self.user)
        record = IndividualDataUploadRecords(
            data_upload=upload, workflow=workflow, json_ext={"group_aggregation_column": "group_code"})
        record.save(user=self.user)
        for code in codes:
            json_ext = {"group_code": code, "individual_role": "HEAD"}
            individual = Individual(first_name=code, last_name="Test", dob="1990-01-01", json_ext=dict(json_ext))
            individual.save(user=self.user)
            IndividualDataSource(upload=upload, individual=individual, json_ext=dict(json_ext)).save(user=self.user)
        return upload, record

    def group_step(self, upload, record, maker_checker=False):
        with mock.patch.object(IndividualConfig, "enable_maker_checker_for_group_upload", maker_checker):
            IndividualItemsImportTaskCompletionEvent("test.test", record, str(upload.id), self.user).run_group_step()

    def assert_failed_with_nothing_grouped(self, upload, key=None, calls=1):
        upload.refresh_from_db()
        self.assertEqual(upload.status, IndividualDataSourceUpload.Status.FAIL)
        self.assertEqual(upload.error["group_step"], self.refusal)
        if key:
            self.assertEqual(upload.error[key], self.refusal)
        self.assertFalse(Group.objects.filter(code__startswith="PRECHECK-").exists())
        self.assertFalse(GroupDataSource.objects.filter(upload=upload, group__isnull=False).exists())
        self.assertEqual(len(self.calls), calls)

    def sources(self, upload):
        return {source.json_ext["code"]: str(source.id) for source in GroupDataSource.objects.filter(upload=upload)}

    def partial_approval_task(self, record):
        """The group task, accepted and resolved vote by vote by a group of policy ANY outside a flow."""
        task = Task.objects.get(business_event=IndividualConfig.validation_import_group_valid_items,
                                entity_id=record.id)
        task_group = TaskGroup(code="precheck-any", completion_policy="ANY")
        task_group.save(user=self.user)
        TaskExecutor(task_group=task_group, user=self.user).save(user=self.user)
        task.task_group = task_group
        task.status = Task.Status.ACCEPTED
        task.save(user=self.user)
        return task

    def vote(self, task, verdict):
        task.refresh_from_db()
        TaskService(self.user).resolve_task({"id": str(task.id), "business_status": {str(self.user.id): verdict}})

    def test_receivers_get_every_pending_source_before_any_group_is_created(self):
        upload, record = self.upload("PRECHECK-1", "PRECHECK-2")
        self.group_step(upload, record)
        self.assertEqual(len(self.calls), 1)
        self.assertIsInstance(self.calls[0]["sender"], GroupService)
        self.assertEqual(self.calls[0]["codes"], ["PRECHECK-1", "PRECHECK-2"])
        self.assertEqual(self.calls[0]["groups"], 0)
        self.assertEqual(Group.objects.filter(code__startswith="PRECHECK-").count(), 2)

    def test_a_refusal_stops_the_group_step_and_fails_the_upload(self):
        upload, record = self.upload("PRECHECK-1", "PRECHECK-2")
        self.refusal = "upload refused: PRECHECK-2"
        with self.assertRaisesMessage(ValidationError, "upload refused"):
            self.group_step(upload, record)
        self.assert_failed_with_nothing_grouped(upload)
        # The members keep the columns the group step reads, as before it ran.
        self.assertTrue(Individual.objects.filter(json_ext__group_code="PRECHECK-1").exists())

    def test_a_refusal_before_the_group_task_fails_the_upload_without_a_task(self):
        upload, record = self.upload("PRECHECK-1", "PRECHECK-2")
        self.refusal = "upload refused: PRECHECK-2"
        with self.assertRaisesMessage(ValidationError, "upload refused"):
            self.group_step(upload, record, maker_checker=True)
        self.assert_failed_with_nothing_grouped(upload)
        self.assertFalse(Task.objects.filter(
            business_event=IndividualConfig.validation_import_group_valid_items, entity_id=record.id).exists())

    def test_a_refusal_when_the_group_task_completes_fails_the_upload(self):
        upload, record = self.upload("PRECHECK-1", "PRECHECK-2")
        self.group_step(upload, record, maker_checker=True)
        task = Task.objects.get(business_event=IndividualConfig.validation_import_group_valid_items,
                                entity_id=record.id)
        self.refusal = "upload refused: PRECHECK-2"
        TaskService(self.user).complete_task({"id": task.id})
        self.assert_failed_with_nothing_grouped(upload, "Task Resolve", calls=2)
        self.assertEqual([call["codes"] for call in self.calls], [["PRECHECK-1", "PRECHECK-2"]] * 2)

    def test_a_partial_approval_is_refused_for_a_pending_row_it_does_not_accept(self):
        upload, record = self.upload("PRECHECK-1", "PRECHECK-2", "PRECHECK-3")
        self.group_step(upload, record, maker_checker=True)
        task = self.partial_approval_task(record)
        sources = self.sources(upload)
        self.refusal, self.refused_code = "upload refused: PRECHECK-2", "PRECHECK-2"
        self.vote(task, {"ACCEPT": [sources["PRECHECK-1"]]})
        self.assert_failed_with_nothing_grouped(upload, calls=2)
        self.assertEqual(self.calls[1]["codes"], ["PRECHECK-1", "PRECHECK-2", "PRECHECK-3"])
        # Once refused, the upload stays refused: a later vote groups nothing, even without the refused row.
        self.vote(task, {"REJECT": [sources["PRECHECK-2"]], "ACCEPT": [sources["PRECHECK-3"]]})
        self.assert_failed_with_nothing_grouped(upload, calls=2)

    def test_a_row_rejected_in_a_partial_approval_is_not_prechecked(self):
        upload, record = self.upload("PRECHECK-1", "PRECHECK-2")
        self.group_step(upload, record, maker_checker=True)
        task = self.partial_approval_task(record)
        sources = self.sources(upload)
        self.refusal, self.refused_code = "upload refused: PRECHECK-2", "PRECHECK-2"
        self.vote(task, {"REJECT": [sources["PRECHECK-2"]], "ACCEPT": [sources["PRECHECK-1"]]})
        self.assertTrue(Group.objects.filter(code="PRECHECK-1").exists())
        self.assertFalse(Group.objects.filter(code="PRECHECK-2").exists())
        self.assertEqual([call["codes"] for call in self.calls], [["PRECHECK-1", "PRECHECK-2"], ["PRECHECK-1"]])

    @mock.patch.object(IndividualImportService, "synchronize_data_for_reporting")
    def test_a_refusal_in_the_last_import_task_fails_the_upload(self, _sync):
        upload, record = self.upload(
            "PRECHECK-1", "PRECHECK-2", workflow=import_pipeline.PIPELINE_IMPORT_WORKFLOW)
        self.refusal = "upload refused: PRECHECK-2"
        with mock.patch.object(IndividualConfig, "enable_maker_checker_for_group_upload", False):
            result = finish_import_task.apply(args=(str(upload.id), str(self.user.id)))
        self.assertTrue(result.failed())
        self.assert_failed_with_nothing_grouped(upload, "pipeline")
        self.assertIn("timestamp", upload.error)

    def test_a_failed_upload_workflow_keeps_the_refusal_of_the_group_step(self):
        upload, _ = self.upload("PRECHECK-1")
        self.refusal = "upload refused: PRECHECK-1"

        def run(_arguments):
            # The upload workflow runs the group step when no review is required.
            IndividualDataSourceUpload.objects.filter(id=upload.id).update(
                status=IndividualDataSourceUpload.Status.FAIL, error={"group_step": self.refusal})
            return {"success": False, "message": "workflow stopped"}

        workflow = mock.Mock(run=run)
        IndividualImportService(self.user)._trigger_workflow(workflow, upload)
        upload.refresh_from_db()
        self.assertEqual(upload.status, IndividualDataSourceUpload.Status.FAIL)
        self.assertEqual(upload.error, {"group_step": self.refusal, "workflow": "workflow stopped"})
