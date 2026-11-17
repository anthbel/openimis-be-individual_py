"""The merge journal a duplicate merge writes into the kept person's ``json_ext`` (``merge_conflicts``, both
persons' differing values) is masked by the person mask for a reader without the masked-data right, on every path
the person mask reaches, while the person mask masks any field."""
import csv
import datetime
import io
import json
from unittest import mock

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.apps import IndividualConfig
from individual.data_masking import GroupIndividualMask, GroupMask, IndividualMask
from individual.models import Group, GroupIndividual, Individual
from individual.services import IndividualService
from location.test_helpers import create_test_village
from tasks_management.models import Task, TaskExecutor, TaskGroup

MASKED = "***************"
UNMASK = "gql_query_enable_viewing_masked_data_perms"
PERMS = ["gql_individual_search_perms", "gql_group_search_perms", "gql_task_search_perms",
         "gql_individual_update_perms", "gql_group_update_perms"]
# The entries deduplication's merge writes: a differing column and a differing json_ext key.
JOURNAL = [
    {"field": "first_name", "kept": "Ada", "retired": "Augusta", "retired_id": "7f0c5d1e-0000-4000-8000-000000000001",
     "at": "2026-11-02T10:00:00", "actor": "dedup_officer"},
    {"field": "phone", "kept": "+257 1", "retired": "+257 9", "retired_id": "7f0c5d1e-0000-4000-8000-000000000001",
     "at": "2026-11-02T10:00:00", "actor": "dedup_officer"},
]
JOURNAL_MASKED = [{key: MASKED for key in entry} for entry in JOURNAL]
CLEAR_VALUES = ("Augusta", "+257 9", "dedup_officer")

PERSON_QUERY = """
query {
  individual(id: "%(person)s") { edges { node { jsonExt } } }
  individualHistory(id: "%(person)s") { edges { node { jsonExt } } }
}
"""
GROUP_QUERY = """
query {
  group(id: "%(group)s") { edges { node { jsonExt head { jsonExt } } } }
  groupHistory(id: "%(group)s") { edges { node { jsonExt } } }
}
"""
EXPORT = '{ individualExport(fields: ["id", "%s"], fieldsColumns: "{}") }'


def json_arg(value):
    return json.dumps(json.dumps(value))


class MergeConflictsMaskingTest(openIMISGraphQLTestCase):
    def setUp(self):
        super().setUp()
        for mask, fields in ((IndividualMask, ["first_name", "json_ext.phone"]), (GroupMask, []),
                             (GroupIndividualMask, [])):
            patcher = mock.patch.dict(mask._sources, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
            mask.register_fields("test", lambda fields=fields: list(fields))
        patcher = mock.patch.object(IndividualConfig, "check_individual_update", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        village = create_test_village({"name": "Merge journal village", "code": "MjV"})
        district = village.parent.parent.code
        self.village = village
        self.admin = create_right_only_user("mjournal_admin", PERMS + [UNMASK], [district])
        self.viewer = create_right_only_user("mjournal_viewer", PERMS, [district])
        self.unmasked = create_right_only_user("mjournal_unmasked", PERMS + [UNMASK], [district])
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}
        self.person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                                 location=village,
                                 json_ext={"phone": "+257 1", "sex": "F", "merge_conflicts": JOURNAL})
        self.person.save(user=self.admin)
        self.group = Group(code="MJ-G1", location=village, json_ext={})
        self.group.save(user=self.admin)
        GroupIndividual(group=self.group, individual=self.person, role=GroupIndividual.Role.HEAD).save(user=self.admin)
        self.group.refresh_from_db()
        self.assertEqual(self.group.json_ext["merge_conflicts"], JOURNAL)

    def query_as(self, user, query):
        response = self.query(query % {"person": self.person.id, "group": self.group.id},
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        return json.loads(response.content)

    def data_as(self, user, query):
        content = self.query_as(user, query)
        self.assertNotIn("errors", content)
        return content["data"]

    def json_exts(self, user, query, *connections):
        data = self.data_as(user, query)
        nodes = [edge["node"] for connection in connections for edge in data[connection]["edges"]]
        self.assertTrue(nodes, data)
        return [json.loads(node["jsonExt"]) for node in nodes] + [
            json.loads(node["head"]["jsonExt"]) for node in nodes if node.get("head")]

    def test_the_person_queries_mask_the_journal(self):
        for json_ext in self.json_exts(self.viewer, PERSON_QUERY, "individual", "individualHistory"):
            self.assertEqual(json_ext["merge_conflicts"], JOURNAL_MASKED)
            self.assertEqual(json_ext["sex"], "F")

    def test_the_group_copy_of_the_head_journal_is_masked(self):
        # The group's first history record predates its head, so it holds no copy.
        json_exts = self.json_exts(self.viewer, GROUP_QUERY, "group", "groupHistory")
        copies = [json_ext["merge_conflicts"] for json_ext in json_exts if "merge_conflicts" in json_ext]
        self.assertGreaterEqual(len(copies), 3)
        self.assertEqual(copies, [JOURNAL_MASKED] * len(copies))

    def test_the_journal_is_clear_with_the_masked_data_right(self):
        for query, connections in ((PERSON_QUERY, ("individual",)), (GROUP_QUERY, ("group",))):
            for json_ext in self.json_exts(self.unmasked, query, *connections):
                self.assertEqual(json_ext["merge_conflicts"], JOURNAL)

    def test_the_journal_is_clear_while_the_person_mask_masks_nothing(self):
        IndividualMask.register_fields("test", None)
        with mock.patch.object(IndividualConfig, "individual_masking_enabled", False):
            [json_ext] = self.json_exts(self.viewer, PERSON_QUERY, "individual")
            self.assertEqual(json_ext["merge_conflicts"], JOURNAL)

    def exported_cells(self, user, field):
        response = self.query(EXPORT % field, headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        export = ExportableQueryModel.objects.get(name=content["data"]["individualExport"])
        export.content.open("rb")
        rows = list(csv.DictReader(io.StringIO(export.content.read().decode())))
        self.assertTrue(rows)
        return [cell for row in rows for cell in row.values()]

    def test_the_export_writes_the_journal_masked(self):
        for field in ("jsonExt", "jsonExt.merge_conflicts"):
            with self.subTest(field=field):
                cells = " ".join(self.exported_cells(self.viewer, field))
                self.assertIn(MASKED, cells)
                for value in CLEAR_VALUES:
                    self.assertNotIn(value, cells)
                self.assertIn("Augusta", " ".join(self.exported_cells(self.unmasked, field)))

    def test_a_custom_filter_reading_the_journal_is_refused(self):
        content = self.query_as(
            self.viewer, '{ individual(customFilters: ["merge_conflicts__icontains__string=Augusta"]) '
                         '{ totalCount } }')
        self.assertIn("errors", content)
        self.assertIn("view masked data", content["errors"][0]["message"])

    def test_a_person_update_task_masks_the_journal(self):
        task_group = TaskGroup(code="mjournal_tasks", completion_policy="ANY")
        task_group.save(user=self.admin)
        TaskExecutor(task_group=task_group, user=self.viewer).save(user=self.admin)
        result = IndividualService(self.admin).create_update_task({
            "id": str(self.person.id), "last_name": "Byron",
            "json_ext": {"phone": "+257 1", "sex": "F", "merge_conflicts": JOURNAL}})
        self.assertTrue(result["success"], result)
        task = Task.objects.get(id=result["data"]["id"])
        task.status = Task.Status.ACCEPTED
        task.task_group = task_group
        task.save(user=self.admin)
        data = self.data_as(self.viewer, '{ task(source: "IndividualService") { edges { node { data } } } }')
        [node] = [edge["node"] for edge in data["task"]["edges"]]
        task_data = json.loads(node["data"])
        for key in ("incoming_data", "current_data"):
            self.assertEqual(task_data[key]["json_ext"]["merge_conflicts"], JOURNAL_MASKED, key)

    def update_person(self, user, json_ext):
        response = self.query(
            f'mutation {{ updateIndividual(input: {{ id: "{self.person.id}" locationId: {self.village.id} '
            f'lastName: "Byron" jsonExt: {json_arg(json_ext)} }}) {{ internalId }} }}',
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        return self._get_mutation_log_node(
            content["data"]["updateIndividual"]["internalId"], self.tokens[user.username])

    def test_an_update_leaving_the_journal_out_keeps_it(self):
        node = self.update_person(self.viewer, {"sex": "M"})
        self.assertEqual(node["status"], 2, node["error"])
        person = Individual.objects.get(id=self.person.id)
        self.assertEqual((person.last_name, person.json_ext["sex"]), ("Byron", "M"))
        self.assertEqual(person.json_ext["merge_conflicts"], JOURNAL)

    def test_an_update_sending_the_masked_journal_back_is_refused(self):
        node = self.update_person(self.viewer, {"sex": "F", "merge_conflicts": JOURNAL_MASKED})
        self.assertNotEqual(node["status"], 2, node)
        self.assertIn("json_ext.merge_conflicts", node["error"])
        self.assertEqual(Individual.objects.get(id=self.person.id).json_ext["merge_conflicts"], JOURNAL)
