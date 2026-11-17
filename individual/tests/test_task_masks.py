"""The tasks of individual's services are masked by the person, group and membership masks for a reader without
the masked-data right: their data, the names their business data serializers write, and the entity string of a
task on a person."""
import datetime
import json
from unittest import mock

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.data_masking import GroupIndividualMask, GroupMask, IndividualMask
from individual.models import Group, GroupIndividual, Individual
from individual.services import GroupIndividualService, IndividualService
from location.test_helpers import create_test_village
from tasks_management.models import Task, TaskExecutor, TaskGroup

MASKED = "***************"
TASK_PERMS = ["gql_task_search_perms", "gql_individual_search_perms", "gql_group_search_perms"]


def person_fields():
    return ["first_name", "last_name", "dob", "json_ext.phone"]


def group_fields():
    return ["code"]


def membership_fields():
    return ["role"]


TASK_QUERY = """
query { task(source: "%(source)s") { edges { node { data businessData } } } }
"""


class TaskMasksTest(openIMISGraphQLTestCase):
    def setUp(self):
        super().setUp()
        for mask, source in ((IndividualMask, person_fields), (GroupMask, group_fields),
                             (GroupIndividualMask, membership_fields)):
            patcher = mock.patch.dict(mask._sources, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
            mask.register_fields("test", source)
            self.addCleanup(mask.register_fields, "test", None)
        village = create_test_village({"name": "Task mask village", "code": "TmV"})
        district = village.parent.parent.code
        self.admin = create_right_only_user(
            "task_mask_admin", TASK_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        self.reader = create_right_only_user("task_mask_reader", TASK_PERMS, [district])
        self.unmasked = create_right_only_user(
            "task_mask_unmasked", TASK_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        self.task_group = TaskGroup(code="task_mask_group", completion_policy="ANY")
        self.task_group.save(user=self.admin)
        for user in (self.reader, self.unmasked):
            TaskExecutor(task_group=self.task_group, user=user).save(user=self.admin)
        self.person = Individual(
            first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2), location=village,
            json_ext={"phone": "+257 1", "sex": "F"})
        self.person.save(user=self.admin)
        self.group = Group(code="TM-G1", location=village, json_ext={})
        self.group.save(user=self.admin)
        self.membership = GroupIndividual(group=self.group, individual=self.person, role="HEAD")
        self.membership.save(user=self.admin)

    def _accept(self, result):
        self.assertTrue(result["success"], result)
        task = Task.objects.get(id=result["data"]["id"])
        task.status = Task.Status.ACCEPTED
        task.task_group = self.task_group
        task.save(user=self.admin)
        return task

    def _person_update_task(self):
        return self._accept(IndividualService(self.admin).create_update_task({
            "id": str(self.person.id), "first_name": "Adah", "last_name": "Byron", "dob": "1981-02-03",
            "json_ext": {"phone": "+257 2", "sex": "F"},
        }))

    def _membership_update_task(self):
        return self._accept(GroupIndividualService(self.admin).create_update_task({
            "id": str(self.membership.id), "group_id": str(self.group.id), "role": "RECIPIENT",
        }))

    def _node(self, user, source):
        token = BaseTestContext(user=user).get_jwt()
        response = self.query(TASK_QUERY % {"source": source}, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        self.assertResponseNoErrors(response)
        node, = [edge["node"] for edge in json.loads(response.content)["data"]["task"]["edges"]]
        return {key: json.loads(value) for key, value in node.items()}

    def test_a_person_update_task_is_masked_without_the_masked_data_right(self):
        self._person_update_task()
        node = self._node(self.reader, "IndividualService")
        for data in (node["data"], node["businessData"]):
            self.assertEqual(
                {key: data["incoming_data"][key] for key in ("first_name", "last_name", "dob")}, {
                    "first_name": MASKED, "last_name": MASKED, "dob": MASKED})
            self.assertEqual(data["incoming_data"]["json_ext"], {"phone": MASKED, "sex": "F"})
            self.assertEqual(data["current_data"]["first_name"], MASKED)
            self.assertEqual(data["current_data"]["json_ext"], {"phone": MASKED, "sex": "F"})

    def test_a_masked_location_reads_as_null_in_a_person_update_task(self):
        self._accept(IndividualService(self.admin).create_update_task({
            "id": str(self.person.id), "last_name": "Byron", "location_id": self.person.location_id}))
        with mock.patch.dict(IndividualMask._sources, {"test": lambda: ["location"]}, clear=True):
            node = self._node(self.reader, "IndividualService")
        located = [(name, copy, key) for name, value in node.items() for copy in ("incoming_data", "current_data")
                   for key in ("location", "location_id") if key in value[copy]]
        self.assertTrue(located)
        for name, copy, key in located:
            self.assertIsNone(node[name][copy][key], (name, copy, key))
        self.assertEqual(node["data"]["incoming_data"]["last_name"], "Byron")
        node = self._node(self.unmasked, "IndividualService")
        self.assertEqual(node["data"]["incoming_data"]["location_id"], self.person.location_id)

    def test_a_membership_task_shows_the_person_name_and_group_code_masked(self):
        self._membership_update_task()
        node = self._node(self.reader, "GroupIndividualService")
        self.assertEqual(node["businessData"]["incoming_data"]["id"], f"{MASKED} {MASKED}")
        self.assertEqual(node["businessData"]["incoming_data"]["group_id"], MASKED)
        self.assertEqual(node["data"]["incoming_data"]["role"], MASKED)
        self.assertEqual(node["businessData"]["incoming_data"]["role"], MASKED)

    def test_a_reader_with_the_masked_data_right_reads_the_task(self):
        self._person_update_task()
        self._membership_update_task()
        node = self._node(self.unmasked, "IndividualService")
        self.assertEqual(node["data"]["incoming_data"]["first_name"], "Adah")
        self.assertEqual(node["data"]["current_data"]["json_ext"]["phone"], "+257 1")
        node = self._node(self.unmasked, "GroupIndividualService")
        self.assertEqual(node["businessData"]["incoming_data"]["id"], "Ada Lovelace")
        self.assertEqual(node["businessData"]["incoming_data"]["group_id"], "TM-G1")

    def test_the_entity_string_of_a_task_on_a_person_is_masked_while_a_name_column_is(self):
        from individual.task_masking import HistoricalTaskMask, TaskMask
        task = self._person_update_task()
        for mask, row in ((TaskMask(), task), (HistoricalTaskMask(), task.history.latest("history_date"))):
            row.entity_string = str(self.person)
            mask.apply_mask(row)
            self.assertEqual(row.entity_string, MASKED)
        with mock.patch.dict(IndividualMask._sources, {"test": lambda: ["dob"]}, clear=True):
            task.entity_string = str(self.person)
            TaskMask().apply_mask(task)
            self.assertEqual(task.entity_string, str(self.person))

    def test_a_task_of_another_source_keeps_its_data(self):
        from individual.task_masking import TaskMask
        task = Task(source="other_source", data={"incoming_data": {"first_name": "Ada"}}, entity=self.group)
        task.entity_string = str(self.group)
        TaskMask().apply_mask(task)
        self.assertEqual(task.data, {"incoming_data": {"first_name": "Ada"}})
        self.assertEqual(task.entity_string, str(self.group))

    def test_a_registered_source_masks_its_tasks_and_their_history(self):
        from individual.task_masking import HistoricalTaskMask, TaskMask

        def masker(task):
            task.data["evidence"] = MASKED

        TaskMask.register_source("other_module_source", masker)
        self.addCleanup(TaskMask.register_source, "other_module_source", None)
        task = Task(source="other_module_source", data={"evidence": "Ada", "kind": "demographic"})
        task.save(user=self.admin)
        TaskMask().apply_mask(task)
        self.assertEqual(task.data, {"evidence": MASKED, "kind": "demographic"})
        history = Task.objects.get(id=task.id).history.latest("history_date")
        HistoricalTaskMask().apply_mask(history)
        self.assertEqual(history.data["evidence"], MASKED)
        TaskMask.register_source("other_module_source", None)
        self.assertEqual(TaskMask().apply_mask(Task(source="other_module_source", data={"evidence": "Ada"})).data,
                         {"evidence": "Ada"})
