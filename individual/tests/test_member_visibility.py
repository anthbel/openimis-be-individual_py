"""A membership whose person ``Individual.get_queryset`` hides from a reader is not listed to that reader, on the
membership list, under a group and in the membership history. Same-district readers, administrators and live
persons are unchanged."""
import csv
import io
import json

from django.core.cache import caches
from django.test import override_settings

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user, create_test_interactive_user
from individual.models import Group, GroupIndividual, Individual
from individual.schema import Query
from individual.services import IndividualService
from location.models import Location

PERMS = ["gql_individual_search_perms", "gql_group_search_perms"]


def clear_location_cache():
    caches["location"].clear()
    caches["default"].clear()


def make_location(code, loc_type, parent=None):
    return Location.objects.create(code=code, name=code, type=loc_type, parent=parent, validity_from="2019-06-01",
                                   audit_user_id=-1)


@override_settings(ROW_SECURITY=True)
class MemberVisibilityTest(openIMISGraphQLTestCase):
    def setUp(self):
        super().setUp()
        clear_location_cache()
        self.addCleanup(clear_location_cache)
        region = make_location("MVI-R", "R")
        district = make_location("MVI-D", "D", region)
        village = make_location("MVI-V", "V", make_location("MVI-W", "W", district))
        make_location("MVI-D2", "D", region)
        clear_location_cache()
        self.admin = create_test_interactive_user(username="mvi_admin")
        self.far_reader = create_right_only_user("mvi_far_reader", PERMS, ["MVI-D2"])
        self.near_reader = create_right_only_user("mvi_near_reader", PERMS, ["MVI-D"])
        self.unlocated_group = Group(code="MVI-G0")
        self.unlocated_group.save(user=self.admin)
        self.located_group = Group(code="MVI-G1", location=village)
        self.located_group.save(user=self.admin)
        self.live = self.member(self.unlocated_group, "Mvilive", GroupIndividual.Role.HEAD)
        self.retired = self.member(self.unlocated_group, "Mviretired", GroupIndividual.Role.NOT_RELATED)
        self.retired_in_village = self.member(self.located_group, "Mvivillage", GroupIndividual.Role.HEAD)
        for person in (self.retired, self.retired_in_village):
            person.delete(user=self.admin)
        clear_location_cache()

    def member(self, group, last_name, role):
        person = Individual(first_name="Mvi", last_name=last_name, dob="1990-01-01")
        person.save(user=self.admin)
        GroupIndividual(group=group, individual=person, role=role).save(user=self.admin)
        return Individual.objects.get(id=person.id)

    def names(self, user):
        return set(GroupIndividual.get_queryset(None, user).values_list("individual__last_name", flat=True))

    def gql(self, user, query):
        content = self.gql_content(user, query)
        self.assertNotIn("errors", content)
        return content["data"]

    def gql_content(self, user, query):
        token = BaseTestContext(user=user).get_jwt()
        return json.loads(self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"}).content)

    def exported(self, user, export, field, arguments=""):
        """The values of ``field`` in the file ``export`` writes for ``user``."""
        data = self.gql(user, '{ %s(%sfields: ["%s"], fieldsColumns: "{\\"%s\\": \\"value\\"}") }'
                        % (export, arguments, field, Query._adjust_notation(field)))
        file = ExportableQueryModel.objects.get(name=data[export])
        file.content.open("rb")
        return {row["value"] for row in csv.DictReader(io.StringIO(file.content.read().decode()))}

    def exported_members(self, user, group):
        return self.exported(user, "groupIndividualExport", "individual.lastName", 'group_Id: "%s", ' % group.id)

    def listed(self, user, group):
        direct = self.gql(user, '{ groupIndividual(group_Id: "%s") { edges { node { individual { lastName } } } } }'
                          % group.id)["groupIndividual"]
        nested = self.gql(user, '{ group(id: "%s") { edges { node { groupindividuals { edges { node '
                                '{ individual { lastName } } } } } } } }' % group.id)["group"]
        return ({edge["node"]["individual"]["lastName"] for edge in direct["edges"]},
                {member["node"]["individual"]["lastName"] for edge in nested["edges"]
                 for member in edge["node"]["groupindividuals"]["edges"]})

    def test_a_reader_of_another_district_gets_no_membership_of_the_hidden_person(self):
        visible = Individual.get_queryset(None, self.far_reader).values_list("id", flat=True)
        self.assertNotIn(self.retired.id, set(visible))
        self.assertNotIn("Mviretired", self.names(self.far_reader))
        self.assertIn("Mvilive", self.names(self.far_reader))
        self.assertEqual(self.listed(self.far_reader, self.unlocated_group), ({"Mvilive"}, {"Mvilive"}))

    def test_a_reader_of_the_person_district_gets_no_membership_of_a_person_the_same_rule_hides(self):
        self.assertNotIn("Mviretired", self.names(self.near_reader))
        self.assertEqual(self.listed(self.near_reader, self.unlocated_group), ({"Mvilive"}, {"Mvilive"}))

    def test_a_deleted_person_of_a_located_household_stays_listed_to_the_district_of_that_household(self):
        self.assertIn("Mvivillage", self.names(self.near_reader))
        self.assertNotIn("Mvivillage", self.names(self.far_reader))
        self.assertEqual(self.listed(self.near_reader, self.located_group), ({"Mvivillage"}, {"Mvivillage"}))

    def test_administrators_see_every_membership(self):
        self.assertEqual(self.names(self.admin), {"Mvilive", "Mviretired", "Mvivillage"})
        self.assertEqual(self.listed(self.admin, self.unlocated_group)[0], {"Mvilive", "Mviretired"})

    def test_the_membership_history_hides_the_person_from_the_same_readers(self):
        query = '{ groupIndividualHistory { edges { node { individual { lastName } } } } }'
        far = self.gql(self.far_reader, query)["groupIndividualHistory"]["edges"]
        near = self.gql(self.admin, query)["groupIndividualHistory"]["edges"]
        self.assertNotIn("Mviretired", {edge["node"]["individual"]["lastName"] for edge in far})
        self.assertIn("Mviretired", {edge["node"]["individual"]["lastName"] for edge in near})

    def test_service_delete_of_a_person_leaves_the_membership(self):
        person = self.member(self.unlocated_group, "Mviservice", GroupIndividual.Role.NOT_RELATED)
        result = IndividualService(self.admin).delete({"id": str(person.id)})
        self.assertTrue(result["success"], result)
        membership = GroupIndividual.objects.get(individual_id=person.id)
        self.assertFalse(membership.is_deleted)

    def test_the_membership_export_lists_what_the_membership_list_lists_to_the_same_reader(self):
        self.member(self.located_group, "Mvihousehold", GroupIndividual.Role.SON)
        for user, group in ((self.near_reader, self.located_group), (self.near_reader, self.unlocated_group),
                            (self.far_reader, self.unlocated_group), (self.admin, self.located_group),
                            (self.admin, self.unlocated_group)):
            with self.subTest(user=user.username, group=group.code):
                self.assertEqual(self.exported_members(user, group), self.listed(user, group)[0])
        self.assertEqual(self.exported_members(self.near_reader, self.located_group), {"Mvivillage", "Mvihousehold"})
        self.assertEqual(self.exported_members(self.admin, self.unlocated_group), {"Mvilive", "Mviretired"})

    def test_a_reader_of_another_district_exports_no_membership_of_a_household_of_that_district(self):
        self.member(self.located_group, "Mvihousehold", GroupIndividual.Role.SON)
        self.assertEqual(self.listed(self.far_reader, self.located_group), (set(), set()))
        before = set(ExportableQueryModel.objects.values_list("id", flat=True))
        self.gql_content(self.far_reader, '{ groupIndividualExport(group_Id: "%s", fields: ["individual.lastName"], '
                                          'fieldsColumns: "{}") }' % self.located_group.id)
        for file in ExportableQueryModel.objects.exclude(id__in=before):
            file.content.open("rb")
            self.assertEqual(list(csv.DictReader(io.StringIO(file.content.read().decode()))), [])

    def test_the_membership_export_leaves_out_the_hidden_person(self):
        for user in (self.far_reader, self.near_reader):
            with self.subTest(user=user.username):
                self.assertEqual(self.exported_members(user, self.unlocated_group), {"Mvilive"})

    def test_the_person_and_household_exports_leave_out_what_the_reader_may_not_see(self):
        self.member(self.located_group, "Mvihousehold", GroupIndividual.Role.SON)
        self.assertEqual(self.exported(self.far_reader, "individualExport", "lastName"), {"Mvilive"})
        self.assertEqual(self.exported(self.near_reader, "individualExport", "lastName"), {"Mvilive", "Mvihousehold"})
        self.assertEqual(self.exported(self.near_reader, "individualExport", "lastName", "isDeleted: true, "),
                         {"Mvivillage"})
        self.assertEqual(self.exported(self.admin, "individualExport", "lastName", "isDeleted: true, "),
                         {"Mviretired", "Mvivillage"})
        self.assertEqual(self.exported(self.far_reader, "groupExport", "code"), {"MVI-G0"})
        self.assertEqual(self.exported(self.near_reader, "groupExport", "code"), {"MVI-G0", "MVI-G1"})
