import datetime
import json
from unittest import mock

from django.http import HttpResponse
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.apps import IndividualConfig
from individual.data_masking import GroupHistoryMask, GroupMask, IndividualHistoryMask, IndividualMask
from individual.models import (
    Group, GroupDataSource, GroupIndividual, Individual, IndividualDataSource, IndividualDataSourceUpload,
)
from individual.views import download_individual_upload, download_invalid_items
from location.test_helpers import create_test_village

MASKED = "***************"
SEARCH_PERMS = ["gql_individual_search_perms", "gql_group_search_perms"]


def person_fields():
    return ["first_name", "dob", "json_ext.phone", "json_ext.gps"]


def group_fields():
    return ["code", "json_ext.income"]


class RegisteredFieldsTestMixin:
    def register(self, person=person_fields, group=group_fields):
        for mask in (IndividualMask, GroupMask):
            patcher = mock.patch.dict(mask._sources, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        IndividualMask.register_fields("test", person)
        GroupMask.register_fields("test", group)
        self.addCleanup(IndividualMask.register_fields, "test", None)
        self.addCleanup(GroupMask.register_fields, "test", None)


class MaskFieldsTest(RegisteredFieldsTestMixin, TestCase):
    def test_the_person_masks_add_the_registered_fields_to_their_own(self):
        self.register()
        for mask in (IndividualMask(), IndividualHistoryMask()):
            self.assertEqual(mask.anon_fields, [*IndividualConfig.individual_mask_fields, *person_fields()])
            self.assertTrue(mask.masking_enabled)

    def test_the_registered_fields_are_read_when_the_mask_is_applied(self):
        fields = ["first_name"]
        self.register(person=lambda: list(fields))
        fields.append("last_name")
        self.assertIn("last_name", IndividualMask().anon_fields)

    def test_none_removes_a_registration(self):
        self.register()
        IndividualMask.register_fields("test", None)
        GroupMask.register_fields("test", None)
        self.assertEqual(IndividualMask().anon_fields, list(IndividualConfig.individual_mask_fields))
        self.assertNotIn("code", GroupMask().anon_fields)

    def test_the_group_masks_take_the_registered_fields_and_the_copy_of_the_masked_head_fields(self):
        self.register()
        with mock.patch.object(IndividualConfig, "group_json_ext_keys_not_copied_from_head", ["gps"]):
            for mask in (GroupMask(), GroupHistoryMask()):
                fields = mask.anon_fields
                self.assertLessEqual({"code", "json_ext.income", "json_ext.phone", "json_ext.head",
                                      "json_ext.members", "json_ext.primary_recipient",
                                      "json_ext.secondary_recipient"}, set(fields))
                self.assertNotIn("json_ext.gps", fields)
                self.assertNotIn("first_name", fields)

    def test_the_own_fields_follow_the_configuration_in_use(self):
        with mock.patch.object(IndividualConfig, "individual_mask_fields", ["last_name"]), \
                mock.patch.object(IndividualConfig, "individual_masking_enabled", True):
            self.assertEqual(IndividualMask().anon_fields, ["last_name"])
            self.assertEqual(IndividualHistoryMask().anon_fields, ["last_name"])
            with mock.patch.object(IndividualConfig, "individual_masking_enabled", False):
                self.assertEqual(IndividualMask().anon_fields, [])
                self.assertFalse(IndividualMask().masking_enabled)

    def test_the_registered_fields_are_masked_when_individual_masking_is_disabled(self):
        self.register()
        with mock.patch.object(IndividualConfig, "individual_masking_enabled", False):
            self.assertEqual(IndividualMask().anon_fields, person_fields())
            self.assertTrue(IndividualMask().masking_enabled)
            IndividualMask.register_fields("test", None)
            self.assertFalse(IndividualMask().masking_enabled)

    def test_dates_and_lists_are_masked(self):
        mask = IndividualMask()
        self.assertEqual(mask.anonymize(datetime.date(1980, 1, 1)), MASKED)
        self.assertEqual(mask.anonymize(["a", 1]), [MASKED, 0])
        self.assertEqual(mask.anonymize({"lat": 1.5, "tags": ["x"]}), {"lat": 0, "tags": [MASKED]})


PERSON_QUERY = """
query {
  individual(id: "%(individual)s") { edges { node { firstName lastName dob jsonExt } } }
  individualHistory(id: "%(individual)s") { edges { node { firstName dob jsonExt } } }
}
"""

GROUP_QUERY = """
query {
  group(id: "%(group)s") { edges { node { code jsonExt head { firstName dob jsonExt } } } }
  groupHistory(id: "%(group)s") { edges { node { code jsonExt head { firstName } } } }
  groupIndividual(group_Id: "%(group)s") {
    edges { node { individual { firstName dob jsonExt } group { code jsonExt } } }
  }
}
"""


class MaskedQueriesTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """individual's person and group queries, and the persons and groups they reach, are masked by the person
    and group masks for a user without the masked-data right."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Mask village", "code": "MkV"})
        district = village.parent.parent.code
        admin = create_right_only_user("mask_admin", SEARCH_PERMS, [district])
        self.viewer = create_right_only_user("mask_viewer", SEARCH_PERMS, [district])
        self.unmasked = create_right_only_user(
            "mask_unmasked", SEARCH_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        self.group = Group(code="MK-G1", location=village, json_ext={"income": 120, "roof": "tin"})
        self.group.save(user=admin)
        self.person = Individual(
            first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2), location=village,
            json_ext={"phone": "+257 1", "gps": {"lat": 1.5, "lon": 2.5}, "educated_level": "primary",
                      "sex": "F"},
        )
        self.person.save(user=admin)
        GroupIndividual(group=self.group, individual=self.person, role=GroupIndividual.Role.HEAD).save(user=admin)
        self.group.refresh_from_db()
        self.assertEqual(self.group.json_ext["phone"], "+257 1")
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}
        self.register()

    def gql(self, user, query):
        response = self.query(query % {"individual": self.person.id, "group": self.group.id},
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        return content["data"]

    @staticmethod
    def nodes(data, name):
        return [edge["node"] for edge in data[name]["edges"]]

    def assert_person_masked(self, person):
        json_ext = json.loads(person["jsonExt"]) if "jsonExt" in person else None
        self.assertEqual(person["firstName"], MASKED)
        if "dob" in person:
            self.assertEqual(person["dob"], MASKED)
        if json_ext is not None:
            self.assertEqual(json_ext["phone"], MASKED)
            self.assertEqual(json_ext["gps"], {"lat": 0, "lon": 0})
            self.assertEqual(json_ext["educated_level"], MASKED)
            self.assertEqual(json_ext["sex"], "F")

    def assert_group_masked(self, group):
        json_ext = json.loads(group["jsonExt"])
        self.assertEqual(group["code"], MASKED)
        self.assertEqual((json_ext["income"], json_ext["roof"]), (0, "tin"))
        self.assertEqual((json_ext["phone"], json_ext["head"]), (MASKED, MASKED))
        self.assertEqual(set(json_ext["members"].values()), {MASKED})

    def test_the_person_queries_mask_the_registered_fields(self):
        data = self.gql(self.viewer, PERSON_QUERY)
        [person] = self.nodes(data, "individual")
        self.assert_person_masked(person)
        self.assertEqual(person["lastName"], "Lovelace")
        history = self.nodes(data, "individualHistory")
        self.assertTrue(history)
        for record in history:
            self.assert_person_masked(record)

    def test_the_group_queries_mask_the_group_and_the_persons_they_reach(self):
        data = self.gql(self.viewer, GROUP_QUERY)
        [group] = self.nodes(data, "group")
        self.assert_group_masked(group)
        self.assert_person_masked(group["head"])
        history = self.nodes(data, "groupHistory")
        self.assertTrue(history)
        for record in history:
            self.assertEqual(record["code"], MASKED)
            self.assertEqual(json.loads(record["jsonExt"])["income"], 0)
            self.assertEqual(record["head"]["firstName"], MASKED)
        [membership] = self.nodes(data, "groupIndividual")
        self.assert_person_masked(membership["individual"])
        self.assert_group_masked(membership["group"])

    def test_a_user_with_the_masked_data_right_reads_the_values(self):
        data = self.gql(self.unmasked, PERSON_QUERY)
        [person] = self.nodes(data, "individual")
        self.assertEqual((person["firstName"], person["dob"]), ("Ada", "1980-01-02"))
        self.assertEqual(json.loads(person["jsonExt"])["phone"], "+257 1")
        data = self.gql(self.unmasked, GROUP_QUERY)
        [group] = self.nodes(data, "group")
        self.assertEqual((group["code"], json.loads(group["jsonExt"])["income"]), ("MK-G1", 120))
        self.assertEqual(json.loads(group["jsonExt"])["head"], "Ada Lovelace")
        self.assertEqual(group["head"]["firstName"], "Ada")
        [membership] = self.nodes(data, "groupIndividual")
        self.assertEqual(membership["individual"]["firstName"], "Ada")

    def test_without_registered_fields_the_own_fields_are_masked(self):
        IndividualMask.register_fields("test", None)
        GroupMask.register_fields("test", None)
        [person] = self.nodes(self.gql(self.viewer, PERSON_QUERY), "individual")
        self.assertEqual((person["firstName"], person["dob"]), ("Ada", "1980-01-02"))
        self.assertEqual(json.loads(person["jsonExt"])["educated_level"], MASKED)
        [group] = self.nodes(self.gql(self.viewer, GROUP_QUERY), "group")
        self.assertEqual(group["code"], "MK-G1")
        self.assertEqual(json.loads(group["jsonExt"])["educated_level"], MASKED)


EXPORT = '{ %s(%s fields: [%s], fieldsColumns: "{}") }'


class MaskedExportTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """An export column reading a field the person or group mask masks needs the masked-data right."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Export village", "code": "MkE"})
        district = village.parent.parent.code
        admin = create_right_only_user("mask_exp_admin", SEARCH_PERMS, [district])
        self.viewer = create_right_only_user("mask_exp_viewer", SEARCH_PERMS, [district])
        self.unmasked = create_right_only_user(
            "mask_exp_unmasked", SEARCH_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        self.group = Group(code="MK-E1", location=village, json_ext={"income": 120, "roof": "tin"})
        self.group.save(user=admin)
        person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                            location=village, json_ext={"phone": "+257 1"})
        person.save(user=admin)
        GroupIndividual(group=self.group, individual=person, role=GroupIndividual.Role.HEAD).save(user=admin)
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}
        self.register()

    def export(self, user, query, fields, args=""):
        response = self.query(EXPORT % (query, args, ", ".join(f'"{field}"' for field in fields)),
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        return json.loads(response.content)

    def test_a_masked_column_is_refused_without_the_masked_data_right(self):
        group_arg = f'group_Id: "{self.group.id}",'
        for query, column, args in (("individualExport", "firstName", ""), ("individualExport", "dob", ""),
                                    ("individualExport", "jsonExt", ""), ("individualExport", "jsonExt.phone", ""),
                                    ("groupExport", "code", ""), ("groupExport", "jsonExt", ""),
                                    ("groupExport", "jsonExt.head", ""),
                                    ("groupIndividualExport", "individual.firstName", group_arg),
                                    ("groupIndividualExport", "group.jsonExt", group_arg)):
            with self.subTest(query=query, column=column):
                self.assertIn("errors", self.export(self.viewer, query, ["id", column], args))
                self.assertNotIn("errors", self.export(self.unmasked, query, ["id", column], args))

    def test_an_unmasked_column_is_exported(self):
        for query, column in (("individualExport", "lastName"), ("individualExport", "jsonExt.sex"),
                              ("groupExport", "jsonExt.roof")):
            with self.subTest(query=query, column=column):
                self.assertNotIn("errors", self.export(self.viewer, query, ["id", column]))


DATA_SOURCE_QUERY = """
query {
  individualDataSource(upload_Id: "%(upload)s") { edges { node { jsonExt validations individual { firstName } } } }
  groupDataSource(upload_Id: "%(upload)s") { edges { node { jsonExt validations group { code } } } }
}
"""


class UploadRowMaskTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """An upload row and its validation errors are masked by the mask of the person or group the row holds."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Upload village", "code": "MkU"})
        district = village.parent.parent.code
        admin = create_right_only_user("mask_up_admin", SEARCH_PERMS, [district])
        self.viewer = create_right_only_user("mask_up_viewer", SEARCH_PERMS, [district])
        self.unmasked = create_right_only_user(
            "mask_up_unmasked", SEARCH_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        group = Group(code="MK-U1", location=village, json_ext={"income": 120, "roof": "tin"})
        group.save(user=admin)
        person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                            location=village, json_ext={"phone": "+257 1", "sex": "F"})
        person.save(user=admin)
        self.upload = IndividualDataSourceUpload(source_name="rows.csv", source_type="individual import")
        self.upload.save(user=admin)
        IndividualDataSource(
            upload=self.upload, individual=person,
            json_ext={"first_name": "Ada", "last_name": "Lovelace", "dob": "1980-01-02", "phone": "+257 1",
                      "sex": "F"},
            validations={"validation_errors": [
                {"field_name": "phone", "note": "'phone' Field value '+257 1' is duplicated in this upload"},
                {"field_name": "sex", "note": "'sex' is required"},
            ]},
        ).save(user=admin)
        GroupDataSource(
            upload=self.upload, group=group,
            json_ext={"code": "MK-U1", "income": 120, "roof": "tin",
                      "individuals_data": [{"individual_id": str(person.id), "role": "HEAD"}]},
        ).save(user=admin)
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}
        self.register()

    def gql(self, user):
        response = self.query(DATA_SOURCE_QUERY % {"upload": self.upload.id},
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        [row] = [edge["node"] for edge in content["data"]["individualDataSource"]["edges"]]
        [group_row] = [edge["node"] for edge in content["data"]["groupDataSource"]["edges"]]
        return row, group_row

    def download(self, user, view, **params):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(reverse(view), params)

    def test_the_rows_and_their_errors_are_masked_without_the_masked_data_right(self):
        row, group_row = self.gql(self.viewer)
        data = json.loads(row["jsonExt"])
        self.assertEqual((data["first_name"], data["dob"], data["phone"]), (MASKED, MASKED, MASKED))
        self.assertEqual((data["last_name"], data["sex"]), ("Lovelace", "F"))
        notes = {error["field_name"]: error["note"] for error in json.loads(row["validations"])["validation_errors"]}
        self.assertEqual(notes, {"phone": MASKED, "sex": "'sex' is required"})
        self.assertEqual(row["individual"]["firstName"], MASKED)
        data = json.loads(group_row["jsonExt"])
        self.assertEqual((data["code"], data["income"], data["roof"]), (MASKED, 0, "tin"))
        self.assertEqual(data["individuals_data"][0]["role"], "HEAD")
        self.assertEqual(group_row["group"]["code"], MASKED)

    def test_the_rows_are_clear_with_the_masked_data_right(self):
        row, group_row = self.gql(self.unmasked)
        data = json.loads(row["jsonExt"])
        self.assertEqual((data["first_name"], data["dob"], data["phone"]), ("Ada", "1980-01-02", "+257 1"))
        self.assertIn("+257 1", json.loads(row["validations"])["validation_errors"][0]["note"])
        self.assertEqual(json.loads(group_row["jsonExt"])["code"], "MK-U1")

    def test_the_invalid_rows_download_is_masked_without_the_masked_data_right(self):
        masked = b"".join(self.download(self.viewer, download_invalid_items, upload_id=self.upload.id)
                          .streaming_content).decode()
        self.assertNotIn("Ada", masked)
        self.assertNotIn("+257 1", masked)
        self.assertIn("Lovelace", masked)
        clear = b"".join(self.download(self.unmasked, download_invalid_items, upload_id=self.upload.id)
                         .streaming_content).decode()
        self.assertIn("Ada", clear)
        self.assertIn("+257 1", clear)

    def test_the_uploaded_file_needs_the_masked_data_right(self):
        response = self.download(self.viewer, download_individual_upload, filename="rows.csv")
        self.assertEqual(response.status_code, 403)
        with mock.patch("individual.views.DefaultStorageFileHandler") as handler:
            handler.return_value.get_file_response_csv.return_value = HttpResponse(b"rows")
            response = self.download(self.unmasked, download_individual_upload, filename="rows.csv")
        self.assertEqual(response.status_code, 200)
