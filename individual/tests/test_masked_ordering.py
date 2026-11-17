from django.test import TestCase

from core.test_helpers import create_right_only_user
from individual.data_masking import masked_order_by
from individual.models import Group, GroupIndividual, Individual
from individual.tests.test_data_masking import SEARCH_PERMS, RegisteredFieldsTestMixin


class MaskedOrderByTest(RegisteredFieldsTestMixin, TestCase):
    """For a user without the masked-data right, an orderBy term whose path reads a masked field or traverses a
    JSON field, through any relation, is left out, and the primary key orders last."""

    def setUp(self):
        super().setUp()
        self.viewer = create_right_only_user("mask_ord_viewer", SEARCH_PERMS, [])
        self.unmasked = create_right_only_user(
            "mask_ord_unmasked", SEARCH_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [])
        self.register(person=lambda: ["first_name"], group=lambda: [], membership=lambda: [])

    def test_a_masked_column_is_left_out_through_any_relation(self):
        self.assertEqual(masked_order_by(self.viewer, Individual, ["-firstName", "dateCreated"]),
                         ["dateCreated", "id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["individual__firstName", "role"]),
                         ["role", "id"])
        self.assertEqual(masked_order_by(self.viewer, Group, ["groupindividuals__individual__firstName"]), ["id"])

    def test_a_json_field_is_left_out_through_any_relation_masked_or_not(self):
        self.assertEqual(masked_order_by(self.viewer, Group, ["jsonExt__roof", "code"]), ["code", "id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["-group__jsonExt__income"]), ["id"])
        self.assertEqual(masked_order_by(self.viewer, Individual, ["jsonExt"]), ["id"])

    def test_a_name_that_is_no_field_is_left_out_unless_an_alias_names_what_it_reads(self):
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["headName"]), ["id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["headName"],
                                         {"head_name": "individual__first_name"}), ["id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["-headName"],
                                         {"head_name": "individual__last_name"}), ["-headName", "id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["isEligible"], {"is_eligible": ()}),
                         ["isEligible", "id"])

    def test_a_user_with_the_right_keeps_every_term(self):
        order = ["individual__firstName", "-group__jsonExt__income", "headName"]
        self.assertEqual(masked_order_by(self.unmasked, GroupIndividual, order), [*order, "id"])

    def test_the_primary_key_is_not_added_twice_and_an_empty_order_stays_empty(self):
        self.assertEqual(masked_order_by(self.viewer, Individual, ["-id"]), ["-id"])
        self.assertEqual(masked_order_by(self.viewer, Individual, "lastName"), ["lastName", "id"])
        self.assertEqual(masked_order_by(self.viewer, Individual, None), None)
        self.assertEqual(masked_order_by(self.viewer, Individual, ["?"]), ["?", "id"])
