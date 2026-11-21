import json
from unittest import mock

import pandas as pd
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from core.test_helpers import LogInHelper, create_test_interactive_user
from individual import import_pipeline
from individual.apps import IndividualConfig
from individual.models import Individual, IndividualDataSource, IndividualDataSourceUpload
from individual.services import IndividualImportService
from individual.upload_columns import register_upload_columns, template_columns
from individual.workflows.utils import check_dataframe_headers, remove_registered_columns_from_persons
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


class UploadColumnsPersonTest(TestCase, RegisteredColumns):
    """Registered columns stay on the staged row and never reach the person, on either import path."""

    ROW = {"first_name": "Head", "last_name": "Cols", "dob": "1990-01-01", "group_code": "COLS-1", "email": "a@b.c",
           "consent_note": "kept", "consent_a": "yes", "consent_given_at": "2026-01-01"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def setUp(self):
        super().setUp()
        self.register()
        schema = mock.patch.object(IndividualConfig, "individual_schema", json.dumps(SCHEMA))
        schema.start()
        self.addCleanup(schema.stop)
        self.upload = IndividualDataSourceUpload(source_name="columns", source_type="test")
        self.upload.save(user=self.user)

    def staged(self, individual=None):
        row = IndividualDataSource(upload=self.upload, individual=individual, json_ext=dict(self.ROW),
                                   validations={"validation_errors": [], "row": 0})
        row.save(user=self.user)
        return row

    def assert_off_the_person(self, row):
        row.refresh_from_db()
        self.assertEqual(row.json_ext, self.ROW)
        kept = {key: value for key, value in self.ROW.items() if key not in ("consent_a", "consent_given_at")}
        self.assertEqual(row.individual.json_ext, kept)

    def test_the_pipeline_creates_the_person_without_them(self):
        row = self.staged()
        import_pipeline.import_chunk(str(self.user.id), [row.id])
        self.assert_off_the_person(row)

    def test_the_sql_workflows_remove_them_from_the_persons_they_created(self):
        individual = Individual(first_name="Head", last_name="Cols", dob="1990-01-01", json_ext=dict(self.ROW))
        individual.save(user=self.user)
        row = self.staged(individual)
        remove_registered_columns_from_persons(self.upload.id)
        individual.refresh_from_db()
        self.assert_off_the_person(row)

    def test_a_column_the_provider_removes_is_kept_off_even_when_it_no_longer_passes_the_header_check(self):
        class Closed(ConsentColumns):
            @staticmethod
            def accepts(name):
                return False

            @staticmethod
            def removes(name):
                return name.startswith("consent_")

        register_upload_columns("test", Closed())
        row = self.staged()
        import_pipeline.import_chunk(str(self.user.id), [row.id])
        self.assert_off_the_person(row)


class SqlStagingRowNumberTest(TestCase):
    """The SQL import path stages each row with its 0-based number, kept through validation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def test_each_staged_row_keeps_its_number_through_validation(self):
        upload = IndividualDataSourceUpload(source_name="numbers", source_type="test")
        upload.save(user=self.user)
        service = IndividualImportService(self.user)
        service._save_data_source(pd.DataFrame([{"first_name": "A"}, {"first_name": "B"}, {"first_name": "C"}]),
                                  upload)
        rows = {row.json_ext["first_name"]: row for row in IndividualDataSource.objects.filter(upload=upload)}
        self.assertEqual({name: row.validations for name, row in rows.items()},
                         {"A": {"row": 0}, "B": {"row": 1}, "C": {"row": 2}})
        service.save_validation_error_in_data_source_bulk([
            {"row": {"id": rows["A"].id}, "validations": {}},
            {"row": {"id": rows["B"].id}, "validations": {"dob": {"success": False, "field_name": "dob",
                                                                   "note": "bad"}}},
        ])
        self.assertEqual(
            {name: IndividualDataSource.objects.get(id=row.id).validations for name, row in rows.items()},
            {"A": {"validation_errors": [], "row": 0},
             "B": {"validation_errors": [{"field_name": "dob", "note": "bad"}], "row": 1},
             "C": {"row": 2}})
