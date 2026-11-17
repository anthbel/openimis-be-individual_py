import datetime

from django.test import TestCase

from core.test_helpers import create_test_interactive_user
from individual.models import Group, GroupIndividual, Individual
from location.test_helpers import create_test_village


class MemberLocationTest(TestCase):
    """Saving a household with a location moves its live members to it; a former member keeps their location."""

    def setUp(self):
        self.user = create_test_interactive_user(username="member_location_admin")
        self.old_village = create_test_village({"name": "Old village", "code": "MlO"})
        self.new_village = create_test_village({"name": "New village", "code": "MlN"})
        self.old_group = Group(code="ML-OLD", location=self.old_village)
        self.old_group.save(user=self.user)
        self.new_group = Group(code="ML-NEW", location=self.new_village)
        self.new_group.save(user=self.user)
        self.mover = Individual(first_name="Mover", last_name="Person", dob=datetime.date(1980, 1, 2),
                                location=self.old_village)
        self.mover.save(user=self.user)
        self.stayer = Individual(first_name="Stayer", last_name="Person", dob=datetime.date(1981, 1, 2),
                                 location=self.old_village)
        self.stayer.save(user=self.user)
        GroupIndividual(group=self.old_group, individual=self.stayer,
                        role=GroupIndividual.Role.HEAD).save(user=self.user)
        old_membership = GroupIndividual(group=self.old_group, individual=self.mover, role=GroupIndividual.Role.SON)
        old_membership.save(user=self.user)
        old_membership.delete(user=self.user)
        GroupIndividual(group=self.new_group, individual=self.mover, role=GroupIndividual.Role.HEAD).save(user=self.user)

    def test_saving_the_former_household_leaves_a_former_member_where_they_live(self):
        self.mover.refresh_from_db()
        self.assertEqual(self.mover.location_id, self.new_village.id)
        third_village = create_test_village({"name": "Third village", "code": "MlT"})
        self.old_group.refresh_from_db()
        self.old_group.location = third_village
        self.old_group.save(user=self.user)
        self.mover.refresh_from_db()
        self.stayer.refresh_from_db()
        self.assertEqual(self.mover.location_id, self.new_village.id)
        self.assertEqual(self.stayer.location_id, third_village.id)
