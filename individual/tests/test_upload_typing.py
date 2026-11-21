import io
import json
from unittest import mock

import pandas as pd
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from core.test_helpers import LogInHelper, create_test_interactive_user
from individual import upload_typing
from individual.apps import IndividualConfig
from individual.models import IndividualDataSource, IndividualDataSourceUpload
from individual.services import IndividualImportService
from individual.upload_columns import register_upload_columns

SCHEMA = {"properties": {
    "first_name": {"type": "string"}, "last_name": {"type": "string"}, "dob": {"type": "string"},
    "sexe": {"type": "string", "enum": ["1", "2"]}, "age": {"type": "integer"}, "income": {"type": "number"},
}}
CSV = ("first_name,last_name,dob,location_name,location_code,group_code,sexe,age,income,hh_phone\n"
       "Ada,One,1990-01-01,Village,0101,G1,1,35,12.5,07123456\n"
       "Bo,Two,2010-01-01,Village,0101,G1,2,,3,\n")


class TextColumns:
    @staticmethod
    def template_columns():
        return []

    @staticmethod
    def accepts(name):
        return name.startswith("hh_")


class Typed:
    def setUp(self):
        super().setUp()
        register_upload_columns("test", TextColumns())
        self.addCleanup(register_upload_columns, "test", None)
        schema = mock.patch.object(IndividualConfig, "individual_schema", json.dumps(SCHEMA))
        schema.start()
        self.addCleanup(schema.stop)


class UploadTypingTest(Typed, SimpleTestCase):
    def records(self, frame):
        return [upload_typing.typed_record(record, SCHEMA["properties"])
                for record in json.loads(frame.to_json(orient="records"))]

    def test_text_columns_keep_their_text_and_whole_floats_of_integers_become_ints(self):
        records = self.records(upload_typing.read_csv(io.StringIO(CSV)))
        self.assertEqual(records[0], {
            "first_name": "Ada", "last_name": "One", "dob": "1990-01-01", "location_name": "Village",
            "location_code": "0101", "group_code": "G1", "sexe": "1", "age": 35, "income": 12.5,
            "hh_phone": "07123456",
        })
        self.assertEqual((records[1]["age"], records[1]["income"], records[1]["hh_phone"]), (None, 3, None))

    def test_a_spreadsheet_is_read_with_the_same_text_columns(self):
        dtypes = []

        def sheet(handle, **kwargs):
            dtypes.append(kwargs.get("dtype"))
            return pd.read_csv(handle, **kwargs)

        with mock.patch.object(upload_typing.pd, "read_excel", sheet):
            records = self.records(upload_typing.read_excel(io.StringIO(CSV)))
        self.assertEqual(dtypes[-1], {column: str for column in (
            "first_name", "last_name", "dob", "location_code", "sexe", "hh_phone")})
        self.assertEqual((records[0]["sexe"], records[0]["location_code"], records[0]["hh_phone"]),
                         ("1", "0101", "07123456"))


class SqlStagingTypingTest(Typed, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def test_the_sql_path_stages_typed_rows(self):
        upload = IndividualDataSourceUpload(source_name="typed", source_type="test")
        upload.save(user=self.user)
        service = IndividualImportService(self.user)
        frame = service.import_loaders["text/csv"](io.StringIO(CSV))
        service._save_data_source(frame, upload)
        rows = {row.json_ext["first_name"]: row.json_ext for row in IndividualDataSource.objects.filter(upload=upload)}
        self.assertEqual((rows["Ada"]["sexe"], rows["Ada"]["age"], rows["Ada"]["hh_phone"]), ("1", 35, "07123456"))
        self.assertEqual(rows["Bo"]["age"], None)


class TemplateOnceTest(Typed, APITestCase):
    def test_each_column_is_listed_once_the_base_fields_first(self):
        register_upload_columns("test-template", type("Cols", (), {
            "template_columns": staticmethod(lambda: ["hh_phone", "first_name"]),
            "accepts": staticmethod(lambda name: name == "hh_phone"),
        })())
        self.addCleanup(register_upload_columns, "test-template", None)
        self.client.force_authenticate(user=create_test_interactive_user())
        response = self.client.get(reverse("download_template_file"))
        header = b"".join(response.streaming_content).decode("utf-8").splitlines()[0].split(",")
        self.assertEqual(header, [
            "first_name", "last_name", "dob", "location_name", "location_code", "id", "sexe", "age", "income",
            "recipient_info", "individual_role", "group_code", "hh_phone",
        ])
