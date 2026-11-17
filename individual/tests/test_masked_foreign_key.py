import csv
import datetime
import io
import json
from unittest import mock

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.apps import IndividualConfig
from individual.data_masking import mask_for_user
from individual.models import Group, GroupIndividual, Individual, IndividualDataSource, IndividualDataSourceUpload
from individual.tests.test_data_masking import MASKED, SEARCH_PERMS, UPDATE_PERMS, RegisteredFieldsTestMixin
from location.test_helpers import create_test_village

LOCATION_KEYS = ("location_name", "location_code", "location_str")


class MaskedForeignKeyTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """A masked foreign-key column (``location``) reads as null for a user without the masked-data right on every
    path, with the json_ext keys holding a copy of it masked; a null sent back keeps the stored value."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Fk village", "code": "MkK"})
        self.village, self.district = village, village.parent.parent.code
        admin = create_right_only_user("mask_fk_admin", SEARCH_PERMS, [self.district])
        self.viewer = create_right_only_user("mask_fk_viewer", UPDATE_PERMS, [self.district])
        self.unmasked = create_right_only_user(
            "mask_fk_unmasked", UPDATE_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [self.district])
        copies = {"location_name": village.name, "location_code": village.code, "location_str": str(village)}
        self.group = Group(code="MK-K1", location=village, json_ext={"roof": "tin"})
        self.group.save(user=admin)
        self.person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                                 location=village, json_ext={**copies, "sex": "F"})
        self.person.save(user=admin)
        GroupIndividual(group=self.group, individual=self.person, role=GroupIndividual.Role.HEAD).save(user=admin)
        self.upload = IndividualDataSourceUpload(source_name="fk.csv", source_type="individual import")
        self.upload.save(user=admin)
        IndividualDataSource(upload=self.upload, individual=self.person, json_ext={**copies, "first_name": "Ada"},
                             validations={"validation_errors": [
                                 {"field_name": "location_name", "note": f"Location {village.name} is ambiguous"}]},
                             ).save(user=admin)
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}
        self.register(person=lambda: ["location"], group=lambda: ["location"], membership=lambda: [])
        patcher = mock.patch.object(IndividualConfig, "check_individual_update", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def gql(self, user, query):
        response = self.query(query % {"person": self.person.id, "group": self.group.id, "upload": self.upload.id,
                                       "village": self.village.uuid},
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        return json.loads(response.content)

    LOCATIONS = """{
      individual(id: "%(person)s") { edges { node { location { code } } } }
      individualHistory(id: "%(person)s") { edges { node { location { code } } } }
      group(id: "%(group)s") { edges { node {
        location { code }
        head { location { code } }
        groupindividuals { edges { node { individual { location { code } } } } }
      } } }
      groupHistory(id: "%(group)s") { edges { node { location { code } } } }
      groupIndividual(group_Id: "%(group)s") { edges { node {
        individual { location { code } } group { location { code } } } } }
      individualDataSource(upload_Id: "%(upload)s") { edges { node { individual { location { code } } } } }
    }"""

    def location_codes(self, user):
        content = self.gql(user, self.LOCATIONS)
        self.assertNotIn("errors", content)
        found = []

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "location":
                        found.append(item and item["code"])
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(content["data"])
        return found

    def test_a_masked_location_reads_as_null_on_every_graphql_path(self):
        codes, clear = self.location_codes(self.viewer), self.location_codes(self.unmasked)
        self.assertGreaterEqual(len(codes), 9)
        self.assertEqual(codes, [None] * len(clear))
        self.assertEqual(clear, [self.village.code] * len(codes))

    def test_the_json_ext_copies_of_a_masked_location_are_masked(self):
        content = self.gql(self.viewer, '{ individual(id: "%(person)s") { edges { node { jsonExt } } } '
                                        'individualDataSource(upload_Id: "%(upload)s") { edges { node { '
                                        'jsonExt validations } } } }')
        self.assertNotIn("errors", content)
        json_ext = json.loads(content["data"]["individual"]["edges"][0]["node"]["jsonExt"])
        self.assertEqual({key: json_ext[key] for key in LOCATION_KEYS}, dict.fromkeys(LOCATION_KEYS, MASKED))
        self.assertEqual(json_ext["sex"], "F")
        row = content["data"]["individualDataSource"]["edges"][0]["node"]
        row_json = json.loads(row["jsonExt"])
        self.assertEqual((row_json["location_name"], row_json["location_code"]), (MASKED, MASKED))
        self.assertNotIn(self.village.name, json.dumps(row["validations"]))

    def test_mask_for_user_clears_the_masked_location(self):
        person = Individual.objects.get(id=self.person.id)
        mask_for_user(self.viewer, "Individual", person)
        self.assertEqual((person.location, person.location_id), (None, None))
        self.assertEqual(person.json_ext["location_code"], MASKED)
        person = Individual.objects.get(id=self.person.id)
        mask_for_user(self.unmasked, "Individual", person)
        self.assertEqual(person.location_id, self.village.id)

    def exported_cells(self, user, query, field):
        response = self.query('{ %s(fields: ["id", "%s"], fieldsColumns: "{}") }' % (query, field),
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        export = ExportableQueryModel.objects.get(name=content["data"][query])
        export.content.open("rb")
        rows = list(csv.DictReader(io.StringIO(export.content.read().decode())))
        return {cell for row in rows for column, cell in row.items() if column.startswith("location")}

    def test_an_export_writes_a_masked_location_masked(self):
        for query in ("individualExport", "groupExport"):
            with self.subTest(query=query):
                self.assertEqual(self.exported_cells(self.viewer, query, "location.code"), {MASKED})
                self.assertEqual(self.exported_cells(self.unmasked, query, "location.code"), {self.village.code})

    def test_the_location_column_name_reads_and_orders_as_the_masked_location(self):
        self.assertEqual(self.exported_cells(self.viewer, "individualExport", "locationId"), {MASKED})
        self.assertEqual(self.exported_cells(self.unmasked, "individualExport", "locationId"), {str(self.village.id)})
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for user, ordered in ((self.viewer, False), (self.unmasked, True)):
            with CaptureQueriesContext(connection) as queries:
                content = self.gql(user, '{ individual(orderBy: ["locationId"]) { edges { node { id } } } }')
            self.assertNotIn("errors", content)
            orders = [query["sql"].split("ORDER BY", 1)[1] for query in queries.captured_queries
                      if 'FROM "individual_individual"' in query["sql"] and "ORDER BY" in query["sql"]]
            self.assertTrue(orders)
            self.assertEqual(any("location_id" in order for order in orders), ordered, orders)

    def test_a_location_masked_by_its_column_name_is_masked_as_by_its_name(self):
        self.register(person=lambda: ["location_id"], group=lambda: ["location_id"], membership=lambda: [])
        codes = self.location_codes(self.viewer)
        self.assertTrue(codes)
        self.assertEqual(codes, [None] * len(codes))
        for query in ("individualExport", "groupExport"):
            self.assertEqual(self.exported_cells(self.viewer, query, "location.code"), {MASKED})
        person = Individual.objects.get(id=self.person.id)
        mask_for_user(self.viewer, "Individual", person)
        self.assertEqual((person.location_id, person.json_ext["location_name"]), (None, MASKED))

    CONTAINMENT = ('{ locations(code: "%(code)s") { edges { node { code '
                   'individuals { edges { node { id } } } groups { edges { node { id } } } } } } }')

    def listed(self, user):
        """(persons, groups) the location lists to ``user``."""
        content = self.gql(user, self.CONTAINMENT.replace("%(code)s", self.village.code))
        self.assertNotIn("errors", content)
        [node] = [edge["node"] for edge in content["data"]["locations"]["edges"]]
        return len(node["individuals"]["edges"]), len(node["groups"]["edges"])

    def test_a_location_lists_no_person_or_group_while_their_location_is_masked(self):
        self.assertEqual(self.listed(self.viewer), (0, 0))
        self.assertEqual(self.listed(self.unmasked), (1, 1))
        self.register(person=lambda: [], group=lambda: [], membership=lambda: [])
        self.assertEqual(self.listed(self.viewer), (1, 1))

    def test_a_location_masked_on_persons_only_masks_their_household_location(self):
        self.register(person=lambda: ["location"], group=lambda: [], membership=lambda: [])
        query = ('{ group(id: "%(group)s") { edges { node { location { code } } } } '
                 'individual(id: "%(person)s") { edges { node { groupindividuals { edges { node { '
                 'group { location { code } } } } } } } } }')
        for user, code in ((self.viewer, None), (self.unmasked, self.village.code)):
            content = self.gql(user, query)
            self.assertNotIn("errors", content)
            [group] = [edge["node"] for edge in content["data"]["group"]["edges"]]
            [person] = [edge["node"] for edge in content["data"]["individual"]["edges"]]
            [membership] = [edge["node"] for edge in person["groupindividuals"]["edges"]]
            self.assertEqual((group["location"] and group["location"]["code"],
                              membership["group"]["location"] and membership["group"]["location"]["code"]),
                             (code, code), user.username)
        self.assertEqual(self.listed(self.viewer), (0, 0))

    def test_a_location_filter_is_refused_while_the_location_is_masked(self):
        for name in ("individual", "group"):
            query = '{ %s(parentLocation: "%%(village)s", parentLocationLevel: 3) { totalCount } }' % name
            with self.subTest(query=name):
                content = self.gql(self.viewer, query)
                self.assertIn("masked data", json.dumps(content.get("errors")))
                content = self.gql(self.unmasked, query)
                self.assertNotIn("errors", content)
                self.assertEqual(content["data"][name]["totalCount"], 1)

    def test_a_masked_location_given_null_to_an_update_keeps_the_stored_location(self):
        from individual.services import GroupService, IndividualService

        result = IndividualService(self.viewer).update(
            {"id": self.person.id, "last_name": "Byron", "location_id": None})
        self.assertTrue(result["success"], result)
        person = Individual.objects.get(id=self.person.id)
        self.assertEqual((person.last_name, person.location_id), ("Byron", self.village.id))
        result = GroupService(self.viewer).update({"id": self.group.id, "location_id": None})
        self.assertTrue(result["success"], result)
        self.assertEqual(Group.objects.get(id=self.group.id).location_id, self.village.id)

    def test_the_update_task_keeps_a_masked_location_given_null(self):
        from individual.services import IndividualService
        from tasks_management.models import Task

        result = IndividualService(self.viewer).create_update_task(
            {"id": self.person.id, "last_name": "Byron", "location_id": None})
        self.assertTrue(result["success"], result)
        task = Task.objects.filter(entity_id=str(self.person.id)).order_by("-date_created").first()
        self.assertNotIn("location_id", task.data["incoming_data"])

    def test_a_null_location_from_the_masked_data_right_is_written(self):
        from individual.services import IndividualService

        result = IndividualService(self.unmasked).update({"id": self.person.id, "location_id": None})
        self.assertTrue(result["success"], result)
        self.assertIsNone(Individual.objects.get(id=self.person.id).location_id)
