"""A soft-deleted person with no location of its own and no household with a location is out of a location-scoped
user's rows, in the person list and in its history; administrators still see it.
A live person without a location keeps today's rule."""
import json

from django.core.cache import caches
from django.test import override_settings

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user, create_test_interactive_user
from individual.models import Group, GroupIndividual, Individual
from location.models import Location

SEARCH = ["gql_individual_search_perms"]


def clear_location_cache():
    caches["location"].clear()
    caches["default"].clear()


def make_location(code, loc_type, parent=None):
    return Location.objects.create(code=code, name=code, type=loc_type, parent=parent, validity_from="2019-06-01",
                                   audit_user_id=-1)


@override_settings(ROW_SECURITY=True)
class DeletedUnlocatedVisibilityTest(openIMISGraphQLTestCase):
    def setUp(self):
        super().setUp()
        clear_location_cache()
        self.addCleanup(clear_location_cache)
        region = make_location("DUL-R", "R")
        district = make_location("DUL-D", "D", region)
        self.village = make_location("DUL-V", "V", make_location("DUL-W", "W", district))
        make_location("DUL-D2", "D", region)
        clear_location_cache()
        self.admin = create_test_interactive_user(username="dul_admin")
        self.far_reader = create_right_only_user("dul_far_reader", SEARCH, ["DUL-D2"])
        self.near_reader = create_right_only_user("dul_near_reader", SEARCH, ["DUL-D"])
        self.deleted = self.person("Dulretired")
        self.deleted.delete(user=self.admin)
        self.live = self.person("Dullive")
        self.deleted_in_household = self.person("Dulhousehold")
        group = Group(code="DUL-G", location=self.village)
        group.save(user=self.admin)
        GroupIndividual(group=group, individual=self.deleted_in_household, role=GroupIndividual.Role.HEAD) \
            .save(user=self.admin)
        self.deleted_in_household.delete(user=self.admin)
        clear_location_cache()

    def person(self, last_name):
        person = Individual(first_name="Dul", last_name=last_name, dob="1990-01-01")
        person.save(user=self.admin)
        return Individual.objects.get(id=person.id)

    def visible(self, user):
        return set(Individual.get_queryset(None, user).values_list("id", flat=True))

    def gql(self, user, query):
        token = BaseTestContext(user=user).get_jwt()
        response = self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        return content["data"]

    def gql_last_names(self, user):
        persons = self.gql(user, '{ individual(isDeleted: true, lastName_Istartswith: "Dul") '
                                 '{ edges { node { lastName } } } }')["individual"]
        history = self.gql(user, '{ individualHistory(lastName_Istartswith: "Dul") '
                                 '{ edges { node { lastName } } } }')["individualHistory"]
        return ({edge["node"]["lastName"] for edge in persons["edges"]},
                {edge["node"]["lastName"] for edge in history["edges"]})

    def test_a_reader_limited_to_another_district_does_not_get_it(self):
        visible = self.visible(self.far_reader)
        self.assertNotIn(self.deleted.id, visible)
        self.assertIn(self.live.id, visible)
        persons, history = self.gql_last_names(self.far_reader)
        self.assertNotIn("Dulretired", persons)
        self.assertNotIn("Dulretired", history)
        self.assertIn("Dullive", history)

    def test_a_deleted_person_whose_household_has_a_location_keeps_the_household_rule(self):
        self.assertIn(self.deleted_in_household.id, self.visible(self.near_reader))

    def test_administrators_still_see_it(self):
        self.assertIn(self.deleted.id, self.visible(self.admin))
        persons, history = self.gql_last_names(self.admin)
        self.assertIn("Dulretired", persons)
        self.assertIn("Dulretired", history)
