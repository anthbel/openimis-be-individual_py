import json
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from core.test_helpers import LogInHelper, create_test_interactive_user
from individual.apps import IndividualConfig
from individual.models import Individual, IndividualDataSource, IndividualDataSourceUpload, IndividualDataUploadRecords
from individual.signals.on_validation_import_valid_items import IndividualItemsImportTaskCompletionEvent
from individual.upload_columns import register_upload_columns, template_columns
from individual.workflows.utils import check_dataframe_headers
from workflow.exceptions import PythonWorkflowHandlerException

BASE = {"first_name", "last_name", "dob", "location_name", "location_code", "id"}
SCHEMA = {"properties": {"email": {"type": "string"}, "consent_note": {"type": "string"}}}


class ConsentColumns:
    """Columns named ``consent_*``; the template lists two of them."""

    @staticmethod
    def template_columns():
        return ["consent_a", "consent_given_at"]

    @staticmethod
    def accepts(name):
        return name.startswith("consent_")


class RegisteredColumns:
    def register(self):
        register_upload_columns("test", ConsentColumns())
        self.addCleanup(register_upload_columns, "test", None)


class UploadColumnsHeaderTest(TestCase, RegisteredColumns):
    def test_a_registered_column_passes_the_header_check(self):
        self.register()
        check_dataframe_headers(BASE | {"email", "consent_a", "consent_b"}, SCHEMA)

    def test_an_unregistered_column_is_still_refused(self):
        self.register()
        with self.assertRaises(PythonWorkflowHandlerException) as raised:
            check_dataframe_headers(BASE | {"other"}, SCHEMA)
        self.assertIn("'other'", str(raised.exception))

    def test_an_update_refuses_a_registered_column(self):
        self.register()
        with self.assertRaises(PythonWorkflowHandlerException) as raised:
            check_dataframe_headers(BASE | {"ID", "consent_a"}, SCHEMA, is_update=True)
        self.assertIn("'consent_a'", str(raised.exception))

    def test_a_header_that_is_not_text_is_reported_as_an_invalid_column(self):
        self.register()
        with self.assertRaises(PythonWorkflowHandlerException) as raised:
            check_dataframe_headers(BASE | {2026}, SCHEMA)
        self.assertIn("2026", str(raised.exception))

    def test_without_a_provider_the_column_is_refused(self):
        with self.assertRaises(PythonWorkflowHandlerException):
            check_dataframe_headers(BASE | {"consent_a"}, SCHEMA)

    def test_a_removed_provider_no_longer_accepts_its_columns(self):
        self.register()
        register_upload_columns("test", None)
        self.assertEqual(template_columns(), [])
        with self.assertRaises(PythonWorkflowHandlerException):
            check_dataframe_headers(BASE | {"consent_a"}, SCHEMA)


class UploadColumnsTemplateTest(APITestCase, RegisteredColumns):
    def test_the_template_lists_the_registered_columns(self):
        self.register()
        self.client.force_authenticate(user=create_test_interactive_user())
        response = self.client.get(reverse("download_template_file"))
        header = b"".join(response.streaming_content).decode("utf-8").splitlines()[0].split(",")
        self.assertIn("consent_a", header)
        self.assertIn("consent_given_at", header)
        self.assertEqual(len(header), len(set(header)))


class UploadColumnsGroupStepTest(TestCase, RegisteredColumns):
    """The group step removes the registered columns from the person, and leaves the staged row as uploaded."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def test_the_registered_columns_leave_the_person_and_stay_on_the_row(self):
        self.register()
        upload = IndividualDataSourceUpload(source_name="columns", source_type="test")
        upload.save(user=self.user)
        record = IndividualDataUploadRecords(data_upload=upload, workflow="test",
                                             json_ext={"group_aggregation_column": "group_code"})
        record.save(user=self.user)
        row = {"group_code": "COLS-1", "individual_role": "HEAD", "email": "a@b.c", "consent_note": "kept",
               "consent_a": "yes", "consent_given_at": "2026-01-01"}
        individual = Individual(first_name="Head", last_name="Cols", dob="1990-01-01", json_ext=dict(row))
        individual.save(user=self.user)
        IndividualDataSource(upload=upload, individual=individual, json_ext=dict(row)).save(user=self.user)
        with mock.patch.object(IndividualConfig, "individual_schema", json.dumps(SCHEMA)), \
                mock.patch.object(IndividualConfig, "enable_maker_checker_for_group_upload", False):
            IndividualItemsImportTaskCompletionEvent("test.test", record, str(upload.id), self.user).run_group_step()
        individual.refresh_from_db()
        self.assertEqual(individual.json_ext, {"email": "a@b.c", "consent_note": "kept"})
        self.assertEqual(IndividualDataSource.objects.get(individual=individual).json_ext, row)
