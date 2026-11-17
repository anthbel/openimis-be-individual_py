import datetime
import json

from django.apps import apps

from core.models import ModuleConfiguration
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.apps import DEFAULT_CONFIG, IndividualConfig
from individual.models import Individual
from individual.tests.test_data_masking import MASKED, SEARCH_PERMS, RegisteredFieldsTestMixin
from location.test_helpers import create_test_village


class MaskConfigurationRefreshTest(RegisteredFieldsTestMixin, openIMISGraphQLTestCase):
    """A person mask configuration stored by another process applies from this process's next request."""

    def setUp(self):
        super().setUp()
        village = create_test_village({"name": "Refresh village", "code": "MkR"})
        district = village.parent.parent.code
        admin = create_right_only_user("mask_rf_admin", SEARCH_PERMS, [district])
        self.viewer = create_right_only_user("mask_rf_viewer", SEARCH_PERMS, [district])
        self.person = Individual(first_name="Ada", last_name="Lovelace", dob=datetime.date(1980, 1, 2),
                                 location=village, json_ext={"phone": "+257 1"})
        self.person.save(user=admin)
        self.token = BaseTestContext(user=self.viewer).get_jwt()
        self.register(person=lambda: [], group=lambda: [], membership=lambda: [])
        self.addCleanup(self.restore_configuration)

    def restore_configuration(self):
        ModuleConfiguration.objects.filter(module="individual", layer="be").delete()
        apps.get_app_config("individual").refresh_config()

    def store_without_reload(self, **cfg):
        """Stores the individual configuration as another process would: no reload in this one."""
        rows = ModuleConfiguration.objects.filter(module="individual", layer="be")
        config = json.dumps({**DEFAULT_CONFIG, **cfg})
        if rows.exists():
            rows.update(config=config)
        else:
            ModuleConfiguration.objects.bulk_create(
                [ModuleConfiguration(module="individual", layer="be", version="1", config=config)])

    def phone(self):
        response = self.query('{ individual(id: "%s") { edges { node { jsonExt } } } }' % self.person.id,
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.token}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        return json.loads(content["data"]["individual"]["edges"][0]["node"]["jsonExt"])["phone"]

    def test_a_stored_mask_field_applies_from_the_next_request(self):
        self.assertEqual(self.phone(), "+257 1")
        self.store_without_reload(individual_masking_enabled=True, individual_mask_fields=["json_ext.phone"])
        self.assertEqual(self.phone(), MASKED)
        self.store_without_reload(individual_masking_enabled=True, individual_mask_fields=[])
        self.assertEqual(self.phone(), "+257 1")

    def test_a_stored_individual_schema_applies_from_the_next_request(self):
        schema = json.dumps({"properties": {"phone": {"type": "string"}}})
        self.phone()
        self.store_without_reload(individual_schema=schema)
        self.phone()
        self.assertEqual(IndividualConfig.individual_schema, schema)
