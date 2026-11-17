import copy
import csv
import datetime
import io
import json
from unittest import mock

from django.http import HttpResponse
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.apps import IndividualConfig
from individual.data_masking import (
    GroupHistoryMask, GroupIndividualMask, GroupMask, IndividualHistoryMask, IndividualMask,
)
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


def membership_fields():
    return ["role", "recipient_type"]


class RegisteredFieldsTestMixin:
    def register(self, person=person_fields, group=group_fields, membership=membership_fields):
        masks = ((IndividualMask, person), (GroupMask, group), (GroupIndividualMask, membership))
        for mask, source in masks:
            patcher = mock.patch.dict(mask._sources, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
            mask.register_fields("test", source)
            self.addCleanup(mask.register_fields, "test", None)


class MaskFieldsTest(RegisteredFieldsTestMixin, TestCase):
    def test_the_person_masks_add_the_registered_fields_to_their_own(self):
        self.register()
        for mask in (IndividualMask(), IndividualHistoryMask()):
            self.assertEqual(mask.anon_fields, [*IndividualConfig.individual_mask_fields, *person_fields(),
                                                "json_ext.first_name", "json_ext.dob", "json_ext.merge_conflicts"])
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
        self.assertEqual(IndividualMask().anon_fields,
                         [*IndividualConfig.individual_mask_fields, "json_ext.merge_conflicts"])
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
            own = ["last_name", "json_ext.last_name", "json_ext.merge_conflicts"]
            self.assertEqual(IndividualMask().anon_fields, own)
            self.assertEqual(IndividualHistoryMask().anon_fields, own)
            with mock.patch.object(IndividualConfig, "individual_masking_enabled", False):
                self.assertEqual(IndividualMask().anon_fields, [])
                self.assertFalse(IndividualMask().masking_enabled)

    def test_the_registered_fields_are_masked_when_individual_masking_is_disabled(self):
        self.register()
        with mock.patch.object(IndividualConfig, "individual_masking_enabled", False):
            self.assertEqual(IndividualMask().anon_fields,
                             [*person_fields(), "json_ext.first_name", "json_ext.dob", "json_ext.merge_conflicts"])
            self.assertTrue(IndividualMask().masking_enabled)
            IndividualMask.register_fields("test", None)
            self.assertFalse(IndividualMask().masking_enabled)

    def test_dates_numbers_booleans_and_lists_are_masked_as_the_string_mask(self):
        mask = IndividualMask()
        self.assertEqual(mask.anonymize(datetime.date(1980, 1, 1)), MASKED)
        self.assertEqual(mask.anonymize(["a", 1, True]), [MASKED, MASKED, MASKED])
        self.assertEqual(mask.anonymize({"lat": 1.5, "tags": ["x"], "ok": False}),
                         {"lat": MASKED, "tags": [MASKED], "ok": MASKED})
        self.assertIsNone(mask.anonymize(None))


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
            self.assertEqual(json_ext["gps"], {"lat": MASKED, "lon": MASKED})
            self.assertEqual(json_ext["educated_level"], MASKED)
            self.assertEqual(json_ext["sex"], "F")

    def assert_group_masked(self, group):
        json_ext = json.loads(group["jsonExt"])
        self.assertEqual(group["code"], MASKED)
        self.assertEqual((json_ext["income"], json_ext["roof"]), (MASKED, "tin"))
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
            self.assertEqual(json.loads(record["jsonExt"])["income"], MASKED)
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
    """An export column reading a field the person, group or membership mask masks is written masked for a user
    without the masked-data right, and in clear with it."""

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
                            location=village, json_ext={"phone": "+257 1", "sex": "F"})
        person.save(user=admin)
        GroupIndividual(group=self.group, individual=person, role=GroupIndividual.Role.HEAD).save(user=admin)
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}
        self.register()

    def export(self, user, query, fields, args=""):
        response = self.query(EXPORT % (query, args, ", ".join(f'"{field}"' for field in fields)),
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        return json.loads(response.content)

    def exported_cells(self, user, query, fields, args=""):
        """The cells of the file the export writes, by column."""
        content = self.export(user, query, fields, args)
        self.assertNotIn("errors", content)
        export = ExportableQueryModel.objects.get(name=content["data"][query])
        export.content.open("rb")
        rows = list(csv.DictReader(io.StringIO(export.content.read().decode())))
        self.assertTrue(rows)
        return {column: {row[column] for row in rows} for column in rows[0]}

    MASKED_COLUMNS = (
        ("individualExport", "firstName", "", "first_name", "Ada"),
        ("individualExport", "dob", "", "dob", "1980-01-02"),
        ("individualExport", "jsonExt", "", "phone", "+257 1"),
        ("individualExport", "jsonExt.phone", "", "json_ext__phone", "+257 1"),
        ("groupExport", "code", "", "code", "MK-E1"),
        ("groupExport", "jsonExt", "", "income", "120"),
        ("groupExport", "jsonExt.head", "", "json_ext__head", "Ada Lovelace"),
        ("groupIndividualExport", "role", "group_Id", "role", "HEAD"),
        ("groupIndividualExport", "individual.firstName", "group_Id", "individual__first_name", "Ada"),
    )

    def test_a_masked_column_is_written_masked_without_the_masked_data_right(self):
        for query, field, arg, column, clear in self.MASKED_COLUMNS:
            args = f'{arg}: "{self.group.id}",' if arg else ""
            with self.subTest(query=query, field=field):
                cells = self.exported_cells(self.viewer, query, ["id", field], args)
                self.assertEqual(cells[column], {MASKED})
                self.assertNotIn(clear, {cell for values in cells.values() for cell in values})
                self.assertEqual(self.exported_cells(self.unmasked, query, ["id", field], args)[column], {clear})

    def test_a_json_column_reached_through_a_relation_is_written_with_its_masked_keys_masked(self):
        args = f'group_Id: "{self.group.id}",'
        [cell] = self.exported_cells(self.viewer, "groupIndividualExport", ["id", "group.jsonExt"], args)[
            "group__json_ext"]
        self.assertIn(f"'income': '{MASKED}'", cell)
        self.assertIn("'roof': 'tin'", cell)
        [cell] = self.exported_cells(self.unmasked, "groupIndividualExport", ["id", "group.jsonExt"], args)[
            "group__json_ext"]
        self.assertIn("'income': 120", cell)

    def test_the_unmasked_keys_of_a_masked_json_column_and_the_unmasked_columns_are_written_in_clear(self):
        cells = self.exported_cells(self.viewer, "individualExport", ["id", "lastName", "jsonExt"])
        self.assertEqual((cells["last_name"], cells["sex"], cells["phone"]), ({"Lovelace"}, {"F"}, {MASKED}))
        cells = self.exported_cells(self.viewer, "groupExport", ["id", "jsonExt.roof"])
        self.assertEqual(cells["json_ext__roof"], {"tin"})

    def test_an_export_with_custom_filters_writes_the_matching_rows_with_the_masked_columns_masked(self):
        Individual(first_name="Bob", last_name="Byron", dob=datetime.date(1970, 5, 6), location=self.group.location,
                   json_ext={"phone": "+257 2", "sex": "M"}).save(user=self.unmasked)
        Group(code="MK-E2", location=self.group.location, json_ext={"income": 80, "roof": "straw"}).save(
            user=self.unmasked)
        for query, condition, field, column, clear, other in (
                ("individualExport", "sex__iexact__string=F", "firstName", "first_name", "Ada", "Byron"),
                ("groupExport", "roof__iexact__string=tin", "code", "code", "MK-E1", "MK-E2")):
            args = 'customFilters: ["%s"],' % condition
            with self.subTest(query=query):
                cells = self.exported_cells(self.viewer, query, ["id", field, "lastName" if other == "Byron"
                                                                 else "jsonExt.roof"], args)
                self.assertEqual(cells[column], {MASKED})
                self.assertNotIn(other, {cell for values in cells.values() for cell in values})
                cells = self.exported_cells(self.unmasked, query, ["id", field], args)
                self.assertEqual(cells[column], {clear})

    def test_a_failing_source_writes_no_file(self):
        def failing():
            raise RuntimeError("source unavailable")

        self.register(person=failing)
        exports = ExportableQueryModel.objects.count()
        content = self.export(self.viewer, "individualExport", ["id", "lastName"])
        self.assertIn("errors", content)
        self.assertEqual(ExportableQueryModel.objects.count(), exports)


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
                      "sex": "F", "individual_role": "HEAD", "recipient_info": "PRIMARY", "group_code": "MK-U1"},
            validations={"validation_errors": [
                {"field_name": "phone", "note": "'phone' Field value '+257 1' is duplicated in this upload"},
                {"field_name": "sex", "note": "'sex' is required"},
            ]},
        ).save(user=admin)
        GroupDataSource(
            upload=self.upload, group=group,
            json_ext={"code": "MK-U1", "income": 120, "roof": "tin",
                      "individuals_data": [{"individual_id": str(person.id), "role": "HEAD",
                                            "recipient_type": "PRIMARY"}]},
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
        self.assertEqual((data["individual_role"], data["recipient_info"]), (MASKED, MASKED))
        data = json.loads(group_row["jsonExt"])
        self.assertEqual((data["code"], data["income"], data["roof"]), (MASKED, MASKED, "tin"))
        self.assertEqual((data["individuals_data"][0]["role"], data["individuals_data"][0]["recipient_type"]),
                         (MASKED, MASKED))
        self.assertEqual(group_row["group"]["code"], MASKED)

    def test_the_membership_keys_of_the_rows_follow_the_membership_mask(self):
        self.register(membership=lambda: [])
        row, group_row = self.gql(self.viewer)
        data = json.loads(row["jsonExt"])
        self.assertEqual((data["individual_role"], data["recipient_info"]), ("HEAD", "PRIMARY"))
        self.assertEqual(json.loads(group_row["jsonExt"])["individuals_data"][0]["role"], "HEAD")

    def test_the_rows_are_clear_with_the_masked_data_right(self):
        row, group_row = self.gql(self.unmasked)
        data = json.loads(row["jsonExt"])
        self.assertEqual((data["first_name"], data["dob"], data["phone"]), ("Ada", "1980-01-02", "+257 1"))
        self.assertIn("+257 1", json.loads(row["validations"])["validation_errors"][0]["note"])
        self.assertEqual(json.loads(group_row["jsonExt"])["code"], "MK-U1")
        self.assertEqual(json.loads(row["jsonExt"])["individual_role"], "HEAD")
        self.assertEqual(json.loads(group_row["jsonExt"])["individuals_data"][0]["recipient_type"], "PRIMARY")

    def test_the_invalid_rows_download_is_masked_without_the_masked_data_right(self):
        masked = b"".join(self.download(self.viewer, download_invalid_items, upload_id=self.upload.id)
                          .streaming_content).decode()
        self.assertNotIn("Ada", masked)
        self.assertNotIn("+257 1", masked)
        self.assertNotIn("HEAD", masked)
        self.assertNotIn("PRIMARY", masked)
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


MEMBERSHIP_QUERY = """
query {
  groupIndividual(group_Id: "%(group)s") { edges { node { role recipientType individual { firstName } } } }
  groupIndividualHistory(group_Id: "%(group)s") { edges { node { role recipientType } } }
  group(id: "%(group)s") { edges { node { code } } }
}
"""


class MembershipMaskTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """The membership fields other modules register are masked on every membership path; a masked role or
    recipient type is null."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Membership village", "code": "MkM"})
        district = village.parent.parent.code
        admin = create_right_only_user("mask_gi_admin", SEARCH_PERMS, [district])
        self.viewer = create_right_only_user("mask_gi_viewer", SEARCH_PERMS, [district])
        self.unmasked = create_right_only_user(
            "mask_gi_unmasked", SEARCH_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        self.group = Group(code="MK-M1", location=village)
        self.group.save(user=admin)
        person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2), location=village)
        person.save(user=admin)
        GroupIndividual(group=self.group, individual=person, role=GroupIndividual.Role.HEAD,
                        recipient_type=GroupIndividual.RecipientType.PRIMARY).save(user=admin)
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}
        self.register(group=lambda: [])

    def memberships(self, user):
        response = self.query(MEMBERSHIP_QUERY % {"group": self.group.id},
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        data = content["data"]
        history = [edge["node"] for edge in data["groupIndividualHistory"]["edges"]]
        self.assertTrue(history)
        return [edge["node"] for edge in data["groupIndividual"]["edges"]], history

    def test_the_registered_membership_fields_are_masked_without_the_masked_data_right(self):
        [membership], history = self.memberships(self.viewer)
        self.assertEqual((membership["role"], membership["recipientType"]), (None, None))
        self.assertEqual(membership["individual"]["firstName"], MASKED)
        for record in history:
            self.assertEqual((record["role"], record["recipientType"]), (None, None))

    def test_the_membership_fields_are_clear_with_the_masked_data_right(self):
        [membership], history = self.memberships(self.unmasked)
        self.assertEqual((membership["role"], membership["recipientType"]), ("HEAD", "PRIMARY"))
        self.assertEqual(history[0]["role"], "HEAD")


class FailingSourceTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """A mask whose fields cannot be computed never lets the values through."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Failing village", "code": "MkF"})
        district = village.parent.parent.code
        admin = create_right_only_user("mask_fail_admin", SEARCH_PERMS, [district])
        self.viewer = create_right_only_user("mask_fail_viewer", SEARCH_PERMS, [district])
        self.group = Group(code="MK-F1", location=village)
        self.group.save(user=admin)
        person = Individual(first_name="Adaline", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                            location=village, json_ext={"phone": "+257 9"})
        person.save(user=admin)
        GroupIndividual(group=self.group, individual=person, role=GroupIndividual.Role.HEAD).save(user=admin)
        self.token = BaseTestContext(user=self.viewer).get_jwt()

        def failing():
            raise RuntimeError("source unavailable")

        self.register(person=failing, group=lambda: [])

    def test_a_failing_source_gives_errors_not_values(self):
        for query in ('{ group(id: "%s") { edges { node { head { dateCreated firstName dob jsonExt } } } } }',
                      '{ groupIndividual(group_Id: "%s") { edges { node { individual { dateCreated firstName dob '
                      'jsonExt } } } } }'):
            with self.subTest(query=query):
                response = self.query(query % self.group.id, headers={"HTTP_AUTHORIZATION": f"Bearer {self.token}"})
                text = response.content.decode()
                self.assertIn("errors", json.loads(text))
                self.assertNotIn("Adaline", text)
                self.assertNotIn("+257 9", text)
                self.assertNotIn("1980-01-02", text)


UPDATE_PERMS = SEARCH_PERMS + ["gql_individual_update_perms", "gql_group_update_perms"]
NESTED_KEYS = ("phones", "address", "score", "active")


def json_arg(value):
    """``value`` as a GraphQL string literal of its JSON."""
    return json.dumps(json.dumps(value))


class MaskedValueSentBackTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """An update by a user without the masked-data right never stores the mask it read."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Update village", "code": "MkW"})
        district = village.parent.parent.code
        admin = create_right_only_user("mask_upd_admin", SEARCH_PERMS, [district])
        self.editor = create_right_only_user("mask_upd_editor", UPDATE_PERMS, [district])
        self.unmasked = create_right_only_user(
            "mask_upd_unmasked", UPDATE_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        self.village = village
        self.group = Group(code="MK-W1", location=village, json_ext={"income": 120})
        self.group.save(user=admin)
        self.person = Individual(
            first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2), location=village,
            json_ext={"phone": "+257 1", "sex": "F", "phones": ["+257 11", "+257 22"],
                      "address": {"hill": "Rohero", "colline": "Kiriri"}, "score": 5, "active": True})
        self.person.save(user=admin)
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.editor, self.unmasked)}
        self.register(person=lambda: [*person_fields(), *(f"json_ext.{key}" for key in NESTED_KEYS)],
                      group=lambda: ["code", "json_ext.gps"])
        patcher = mock.patch.object(IndividualConfig, "check_individual_update", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def update_individual(self, user, fields):
        return self.mutate(user, "updateIndividual", f'id: "{self.person.id}" locationId: {self.village.id} {fields}')

    def mutate(self, user, name, fields):
        response = self.query(f"mutation {{ {name}(input: {{ {fields} }}) {{ internalId }} }}",
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        return self._get_mutation_log_node(content["data"][name]["internalId"], self.tokens[user.username])

    def person_now(self):
        return Individual.objects.get(id=self.person.id)

    def test_a_person_update_sending_masked_values_back_is_refused(self):
        node = self.update_individual(self.editor, (
            f'firstName: "{MASKED}" lastName: "Byron" '
            f'jsonExt: "{{\\"phone\\": \\"{MASKED}\\", \\"sex\\": \\"F\\"}}"'))
        self.assertNotEqual(node["status"], 2, node)
        self.assertIn("first_name", node["error"])
        self.assertIn("json_ext.phone", node["error"])
        person = self.person_now()
        self.assertEqual((person.first_name, person.last_name, person.json_ext["phone"]),
                         ("Ada", "Lovelace", "+257 1"))

    def test_fields_left_out_and_a_masked_dob_keep_their_values(self):
        node = self.update_individual(
            self.editor, f'lastName: "Byron" dob: "{MASKED}" jsonExt: "{{\\"sex\\": \\"M\\"}}"')
        self.assertEqual(node["status"], 2, node["error"])
        person = self.person_now()
        self.assertEqual((person.first_name, person.last_name, person.dob), ("Ada", "Byron", datetime.date(1980, 1, 2)))
        self.assertEqual((person.json_ext["phone"], person.json_ext["sex"]), ("+257 1", "M"))

    def test_a_new_value_of_a_masked_field_is_saved(self):
        node = self.update_individual(self.editor, 'firstName: "Augusta" dob: "1981-03-04"')
        self.assertEqual(node["status"], 2, node["error"])
        person = self.person_now()
        self.assertEqual((person.first_name, person.dob), ("Augusta", datetime.date(1981, 3, 4)))

    def test_the_update_task_refuses_masked_values_sent_back(self):
        from tasks_management.models import Task

        tasks = Task.objects.count()
        with mock.patch.object(IndividualConfig, "check_individual_update", True):
            node = self.update_individual(self.editor, f'firstName: "{MASKED}"')
        self.assertNotEqual(node["status"], 2, node)
        self.assertIn("first_name", node["error"])
        self.assertEqual(Task.objects.count(), tasks)

    def test_a_group_update_sending_the_masked_code_back_is_refused(self):
        node = self.mutate(self.editor, "updateGroup", f'id: "{self.group.id}" code: "{MASKED}"')
        self.assertNotEqual(node["status"], 2, node)
        self.assertIn("code", node["error"])
        self.assertEqual(Group.objects.get(id=self.group.id).code, "MK-W1")

    def test_a_list_or_object_holding_the_mask_is_refused(self):
        node = self.update_individual(self.editor, "jsonExt: " + json_arg({
            "sex": "F", "phones": [MASKED, MASKED, "+257 33"], "address": {"hill": MASKED, "colline": "Nyakabiga"}}))
        self.assertNotEqual(node["status"], 2, node)
        self.assertIn("json_ext.phones", node["error"])
        self.assertIn("json_ext.address", node["error"])
        json_ext = self.person_now().json_ext
        self.assertEqual(json_ext["phones"], ["+257 11", "+257 22"])
        self.assertEqual(json_ext["address"], {"hill": "Rohero", "colline": "Kiriri"})

    def test_a_masked_number_or_boolean_takes_any_value(self):
        node = self.update_individual(self.editor, "jsonExt: " + json_arg({"sex": "F", "score": 0, "active": False}))
        self.assertEqual(node["status"], 2, node["error"])
        json_ext = self.person_now().json_ext
        self.assertEqual((json_ext["score"], json_ext["active"]), (0, False))
        self.assertEqual(json_ext["phones"], ["+257 11", "+257 22"])

    def test_a_masked_number_or_boolean_sent_back_as_the_mask_keeps_its_stored_value(self):
        node = self.update_individual(self.editor, "jsonExt: " + json_arg(
            {"sex": "M", "score": MASKED, "active": MASKED}))
        self.assertEqual(node["status"], 2, node["error"])
        json_ext = self.person_now().json_ext
        self.assertEqual((json_ext["sex"], json_ext["score"], json_ext["active"]), ("M", 5, True))

    def test_a_masked_object_of_numbers_read_and_sent_back_keeps_its_stored_values(self):
        from individual.services import GroupService

        Group.objects.filter(id=self.group.id).update(json_ext={"income": 120, "gps": {"x": 1.5, "y": 2.5, "z": 3}})
        response = self.query('{ group(id: "%s") { edges { node { jsonExt } } } }' % self.group.id,
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[self.editor.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        json_ext = json.loads(content["data"]["group"]["edges"][0]["node"]["jsonExt"])
        self.assertEqual(json_ext["gps"], {"x": MASKED, "y": MASKED, "z": MASKED})
        result = GroupService(self.editor).update({"id": self.group.id, "json_ext": copy.deepcopy(json_ext)})
        self.assertTrue(result["success"], result)
        self.assertEqual(Group.objects.get(id=self.group.id).json_ext["gps"], {"x": 1.5, "y": 2.5, "z": 3})
        json_ext["gps"]["y"] = 9.5
        result = GroupService(self.editor).update({"id": self.group.id, "json_ext": json_ext})
        self.assertTrue(result["success"], result)
        self.assertEqual(Group.objects.get(id=self.group.id).json_ext["gps"], {"x": 1.5, "y": 9.5, "z": 3})

    def test_the_group_update_and_its_task_refuse_an_object_holding_the_mask(self):
        from individual.services import GroupService

        data = {"id": self.group.id, "json_ext": {"income": 120, "gps": {"lat": MASKED, "lon": 2.5}}}
        for method in ("update", "create_update_task"):
            with self.subTest(method=method):
                result = getattr(GroupService(self.editor), method)(copy.deepcopy(data))
                self.assertFalse(result["success"], result)
                self.assertIn("json_ext.gps", result["detail"])
        self.assertNotIn("gps", Group.objects.get(id=self.group.id).json_ext)

    def test_a_user_with_the_masked_data_right_is_not_checked(self):
        node = self.update_individual(self.unmasked, 'lastName: "Byron"')
        self.assertEqual(node["status"], 2, node["error"])
        self.assertEqual(self.person_now().last_name, "Byron")


NESTED_QUERY = """
query {
  individual(id: "%(individual)s") {
    edges { node {
      groupindividuals { edges { node { role recipientType } } }
      individualdatasourceSet { edges { node { jsonExt } } }
    } }
  }
  group(id: "%(group)s") {
    edges { node {
      groupindividuals { edges { node { role individual { firstName } } } }
      groupdatasourceSet { edges { node { jsonExt } } }
    } }
  }
}
"""


class NestedMaskPathsTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """Memberships and upload rows reached through a person or a group are masked as the root queries mask them."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Nested village", "code": "MkN"})
        district = village.parent.parent.code
        admin = create_right_only_user("mask_nest_admin", SEARCH_PERMS, [district])
        self.viewer = create_right_only_user("mask_nest_viewer", SEARCH_PERMS, [district])
        self.unmasked = create_right_only_user(
            "mask_nest_unmasked", SEARCH_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        self.group = Group(code="MK-N1", location=village)
        self.group.save(user=admin)
        self.person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                                 location=village)
        self.person.save(user=admin)
        GroupIndividual(group=self.group, individual=self.person, role=GroupIndividual.Role.HEAD,
                        recipient_type=GroupIndividual.RecipientType.PRIMARY).save(user=admin)
        upload = IndividualDataSourceUpload(source_name="nested.csv", source_type="individual import")
        upload.save(user=admin)
        IndividualDataSource(upload=upload, individual=self.person,
                             json_ext={"first_name": "Ada", "last_name": "Lovelace"}).save(user=admin)
        GroupDataSource(upload=upload, group=self.group, json_ext={"code": "MK-N1"}).save(user=admin)
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}
        self.register(group=lambda: ["code"])

    def gql(self, user):
        response = self.query(NESTED_QUERY % {"individual": self.person.id, "group": self.group.id},
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        [person] = [edge["node"] for edge in content["data"]["individual"]["edges"]]
        [group] = [edge["node"] for edge in content["data"]["group"]["edges"]]

        def nodes(connection):
            return [edge["node"] for edge in connection["edges"]]

        return (nodes(person["groupindividuals"]), nodes(person["individualdatasourceSet"]),
                nodes(group["groupindividuals"]), nodes(group["groupdatasourceSet"]))

    def test_the_nested_paths_are_masked_without_the_masked_data_right(self):
        [membership], [row], [group_membership], [group_row] = self.gql(self.viewer)
        self.assertEqual((membership["role"], membership["recipientType"]), (None, None))
        self.assertEqual(json.loads(row["jsonExt"])["first_name"], MASKED)
        self.assertEqual((group_membership["role"], group_membership["individual"]["firstName"]), (None, MASKED))
        self.assertEqual(json.loads(group_row["jsonExt"])["code"], MASKED)

    def test_the_nested_paths_are_clear_with_the_masked_data_right(self):
        [membership], [row], [group_membership], [group_row] = self.gql(self.unmasked)
        self.assertEqual((membership["role"], membership["recipientType"]), ("HEAD", "PRIMARY"))
        self.assertEqual(json.loads(row["jsonExt"])["first_name"], "Ada")
        self.assertEqual(group_membership["individual"]["firstName"], "Ada")
        self.assertEqual(json.loads(group_row["jsonExt"])["code"], "MK-N1")


class MaskedMembershipSentBackTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """A masked role or recipient type reads as null; null sent back by a user without the masked-data right keeps
    the stored value."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Membership update village", "code": "MkMu"})
        district = village.parent.parent.code
        admin = create_right_only_user("mask_gu_admin", SEARCH_PERMS, [district])
        self.editor = create_right_only_user("mask_gu_editor", UPDATE_PERMS, [district])
        self.unmasked = create_right_only_user(
            "mask_gu_unmasked", UPDATE_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        self.group = Group(code="MK-GU1", location=village)
        self.group.save(user=admin)
        person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2), location=village)
        person.save(user=admin)
        self.membership = GroupIndividual(group=self.group, individual=person, role=GroupIndividual.Role.HEAD,
                                          recipient_type=GroupIndividual.RecipientType.PRIMARY)
        self.membership.save(user=admin)
        self.register(person=lambda: [], group=lambda: [])

    def update(self, user, method="update", **fields):
        from individual.services import GroupIndividualService

        data = {"id": self.membership.id, "group_id": self.group.id, **fields}
        return getattr(GroupIndividualService(user), method)(data)

    def stored(self):
        membership = GroupIndividual.objects.get(id=self.membership.id)
        return membership.role, membership.recipient_type

    def test_a_masked_role_sent_back_as_null_keeps_the_stored_role(self):
        result = self.update(self.editor, role=None)
        self.assertTrue(result["success"], result)
        self.assertEqual(self.stored(), ("HEAD", "PRIMARY"))

    def test_the_update_task_keeps_a_masked_role_sent_back_as_null(self):
        from tasks_management.models import Task

        result = self.update(self.editor, "create_update_task", role=None)
        self.assertTrue(result["success"], result)
        task = Task.objects.order_by("-date_created").first()
        self.assertNotIn("role", task.data["incoming_data"])

    def test_a_new_role_and_a_null_from_the_masked_data_right_are_written(self):
        result = self.update(self.editor, role="SON")
        self.assertTrue(result["success"], result)
        self.assertEqual(self.stored()[0], "SON")
        result = self.update(self.unmasked, role=None)
        self.assertTrue(result["success"], result)
        self.assertEqual(self.stored(), (None, "PRIMARY"))


class UnresolvedExportColumnTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """An export column that names no field, such as an annotation, is written masked for a user without the
    masked-data right and in clear with it."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Annotated village", "code": "MkAn"})
        district = village.parent.parent.code
        self.viewer = create_right_only_user("mask_an_viewer", SEARCH_PERMS, [district])
        self.unmasked = create_right_only_user(
            "mask_an_unmasked", SEARCH_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                   location=village).save(user=self.viewer)
        self.register(person=lambda: ["first_name"], group=lambda: [], membership=lambda: [])

    def exported(self, user):
        from functools import partial
        from django.db.models import F
        from individual.data_masking import mask_export_columns

        queryset = Individual.objects.filter(last_name="Lovelace").annotate(head_first_name=F("first_name"))
        export = ExportableQueryModel.create_csv_export(
            queryset, ["last_name", "head_first_name"], user, column_names={},
            patches=[partial(mask_export_columns, Individual, user)])
        export.content.open("rb")
        [row] = list(csv.DictReader(io.StringIO(export.content.read().decode())))
        return row["last_name"], row["head_first_name"]

    def test_an_annotated_column_is_written_masked_without_the_masked_data_right(self):
        self.assertEqual(self.exported(self.viewer), ("Lovelace", MASKED))

    def test_an_annotated_column_is_written_in_clear_with_the_masked_data_right(self):
        self.assertEqual(self.exported(self.unmasked), ("Lovelace", "Ada"))
