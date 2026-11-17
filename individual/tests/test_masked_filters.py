import datetime
import json

from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.models import Group, GroupIndividual, Individual, IndividualDataSource, IndividualDataSourceUpload
from individual.tests.test_data_masking import SEARCH_PERMS, RegisteredFieldsTestMixin
from location.test_helpers import create_test_village


class MaskedFiltersTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """A filter, an ordering or a custom filter reading a masked field is refused to a user without the
    masked-data right, on every connection and summary of the person, group and membership types."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Filter village", "code": "MkF"})
        district = village.parent.parent.code
        self.village, self.district = village, district
        admin = create_right_only_user("mask_flt_admin", SEARCH_PERMS, [district])
        self.viewer = create_right_only_user("mask_flt_viewer", SEARCH_PERMS, [district])
        self.unmasked = create_right_only_user(
            "mask_flt_unmasked", SEARCH_PERMS + ["gql_query_enable_viewing_masked_data_perms"], [district])
        self.group = Group(code="MK-F1", location=village, json_ext={"income": 120, "roof": "tin"})
        self.group.save(user=admin)
        self.person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                                 location=village, json_ext={"phone": "+257 1", "sex": "F"})
        self.person.save(user=admin)
        GroupIndividual(group=self.group, individual=self.person, role=GroupIndividual.Role.HEAD).save(user=admin)
        Individual(first_name="Zed", last_name="Byron", dob=datetime.date(1970, 5, 6), location=village,
                   json_ext={"phone": "+257 0", "sex": "M"}).save(user=admin)
        self.upload = IndividualDataSourceUpload(source_name="filters.csv", source_type="individual import")
        self.upload.save(user=admin)
        IndividualDataSource(upload=self.upload, individual=self.person,
                             json_ext={"first_name": "Ada", "phone": "+257 1"}).save(user=admin)
        self.tokens = {user.username: BaseTestContext(user=user).get_jwt() for user in (self.viewer, self.unmasked)}
        self.register()

    def gql(self, user, query):
        response = self.query(query % {"group": self.group.id, "person": self.person.id, "upload": self.upload.id},
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.username]}"})
        return json.loads(response.content)

    MASKED_FILTERS = {
        "first name": '{ individual(firstName_Icontains: "Ad") { totalCount } }',
        "dob": '{ individual(dob_Lt: "1990-01-01") { totalCount } }',
        "custom filter": '{ individual(customFilters: ["phone__icontains__string=+257"]) { totalCount } }',
        "history": '{ individualHistory(firstName_Iexact: "Ada") { totalCount } }',
        "group member name": '{ group(firstName: "Ada") { totalCount } }',
        "group custom filter": '{ group(customFilters: ["phone__icontains__string=+257"]) { totalCount } }',
        "group code": '{ group(code_Icontains: "MK") { totalCount } }',
        "group head in history": '{ groupHistory(jsonExtHead_Icontains: "Ada") { totalCount } }',
        "membership person": '{ groupIndividual(group_Id: "%(group)s", individual_FirstName_Icontains: "Ad") '
                             '{ totalCount } }',
        "membership role": '{ groupIndividual(group_Id: "%(group)s", role: HEAD) { totalCount } }',
        "upload row person": '{ individualDataSource(upload_Id: "%(upload)s", individual_FirstName_Icontains: "Ad") '
                             '{ totalCount } }',
        "nested memberships of a person": '{ individual(id: "%(person)s") { edges { node { '
                                          'groupindividuals(role: HEAD) { totalCount } } } } }',
        "nested memberships of a group": '{ group(id: "%(group)s") { edges { node { '
                                         'groupindividuals(individual_Dob_Lt: "1990-01-01") { totalCount } } } } }',
        "upload rows nested in an upload": '{ individualDataSourceUpload(id: "%(upload)s") { edges { node { '
                                           'individualdatasourceSet(individual_FirstName_Iexact: "Ada") '
                                           '{ totalCount } } } } }',
        "nested upload rows": '{ individual(id: "%(person)s") { edges { node { '
                              'individualdatasourceSet(individual_FirstName_Iexact: "Ada") { totalCount } } } } }',
        "enrollment summary": '{ individualEnrollmentSummary(customFilters: ["phone__icontains__string=+257"]) '
                              '{ numberOfSelectedIndividuals } }',
        "group enrollment summary": '{ groupEnrollmentSummary(customFilters: ["phone__icontains__string=+257"]) '
                                    '{ numberOfSelectedGroups } }',
        "export filter": '{ individualExport(firstName_Icontains: "Ad", fields: ["id"], fieldsColumns: "{}") }',
    }

    def test_an_export_custom_filter_reading_a_masked_field_is_refused_without_the_masked_data_right(self):
        content = self.gql(self.viewer, '{ individualExport(customFilters: ["phone__icontains__string=+257"], '
                                        'fields: ["id"], fieldsColumns: "{}") }')
        self.assertIn("masked data", json.dumps(content["errors"]))

    def test_a_filter_reading_a_masked_field_is_refused_without_the_masked_data_right(self):
        for case, query in self.MASKED_FILTERS.items():
            with self.subTest(case=case):
                content = self.gql(self.viewer, query)
                self.assertIn("errors", content)
                self.assertIn("masked data", json.dumps(content["errors"]))

    def test_a_filter_reading_a_masked_field_works_with_the_masked_data_right(self):
        for case, query in self.MASKED_FILTERS.items():
            with self.subTest(case=case):
                content = self.gql(self.unmasked, query)
                self.assertNotIn("errors", content)
        content = self.gql(self.unmasked, self.MASKED_FILTERS["first name"])
        self.assertEqual(content["data"]["individual"]["totalCount"], 1)

    def test_a_filter_on_an_unmasked_field_is_applied(self):
        for query, name in (('{ individual(lastName_Icontains: "Love") { totalCount } }', "individual"),
                            ('{ individual(customFilters: ["sex__iexact__string=F"]) { totalCount } }', "individual"),
                            ('{ group(lastName: "Love") { totalCount } }', "group")):
            with self.subTest(query=query):
                content = self.gql(self.viewer, query)
                self.assertNotIn("errors", content)
                self.assertEqual(content["data"][name]["totalCount"], 1)

    def ordered_first_names(self, user, order):
        content = self.gql(user, '{ individual(orderBy: ["%s"]) { edges { node { lastName } } } }' % order)
        self.assertNotIn("errors", content)
        return [edge["node"]["lastName"] for edge in content["data"]["individual"]["edges"]]

    def test_an_ordering_on_a_masked_field_is_left_out_without_the_masked_data_right(self):
        for ascending, descending in (("firstName", "-firstName"), ("dob", "-dob"),
                                      ("json_ext__phone", "-json_ext__phone")):
            with self.subTest(order=ascending):
                self.assertEqual(self.ordered_first_names(self.viewer, ascending),
                                 self.ordered_first_names(self.viewer, descending))
                self.assertEqual(self.ordered_first_names(self.unmasked, ascending),
                                 list(reversed(self.ordered_first_names(self.unmasked, descending))))
        self.assertEqual(self.ordered_first_names(self.viewer, "lastName"), ["Byron", "Lovelace"])
        self.assertEqual(self.ordered_first_names(self.viewer, "-lastName"), ["Lovelace", "Byron"])

    def test_a_list_ordered_on_a_masked_field_pages_through_every_row_once(self):
        admin = create_right_only_user("mask_flt_pager", SEARCH_PERMS, [self.district])
        for index in range(7):
            Individual(first_name="Same", last_name=f"Page{index}", dob=datetime.date(1990, 1, 1),
                       location=self.village).save(user=admin)
        expected = set(Individual.objects.filter(is_deleted=False).values_list("id", flat=True))
        query = '{ individual(orderBy: ["firstName"], first: 2%s) { totalCount pageInfo { hasNextPage endCursor } ' \
                'edges { node { id uuid } } } }'
        seen, after = [], ""
        with CaptureQueriesContext(connection) as queries:
            while True:
                content = self.gql(self.viewer, query % after)
                self.assertNotIn("errors", content)
                page = content["data"]["individual"]
                seen += [node["node"]["uuid"] for node in page["edges"]]
                if not page["pageInfo"]["hasNextPage"]:
                    break
                after = ', after: "%s"' % page["pageInfo"]["endCursor"]
        self.assertEqual(sorted(seen), sorted(str(uuid) for uuid in expected))
        self.assertEqual(len(seen), len(set(seen)))
        paged = [query["sql"] for query in queries.captured_queries
                 if 'FROM "individual_individual"' in query["sql"] and "LIMIT" in query["sql"]]
        self.assertTrue(paged)
        for sql in paged:
            self.assertIn('ORDER BY "individual_individual"."UUID" ASC LIMIT', sql)

    def test_an_ordering_by_unmasked_fields_ends_with_the_primary_key(self):
        with CaptureQueriesContext(connection) as queries:
            content = self.gql(self.viewer, '{ individual(orderBy: ["lastName"], first: 1) { edges { node { id } } } }')
        self.assertNotIn("errors", content)
        self.assertTrue(any('ORDER BY "individual_individual"."last_name" ASC, "individual_individual"."UUID" ASC'
                            in query["sql"] for query in queries.captured_queries))


ENROLL = """
mutation ($input: %sInput!) { %s(input: $input) { internalId } }
"""


class MaskedEnrollmentFilterTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """An enrolment selecting persons or groups by a custom filter reading a masked field is refused to a user
    without the masked-data right and enrols no one."""

    def setUp(self):
        super().setUp()
        from social_protection.tests.test_helpers import create_benefit_plan

        village = create_test_village({"name": "Enrolment village", "code": "MkEn"})
        district = village.parent.parent.code
        perms = SEARCH_PERMS + ["gql_group_create_perms"]
        self.viewer = create_right_only_user("mask_en_viewer", perms, [district])
        self.unmasked = create_right_only_user(
            "mask_en_unmasked", perms + ["gql_query_enable_viewing_masked_data_perms"], [district])
        for first_name, phone, income in (("Ada", "+257 1", 120), ("Zed", "+257 9", 80)):
            Individual(first_name=first_name, last_name="X", dob=datetime.date(1980, 1, 2), location=village,
                       json_ext={"phone": phone}).save(user=self.viewer)
            Group(code=f"MK-EN-{first_name}", location=village, json_ext={"income": income}).save(user=self.viewer)
        self.person_plan = create_benefit_plan(self.viewer.username, {"code": "MKEN-I", "type": "INDIVIDUAL"})
        self.group_plan = create_benefit_plan(self.viewer.username, {"code": "MKEN-G", "type": "GROUP"})
        self.register()

    def enroll(self, user, mutation, plan, condition):
        token = BaseTestContext(user=user).get_jwt()
        name = mutation[0].upper() + mutation[1:]
        content = self.send_mutation_raw(ENROLL % (name + "Mutation", mutation), token, follow=False, variables_param={
            "input": {"clientMutationId": f"mask-en-{mutation}-{user.username}", "customFilters": [condition],
                      "benefitPlanId": str(plan.id), "status": "ACTIVE"}})
        self.assertNotIn("errors", content)
        return self._get_mutation_log_node(content["data"][mutation]["internalId"], token)

    def test_an_enrolment_by_a_masked_field_is_refused_without_the_masked_data_right(self):
        from social_protection.models import Beneficiary, GroupBeneficiary

        for mutation, plan, condition, beneficiaries in (
                ("confirmIndividualEnrollment", self.person_plan, "phone__icontains__string=+257 1", Beneficiary),
                ("confirmGroupEnrollment", self.group_plan, "income__exact__integer=120", GroupBeneficiary)):
            with self.subTest(mutation=mutation):
                node = self.enroll(self.viewer, mutation, plan, condition)
                self.assertNotEqual(node["status"], 2, node)
                self.assertIn("masked data", node["error"])
                self.assertFalse(beneficiaries.objects.filter(benefit_plan=plan).exists())
                node = self.enroll(self.unmasked, mutation, plan, condition)
                self.assertEqual(node["status"], 2, node)
