import csv
import datetime
import io
import json
from unittest import mock

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.apps import IndividualConfig
from individual.models import (
    Individual, IndividualDataSource, IndividualDataSourceUpload, IndividualDataUploadRecords,
)
from individual.tests.test_data_masking import MASKED, SEARCH_PERMS, RegisteredFieldsTestMixin
from individual.workflows.base_individual_update import process_update_individuals_workflow
from location.test_helpers import create_test_village


class MaskedUploadUpdateTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """An update upload refuses a row whose masked field holds the mask placeholder, naming the field on the row,
    and updates the other rows."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Upload update village", "code": "MkUu"})
        district = village.parent.parent.code
        self.admin = create_right_only_user(
            "mask_uu_admin", SEARCH_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        self.viewer = create_right_only_user("mask_uu_viewer", SEARCH_PERMS, [district])
        self.persons = []
        for first_name in ("Ada", "Grace", "Mary"):
            person = Individual(first_name=first_name, last_name="Stored", dob=datetime.date(1980, 1, 2),
                                location=village, json_ext={"phone": f"+257 {first_name}"})
            person.save(user=self.admin)
            self.persons.append(person)
        self.upload = IndividualDataSourceUpload(source_name="update.csv", source_type="individual update")
        self.upload.save(user=self.viewer)
        IndividualDataUploadRecords(data_upload=self.upload, workflow="Python Update Individuals",
                                    json_ext={}).save(user=self.admin)
        self.rows = []
        for person, first_name, phone in ((self.persons[0], MASKED, "+257 new 1"),
                                          (self.persons[1], "Grace", MASKED),
                                          (self.persons[2], "Marie", "+257 new 3")):
            row = IndividualDataSource(upload=self.upload, json_ext={
                "ID": str(person.id), "first_name": first_name, "last_name": "Updated", "dob": "1980-01-02",
                "location_name": None, "location_code": None, "phone": phone})
            row.save(user=self.viewer)
            self.rows.append(row)
        self.register(person=lambda: ["first_name", "json_ext.phone"], group=lambda: [], membership=lambda: [])
        for name, value in (("enable_maker_checker_for_individual_update", False),
                            ("individual_schema", json.dumps({"properties": {"phone": {"type": "string"}}}))):
            patcher = mock.patch.object(IndividualConfig, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_row_sending_the_mask_back_is_refused_and_the_other_rows_are_updated(self):
        process_update_individuals_workflow(self.admin.id, self.upload.id)

        errors = {row.id: IndividualDataSource.objects.get(id=row.id).validations["validation_errors"]
                  for row in self.rows}
        self.assertEqual([error["field_name"] for error in errors[self.rows[0].id]], ["first_name"])
        self.assertEqual([error["field_name"] for error in errors[self.rows[1].id]], ["phone"])
        self.assertEqual(errors[self.rows[2].id], [])
        ada, grace, mary = (Individual.objects.get(id=person.id) for person in self.persons)
        self.assertEqual((ada.first_name, ada.last_name, ada.json_ext["phone"]), ("Ada", "Stored", "+257 Ada"))
        self.assertEqual((grace.last_name, grace.json_ext["phone"]), ("Stored", "+257 Grace"))
        self.assertEqual((mary.first_name, mary.last_name, mary.json_ext["phone"]), ("Marie", "Updated", "+257 new 3"))

    def test_the_uploader_without_the_masked_data_right_reads_which_field_refused_the_row(self):
        process_update_individuals_workflow(self.admin.id, self.upload.id)

        response = self.query(
            '{ individualDataSource(upload_Id: "%s") { edges { node { jsonExt validations } } } }' % self.upload.id,
            headers={"HTTP_AUTHORIZATION": f"Bearer {BaseTestContext(user=self.viewer).get_jwt()}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        refused = sorted(error["field_name"] for edge in content["data"]["individualDataSource"]["edges"]
                         for error in json.loads(edge["node"]["validations"])["validation_errors"])
        self.assertEqual(refused, ["first_name", "phone"])

    def test_the_json_ext_copy_of_a_masked_column_is_masked_wherever_the_column_is(self):
        process_update_individuals_workflow(self.admin.id, self.upload.id)
        mary = Individual.objects.get(id=self.persons[2].id)
        self.assertEqual((mary.json_ext["first_name"], mary.json_ext["last_name"]), ("Marie", "Updated"))
        headers = {"HTTP_AUTHORIZATION": f"Bearer {BaseTestContext(user=self.viewer).get_jwt()}"}

        content = json.loads(self.query('{ individual(id: "%s") { edges { node { jsonExt } } } }' % mary.id,
                                        headers=headers).content)
        self.assertNotIn("errors", content)
        json_ext = json.loads(content["data"]["individual"]["edges"][0]["node"]["jsonExt"])
        self.assertEqual((json_ext["first_name"], json_ext["last_name"]), (MASKED, "Updated"))

        content = json.loads(self.query(
            '{ individual(customFilters: ["first_name__icontains__string=Mar"]) { totalCount } }',
            headers=headers).content)
        self.assertIn("masked data", json.dumps(content.get("errors")))

        content = json.loads(self.query(
            '{ individualExport(id: "%s", fields: ["id", "jsonExt"], fieldsColumns: "{}") }' % mary.id,
            headers=headers).content)
        self.assertNotIn("errors", content)
        export = ExportableQueryModel.objects.get(name=content["data"]["individualExport"])
        export.content.open("rb")
        [row] = list(csv.DictReader(io.StringIO(export.content.read().decode())))
        self.assertEqual((row["first_name"], row["last_name"]), (MASKED, "Updated"))
