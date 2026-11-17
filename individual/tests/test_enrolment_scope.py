import datetime

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.models import Group, GroupIndividual, Individual, IndividualDataSource
from individual.tests.test_data_masking import SEARCH_PERMS
from location.test_helpers import create_test_village

ENROLL = """
mutation ($input: %sInput!) { %s(input: $input) { internalId } }
"""


class EnrolmentScopeTest(openIMISGraphQLTestCase):
    """An enrolment selects only the persons and groups of the locations the user may see."""

    def setUp(self):
        super().setUp()
        from social_protection.tests.test_helpers import create_benefit_plan

        own = create_test_village({"name": "Scope own village", "code": "EsO"})
        other = create_test_village({"name": "Scope other village", "code": "EsX"})
        perms = SEARCH_PERMS + ["gql_group_create_perms"]
        self.user = create_right_only_user("enrol_scope_user", perms, [own.parent.parent.code])
        admin = create_right_only_user("enrol_scope_admin", perms, [own.parent.parent.code, other.parent.parent.code])
        self.persons = {}
        for name, village in (("Own", own), ("Other", other)):
            for kind in ("Single", "Head"):
                person = Individual(first_name=f"{name}{kind}", last_name="Scope", dob=datetime.date(1980, 1, 2),
                                    location=village, json_ext={"sex": "F"})
                person.save(user=admin)
                self.persons[f"{name}{kind}"] = person
            group = Group(code=f"ES-{name}", location=village, json_ext={"roof": "tin"})
            group.save(user=admin)
            GroupIndividual(group=group, individual=self.persons[f"{name}Head"],
                            role=GroupIndividual.Role.HEAD).save(user=admin)
        self.person_plan = create_benefit_plan(self.user.username, {"code": "ESC-I", "type": "INDIVIDUAL"})
        self.group_plan = create_benefit_plan(self.user.username, {"code": "ESC-G", "type": "GROUP"})

    def enroll(self, mutation, plan, condition):
        token = BaseTestContext(user=self.user).get_jwt()
        name = mutation[0].upper() + mutation[1:]
        content = self.send_mutation_raw(ENROLL % (name + "Mutation", mutation), token, follow=False, variables_param={
            "input": {"clientMutationId": f"enrol-scope-{mutation}", "customFilters": [condition],
                      "benefitPlanId": str(plan.id), "status": "ACTIVE"}})
        self.assertNotIn("errors", content)
        node = self._get_mutation_log_node(content["data"][mutation]["internalId"], token)
        self.assertEqual(node["status"], 2, node)

    def selected(self, plan):
        """The first names of the persons the enrolment into ``plan`` put up for review."""
        return set(IndividualDataSource.objects.filter(
            upload__source_name__startswith=f"Enrollment into {plan.code}",
        ).values_list("individual__first_name", flat=True))

    def test_a_person_enrolment_selects_the_persons_of_the_user_locations_only(self):
        self.enroll("confirmIndividualEnrollment", self.person_plan, "sex__iexact__string=F")
        self.assertEqual(self.selected(self.person_plan), {"OwnSingle"})

    def test_a_group_enrolment_selects_the_groups_of_the_user_locations_only(self):
        self.enroll("confirmGroupEnrollment", self.group_plan, "roof__iexact__string=tin")
        self.assertEqual(self.selected(self.group_plan), {"OwnHead"})
