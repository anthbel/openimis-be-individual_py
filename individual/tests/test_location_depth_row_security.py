"""Row security of groups, individuals and memberships in a hierarchy deeper than four levels."""
from unittest import mock

from django.core.cache import caches
from django.test import TestCase, override_settings

from core.test_helpers import create_right_only_user, create_test_interactive_user
from individual.models import Group, GroupIndividual, Individual
from location.apps import LocationConfig
from location.models import Location

FIVE_LEVELS = ["R", "D", "W", "V", "S"]
SEARCH = ["gql_group_search_perms", "gql_individual_search_perms"]


def clear_location_cache():
    caches["location"].clear()
    caches["default"].clear()


def make_location(code, loc_type, parent=None):
    return Location.objects.create(
        code=code, name=code, type=loc_type, parent=parent,
        validity_from="2019-06-01", audit_user_id=-1,
    )


@override_settings(ROW_SECURITY=True)
class FifthLevelRowSecurityTest(TestCase):
    def setUp(self):
        patcher = mock.patch.object(LocationConfig, "location_types", FIVE_LEVELS)
        patcher.start()
        self.addCleanup(patcher.stop)
        clear_location_cache()
        self.addCleanup(clear_location_cache)

        self.admin = create_test_interactive_user(username="depth_admin")
        district = make_location("DPT-D", "D", make_location("DPT-R", "R"))
        village = make_location("DPT-V", "V", make_location("DPT-W", "W", district))
        sub_village = make_location("DPT-S", "S", village)
        other_district = make_location("DPT-D2", "D", district.parent)
        other_sub_village = make_location(
            "DPT-S2", "S", make_location("DPT-V2", "V", make_location("DPT-W2", "W", other_district)),
        )
        clear_location_cache()
        self.user = create_right_only_user("depth_user", SEARCH, ["DPT-D"])

        self.village_household = self.household("V", village)
        self.sub_village_household = self.household("S", sub_village)
        self.other_district_household = self.household("O", other_sub_village)
        clear_location_cache()

    def household(self, suffix, location):
        group = Group(code=f"DPT-G{suffix}", location=location)
        group.save(user=self.admin)
        person = Individual(first_name=suffix, last_name="Depth", dob="1990-01-01", location=location)
        person.save(user=self.admin)
        membership = GroupIndividual(group=group, individual=person, role=GroupIndividual.Role.HEAD)
        membership.save(user=self.admin)
        return group, person, membership

    def visible(self, model):
        return set(model.get_queryset(None, self.user).values_list("id", flat=True))

    def test_district_user_sees_groups_at_every_level_below_the_district(self):
        groups = self.visible(Group)
        self.assertIn(self.village_household[0].id, groups)
        self.assertIn(self.sub_village_household[0].id, groups)
        self.assertNotIn(self.other_district_household[0].id, groups)

    def test_district_user_sees_individuals_at_every_level_below_the_district(self):
        persons = self.visible(Individual)
        self.assertIn(self.village_household[1].id, persons)
        self.assertIn(self.sub_village_household[1].id, persons)
        self.assertNotIn(self.other_district_household[1].id, persons)

    def test_district_user_sees_memberships_at_every_level_below_the_district(self):
        memberships = self.visible(GroupIndividual)
        self.assertIn(self.village_household[2].id, memberships)
        self.assertIn(self.sub_village_household[2].id, memberships)
        self.assertNotIn(self.other_district_household[2].id, memberships)
