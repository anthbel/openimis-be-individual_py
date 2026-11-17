"""A household without a location is visible to a location-scoped reader unless one of its live members has a
location outside the reader's locations: it would show that member (its head, its ``jsonExt``)."""
import csv
import io
import json

from django.core.cache import caches
from django.test import override_settings
from graphql_relay import to_global_id

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user, create_test_interactive_user
from individual.models import Group, GroupIndividual, Individual
from individual.services import GroupIndividualService
from location.models import Location

PERMS = ["gql_individual_search_perms", "gql_group_search_perms"]


def clear_location_cache():
    caches["location"].clear()
    caches["default"].clear()


def make_location(code, loc_type, parent=None):
    return Location.objects.create(code=code, name=code, type=loc_type, parent=parent, validity_from="2019-06-01",
                                   audit_user_id=-1)


@override_settings(ROW_SECURITY=True)
class UnlocatedHouseholdGateTest(openIMISGraphQLTestCase):
    """Districts D and D2; the far reader is limited to D2, the near reader to D. UGH-G0 has no location: U (no
    location) heads it, X (located in D) belongs to it. UGH-G3 has no location and only W, who has none. UGH-G2
    is in D2."""

    def setUp(self):
        super().setUp()
        clear_location_cache()
        self.addCleanup(clear_location_cache)
        region = make_location("UGH-R", "R")
        village = make_location("UGH-V", "V", make_location("UGH-W", "W", make_location("UGH-D", "D", region)))
        village2 = make_location("UGH-V2", "V", make_location("UGH-W2", "W", make_location("UGH-D2", "D", region)))
        clear_location_cache()
        self.admin = create_test_interactive_user(username="ugh_admin")
        self.far = create_right_only_user("ugh_far", PERMS, ["UGH-D2"])
        self.near = create_right_only_user("ugh_near", PERMS, ["UGH-D"])
        self.g0 = self.group("UGH-G0", None)
        self.join(self.g0, self.person("Ughunloc", None), GroupIndividual.Role.HEAD)
        self.x = self.person("Ughxlocd", village)
        self.join(self.g0, self.x, GroupIndividual.Role.SON)
        self.g3 = self.group("UGH-G3", None)
        self.join(self.g3, self.person("Ughwunloc", None), GroupIndividual.Role.HEAD)
        self.g2 = self.group("UGH-G2", village2)
        self.join(self.g2, self.person("Ughqind", village2), GroupIndividual.Role.HEAD)
        clear_location_cache()

    def group(self, code, location):
        group = Group(code=code, location=location)
        group.save(user=self.admin)
        return group

    def person(self, last_name, location):
        person = Individual(first_name="Ugh", last_name=last_name, dob="1990-01-01", location=location)
        person.save(user=self.admin)
        return person

    def join(self, group, person, role):
        result = GroupIndividualService(self.admin).create(
            {"group_id": group.id, "individual_id": person.id, "role": role})
        self.assertTrue(result["success"], result)

    def gql(self, user, query):
        token = BaseTestContext(user=user).get_jwt()
        content = json.loads(self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"}).content)
        self.assertNotIn("errors", content)
        return content["data"]

    def codes(self, user):
        return set(Group.get_queryset(None, user).values_list("code", flat=True))

    def test_the_member_keeps_their_location_in_the_unlocated_household(self):
        self.assertIsNone(Group.objects.get(id=self.g0.id).location_id)
        self.assertIsNotNone(Individual.objects.get(id=self.x.id).location_id)
        self.assertTrue(GroupIndividual.objects.filter(group=self.g0, individual=self.x, is_deleted=False).exists())

    def test_an_unlocated_household_with_a_member_located_elsewhere_is_hidden_from_the_reader(self):
        self.assertEqual(self.codes(self.far), {"UGH-G3", "UGH-G2"})
        data = self.gql(self.far, '{ group { edges { node { code jsonExt head { lastName } } } } '
                                  'groupHistory { edges { node { code jsonExt head { lastName } } } } }')
        self.assertNotIn("UGH-G0", json.dumps(data))
        self.assertNotIn("Ughxlocd", json.dumps(data))
        node = self.gql(self.far, '{ node(id: "%s") { id } }' % to_global_id("GroupGQLType", self.g0.id))
        self.assertIsNone(node["node"])
        before = set(ExportableQueryModel.objects.values_list("id", flat=True))
        export = self.gql(self.far, '{ groupExport(fields: ["id", "json_ext"], fieldsColumns: "{}") }')
        self.assertTrue(export["groupExport"])
        rows = []
        for file in ExportableQueryModel.objects.exclude(id__in=before):
            file.content.open("rb")
            rows += list(csv.DictReader(io.StringIO(file.content.read().decode())))
        self.assertEqual({row["id"] for row in rows}, {str(self.g3.id), str(self.g2.id)})
        self.assertNotIn("Ughxlocd", json.dumps(rows))

    def test_no_membership_of_the_hidden_household_reaches_the_reader(self):
        head = GroupIndividual.objects.get(group=self.g0, individual__last_name="Ughunloc")
        queries = {
            "memberships": '{ groupIndividual(group_Id: "%s") { edges { node { group { code jsonExt } } } } }'
                           % self.g0.id,
            "membership node": '{ node(id: "%s") { ... on GroupIndividualGQLType { group { code jsonExt } } } }'
                               % to_global_id("GroupIndividualGQLType", head.id),
            "person memberships": '{ individual { edges { node { lastName groupindividuals { edges { node '
                                  '{ group { code jsonExt } } } } } } } }',
            "membership history": '{ groupIndividualHistory { edges { node { group { code jsonExt } } } } }',
        }
        for case, query in queries.items():
            with self.subTest(case=case):
                text = json.dumps(self.gql(self.far, query))
                self.assertNotIn("UGH-G0", text)
                self.assertNotIn("Ughxlocd", text)
        self.assertFalse(GroupIndividual.get_queryset(None, self.far).filter(group=self.g0).exists())
        for user in (self.near, self.admin):
            with self.subTest(user=user.username):
                self.assertEqual(GroupIndividual.get_queryset(None, user).filter(group=self.g0).count(), 2)
                text = json.dumps(self.gql(user, queries["memberships"]))
                self.assertIn("Ughxlocd", text)

    def test_the_readers_of_every_member_location_and_administrators_still_see_it(self):
        self.assertEqual(self.codes(self.near), {"UGH-G0", "UGH-G3"})
        self.assertEqual(self.codes(self.admin), {"UGH-G0", "UGH-G3", "UGH-G2"})
        data = self.gql(self.near, '{ group(code_Iexact: "UGH-G0") { edges { node { head { lastName } } } } }')
        self.assertEqual(data["group"]["edges"][0]["node"]["head"]["lastName"], "Ughunloc")

    def test_an_unlocated_household_whose_members_have_no_location_stays_visible_to_every_reader(self):
        for user in (self.far, self.near):
            with self.subTest(user=user.username):
                self.assertIn("UGH-G3", self.codes(user))
                history = self.gql(user, '{ groupHistory(id: "%s") { totalCount } }' % self.g3.id)
                self.assertGreater(history["groupHistory"]["totalCount"], 0)

    def test_a_former_member_located_elsewhere_hides_nothing(self):
        membership = GroupIndividual.objects.get(group=self.g0, individual=self.x)
        result = GroupIndividualService(self.admin).delete({"id": str(membership.id)})
        self.assertTrue(result["success"], result)
        self.assertIn("UGH-G0", self.codes(self.far))
