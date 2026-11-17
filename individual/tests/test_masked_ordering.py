from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from core.data_masking import MaskingClassStorage
from core.test_helpers import create_right_only_user
from individual.data_masking import masked_order_by, reads_masked_field
from individual.models import (
    Group, GroupIndividual, Individual, IndividualDataSource, IndividualDataSourceUpload,
)
from individual.tests.test_data_masking import SEARCH_PERMS, RegisteredFieldsTestMixin


class MaskedOrderByTest(RegisteredFieldsTestMixin, TestCase):
    """For a user without the masked-data right, an orderBy term whose path reads a masked field (a masked
    json_ext key of a masked model included) or traverses a JSON field no mask describes, through any relation,
    is left out, and the primary key orders last."""

    def setUp(self):
        super().setUp()
        self.viewer = create_right_only_user("mask_ord_viewer", SEARCH_PERMS, [])
        self.unmasked = create_right_only_user(
            "mask_ord_unmasked", SEARCH_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [])
        self.register(person=lambda: ["first_name", "json_ext.phone"], group=lambda: [], membership=lambda: [])

    def test_a_masked_column_is_left_out_through_any_relation(self):
        self.assertEqual(masked_order_by(self.viewer, Individual, ["-firstName", "dateCreated"]),
                         ["dateCreated", "id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["individual__firstName", "role"]),
                         ["role", "id"])
        self.assertEqual(masked_order_by(self.viewer, Group, ["groupindividuals__individual__firstName"]), ["id"])

    def test_a_masked_json_ext_key_of_a_masked_model_is_left_out_through_any_relation(self):
        self.assertEqual(masked_order_by(self.viewer, Individual, ["jsonExt__phone", "lastName"]), ["lastName", "id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["-individual__jsonExt__phone"]), ["id"])
        self.assertEqual(masked_order_by(self.viewer, Individual, ["jsonExt"]), ["id"])
        self.assertEqual(masked_order_by(self.viewer, Group, ["groupindividuals__individual__jsonExt__firstName"]),
                         ["id"])

    def test_an_unmasked_json_ext_key_of_a_masked_model_still_orders(self):
        self.assertEqual(masked_order_by(self.viewer, Group, ["jsonExt__roof", "code"]),
                         ["jsonExt__roof", "code", "id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["-individual__jsonExt__sex"]),
                         ["-individual__jsonExt__sex", "id"])

    def test_a_json_field_no_mask_describes_is_left_out_masked_key_or_not(self):
        self.assertEqual(masked_order_by(self.viewer, IndividualDataSource, ["upload__jsonExt__note"]), ["id"])
        self.assertEqual(masked_order_by(self.viewer, IndividualDataSourceUpload, ["jsonExt__note", "sourceName"]),
                         ["sourceName", "id"])

    def test_a_name_that_is_no_field_is_left_out_unless_an_alias_names_what_it_reads(self):
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["headName"]), ["id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["headName"],
                                         {"head_name": "individual__first_name"}), ["id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["-headName"],
                                         {"head_name": "individual__last_name"}), ["-headName", "id"])
        self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["isEligible"], {"is_eligible": ()}),
                         ["isEligible", "id"])

    def test_a_user_with_the_right_keeps_every_term(self):
        order = ["individual__firstName", "-individual__jsonExt__phone", "headName"]
        self.assertEqual(masked_order_by(self.unmasked, GroupIndividual, order), [*order, "id"])
        self.assertEqual(masked_order_by(self.unmasked, IndividualDataSource, ["upload__jsonExt"]),
                         ["upload__jsonExt", "id"])

    def test_the_primary_key_is_not_added_twice_and_an_empty_order_stays_empty(self):
        self.assertEqual(masked_order_by(self.viewer, Individual, ["-id"]), ["-id"])
        self.assertEqual(masked_order_by(self.viewer, Individual, "lastName"), ["lastName", "id"])
        self.assertEqual(masked_order_by(self.viewer, Individual, None), None)
        self.assertEqual(masked_order_by(self.viewer, Individual, ["?"]), ["?", "id"])

    def test_a_mask_field_through_a_relation_masks_only_its_path(self):
        get_masking_class = MaskingClassStorage.get_masking_class
        mask = SimpleNamespace(masking_enabled=True, anon_fields=["group.code", "group.json_ext.income"])
        with mock.patch.object(MaskingClassStorage, "get_masking_class",
                               side_effect=lambda name: mask if name == "GroupIndividual" else get_masking_class(name)):
            for lookup in ("group__code", "group__code__icontains", "group__json_ext__income",
                           "group__json_ext__income__lt", "group__json_ext", "group__json_ext__icontains"):
                self.assertTrue(reads_masked_field(GroupIndividual, lookup), lookup)
            for lookup in ("group", "group__id", "group__json_ext__roof", "group__date_created", "role"):
                self.assertFalse(reads_masked_field(GroupIndividual, lookup), lookup)
            self.assertEqual(masked_order_by(self.viewer, GroupIndividual, ["group__jsonExt__roof", "-group__code"]),
                             ["group__jsonExt__roof", "id"])
