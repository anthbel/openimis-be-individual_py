"""An export column is one of the model's own columns, one of its ``json_ext`` keys or a relation column the
frontends request; any other column is refused before any query. A household without a location places none of
its members in a location-scoped reader's rows: a person is visible to that reader through their own location or
through a located household in the reader's locations."""
import csv
import io
import json

from django.core.cache import caches
from django.test import override_settings
from graphql_relay import to_global_id

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user, create_test_interactive_user, create_test_technical_user
from individual.models import Group, GroupIndividual, Individual, IndividualDataSource, IndividualDataSourceUpload
from individual.schema import Query
from location.models import Location

PERMS = ["gql_individual_search_perms", "gql_group_search_perms"]
ALL = {"Ehunloc", "Ehxlocd", "Ehdel", "Ehpind", "Ehqind", "Ehkboth", "Ehzlocd"}


def clear_location_cache():
    caches["location"].clear()
    caches["default"].clear()


def make_location(code, loc_type, parent=None):
    return Location.objects.create(code=code, name=code, type=loc_type, parent=parent, validity_from="2019-06-01",
                                   audit_user_id=-1)


@override_settings(ROW_SECURITY=True)
class ExportAndHouseholdScopeTest(openIMISGraphQLTestCase):
    """Districts D and D2; the far reader is limited to D2, the near reader to D.

    G0 has no location: U (no location, head), X (located in D), H (no location, deleted). G1 is in D: P (head),
    K. G2 is in D2: Q (head), K. Z is located in D and has no household."""

    def setUp(self):
        super().setUp()
        clear_location_cache()
        self.addCleanup(clear_location_cache)
        region = make_location("EH-R", "R")
        village = make_location("EH-V", "V", make_location("EH-W", "W", make_location("EH-D", "D", region)))
        village2 = make_location("EH-V2", "V", make_location("EH-W2", "W", make_location("EH-D2", "D", region)))
        clear_location_cache()
        self.admin = create_test_interactive_user(username="eh_admin")
        self.far = create_right_only_user("eh_far", PERMS, ["EH-D2"])
        self.near = create_right_only_user("eh_near", PERMS, ["EH-D"])
        self.g0 = self.group("EH-G0", None)
        self.U = self.person("Ehunloc", None)
        self.X = self.person("Ehxlocd", village)
        self.H = self.person("Ehdel", None)
        self.mU = self.join(self.g0, self.U, GroupIndividual.Role.HEAD)
        self.mX = self.join(self.g0, self.X, GroupIndividual.Role.SON)
        self.join(self.g0, self.H, GroupIndividual.Role.NOT_RELATED)
        self.g1 = self.group("EH-G1", village)
        self.P = self.person("Ehpind", None)
        self.join(self.g1, self.P, GroupIndividual.Role.HEAD)
        self.g2 = self.group("EH-G2", village2)
        self.Q = self.person("Ehqind", None)
        self.join(self.g2, self.Q, GroupIndividual.Role.HEAD)
        self.K = self.person("Ehkboth", None)
        self.join(self.g2, self.K, GroupIndividual.Role.SON)
        self.join(self.g1, self.K, GroupIndividual.Role.SON)
        self.Z = self.person("Ehzlocd", village)
        Individual.objects.get(id=self.H.id).delete(user=self.admin)
        upload = IndividualDataSourceUpload(source_name="eh", source_type="csv")
        upload.save(user=self.admin)
        for person in (self.P, self.X, self.Z):
            IndividualDataSource(individual=Individual.objects.get(id=person.id), upload=upload).save(user=self.admin)
        clear_location_cache()

    def group(self, code, location):
        group = Group(code=code, location=location)
        group.save(user=self.admin)
        return group

    def person(self, last_name, location):
        person = Individual(first_name="Eh", last_name=last_name, dob="1990-01-01", location=location,
                            json_ext={"phone": f"tel-{last_name}"})
        person.save(user=self.admin)
        return person

    def join(self, group, person, role):
        membership = GroupIndividual(group=group, individual=person, role=role)
        membership.save(user=self.admin)
        return membership

    def gql(self, user, query):
        token = BaseTestContext(user=user).get_jwt()
        return json.loads(self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"}).content)

    def data(self, user, query):
        content = self.gql(user, query)
        self.assertNotIn("errors", content)
        return content["data"]

    def export(self, user, export, fields, arguments=""):
        """The response of ``export`` and the rows of the files it writes."""
        columns = {Query._adjust_notation(field): field for field in fields}
        before = set(ExportableQueryModel.objects.values_list("id", flat=True))
        content = self.gql(user, '{ %s(%sfields: %s, fieldsColumns: %s) }'
                           % (export, arguments, json.dumps(fields), json.dumps(json.dumps(columns))))
        rows = []
        for file in ExportableQueryModel.objects.exclude(id__in=before):
            file.content.open("rb")
            rows += list(csv.DictReader(io.StringIO(file.content.read().decode())))
        return content, rows

    def visible(self, user):
        return list(Individual.get_queryset(None, user).values_list("last_name", flat=True))

    @staticmethod
    def names(value):
        text = json.dumps(value)
        return {name for name in ALL if name in text}

    # Export columns.

    def relation_columns(self):
        g0, g1, g2 = (f'group_Id: "{group.id}", ' for group in (self.g0, self.g1, self.g2))
        return (
            ("individualExport", ["lastName", "groupindividuals.group.code"], ""),
            ("individualExport", ["lastName", "groupindividuals.group.groupindividuals.individual.lastName"], ""),
            ("individualExport", ["lastName", "location.individuals.lastName"], ""),
            ("individualExport", ["lastName", "location.code"], ""),
            ("groupExport", ["code", "groupindividuals.individual.lastName"], ""),
            ("groupExport", ["code", "groupindividuals.individual.groupindividuals.group.code"], ""),
            ("groupExport", ["code", "groupindividuals.individual.groupindividuals.group.groupindividuals."
                                     "individual.lastName"], ""),
            ("groupIndividualExport", ["individual.lastName", "group.groupindividuals.individual.lastName"], g0),
            ("groupIndividualExport", ["individual.lastName", "group.jsonExt"], g0),
            ("groupIndividualExport", ["individual.lastName", "individual.groupindividuals.group.code"], g2),
            ("groupIndividualExport", ["individual.lastName", "individual.groupindividuals.group.groupindividuals."
                                                              "individual.lastName"], g2),
            ("groupIndividualExport", ["individual.lastName", "individual.groupindividuals.group.groupindividuals."
                                                              "individual.location.code"], g2),
            ("groupIndividualExport", ["individual.lastName", "individual.location.individuals.lastName"], g1),
        )

    def test_an_export_column_following_another_relation_is_refused_naming_the_column(self):
        for export, fields, arguments in self.relation_columns():
            column = Query._adjust_notation(fields[1])
            for user in (self.far, self.near, self.admin):
                with self.subTest(export=export, column=column, user=user.username):
                    files = ExportableQueryModel.objects.count()
                    content, rows = self.export(user, export, fields, arguments)
                    self.assertEqual(rows, [])
                    self.assertEqual(ExportableQueryModel.objects.count(), files)
                    self.assertIn(f"the export column {column} is not exportable", json.dumps(content["errors"]))

    def test_the_far_reader_gets_no_person_of_another_district_through_an_export_column(self):
        for export, fields, arguments in self.relation_columns():
            with self.subTest(export=export, column=fields[1]):
                _content, rows = self.export(self.far, export, fields, arguments)
                self.assertFalse(self.names(rows) & {"Ehxlocd", "Ehpind", "Ehzlocd", "Ehdel"})

    def test_the_columns_the_frontends_request_still_export(self):
        _content, rows = self.export(self.near, "individualExport",
                                     ["id", "first_name", "last_name", "dob", "json_ext", "json_ext__phone"])
        self.assertEqual({row["last_name"] for row in rows}, {"Ehunloc", "Ehxlocd", "Ehpind", "Ehkboth", "Ehzlocd"})
        self.assertEqual({row["json_ext__phone"] for row in rows if row["last_name"] == "Ehzlocd"}, {"tel-Ehzlocd"})
        self.assertEqual({row["phone"] for row in rows if row["last_name"] == "Ehzlocd"}, {"tel-Ehzlocd"})
        _content, rows = self.export(self.near, "groupExport", ["id", "json_ext"])
        self.assertEqual({row["id"] for row in rows}, {str(self.g0.id), str(self.g1.id)})
        _content, rows = self.export(
            self.near, "groupIndividualExport",
            ["individual__id", "individual__first_name", "individual__last_name", "individual__dob", "role",
             "json_ext"], f'group_Id: "{self.g1.id}", ')
        self.assertEqual({(row["individual__last_name"], row["role"]) for row in rows},
                         {("Ehpind", "HEAD"), ("Ehkboth", "SON")})
        self.assertEqual({row["individual__id"] for row in rows}, {str(self.P.id), str(self.K.id)})

    def test_the_model_own_columns_export(self):
        _content, rows = self.export(self.near, "individualExport", ["lastName", "locationId", "location", "isDeleted"])
        self.assertEqual({(float(row["locationId"]), float(row["location"])) for row in rows
                          if row["lastName"] == "Ehzlocd"}, {(self.Z.location_id, self.Z.location_id)})

    # Household clause.

    def test_a_household_without_a_location_shows_a_member_of_another_district_to_no_reader(self):
        self.assertNotIn("Ehxlocd", self.visible(self.far))
        node = self.data(self.far, '{ node(id: "%s") { id } }' % to_global_id("IndividualGQLType", self.X.id))
        self.assertIsNone(node["node"])
        queries = {
            "individual": '{ individual { edges { node { lastName } } } }',
            "individualHistory": '{ individualHistory { edges { node { lastName } } } }',
            "individualDataSource": '{ individualDataSource { edges { node { individual { lastName } } } } }',
            "upload rows": '{ individualDataSourceUpload { edges { node { individualdatasourceSet { edges { node '
                           '{ individual { lastName } } } } } } } }',
            "groupIndividual": '{ groupIndividual(group_Id: "%s") { edges { node { individual { lastName } } } } }'
                               % self.g0.id,
            "groupIndividualHistory": '{ groupIndividualHistory { edges { node { individual { lastName } } } } }',
            "group members": '{ group(id: "%s") { edges { node { groupindividuals { edges { node '
                             '{ individual { lastName } } } } } } } }' % self.g0.id,
        }
        for case, query in queries.items():
            with self.subTest(case=case):
                self.assertNotIn("Ehxlocd", self.names(self.data(self.far, query)))
        node = self.data(self.far, '{ node(id: "%s") { id } }' % to_global_id("GroupIndividualGQLType", self.mX.id))
        self.assertIsNone(node["node"])
        _content, rows = self.export(self.far, "individualExport", ["last_name"])
        self.assertEqual({row["last_name"] for row in rows}, {"Ehunloc", "Ehqind", "Ehkboth"})
        _content, rows = self.export(self.far, "groupIndividualExport", ["individual__last_name"],
                                     f'group_Id: "{self.g0.id}", ')
        self.assertEqual({row["individual__last_name"] for row in rows}, {"Ehunloc"})

    def test_a_member_of_a_household_without_a_location_stays_visible_through_their_own_location(self):
        self.assertIn("Ehxlocd", self.visible(self.near))
        members = self.data(self.near, '{ groupIndividual(group_Id: "%s") { edges { node { individual '
                                       '{ lastName } } } } }' % self.g0.id)
        self.assertEqual(self.names(members), {"Ehunloc", "Ehxlocd"})

    def test_a_live_person_without_a_location_stays_visible(self):
        for user in (self.far, self.near):
            with self.subTest(user=user.username):
                self.assertIn("Ehunloc", self.visible(user))
                members = self.data(user, '{ groupIndividual(group_Id: "%s") { edges { node { individual '
                                          '{ lastName } } } } }' % self.g0.id)
                self.assertIn("Ehunloc", self.names(members))

    def test_the_rows_of_each_reader(self):
        self.assertEqual(sorted(self.visible(self.far)), ["Ehkboth", "Ehqind", "Ehunloc"])
        self.assertEqual(sorted(self.visible(self.near)), ["Ehkboth", "Ehpind", "Ehunloc", "Ehxlocd", "Ehzlocd"])

    def test_a_person_of_two_households_of_the_reader_is_listed_once(self):
        GroupIndividual(group=self.g0, individual=Individual.objects.get(id=self.K.id),
                        role=GroupIndividual.Role.OTHER_RELATIVE).save(user=self.admin)
        self.assertEqual(self.visible(self.near).count("Ehkboth"), 1)
        listed = self.data(self.near, '{ individual(lastName_Iexact: "Ehkboth") { totalCount edges { node { id } } } }')
        self.assertEqual(listed["individual"]["totalCount"], 1)
        self.assertEqual(len(listed["individual"]["edges"]), 1)

    def test_administrators_and_unscoped_users_are_unchanged(self):
        self.assertEqual(set(self.visible(self.admin)), ALL)
        technical = create_test_technical_user(username="eh_technical")
        self.assertEqual(sorted(self.visible(technical)),
                         ["Ehdel", "Ehkboth", "Ehkboth", "Ehpind", "Ehqind", "Ehunloc", "Ehxlocd"])
