"""GroupService writes a group's memberships before the save that stores its version."""
from unittest import mock
from uuid import uuid4

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.test_helpers import LogInHelper
from individual.models import Group, GroupDataSource, GroupIndividual, IndividualDataSourceUpload
from individual.services import GroupIndividualService, GroupService
from individual.signals.on_validation_import_valid_items import BaseGroupColumnAggregationClass
from individual.tests.test_helpers import create_individual


class GroupUpdateOrderTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

    def setUp(self):
        self.service = GroupService(self.user)
        self.head = create_individual(self.user.username, {'first_name': 'Head'})
        self.son = create_individual(self.user.username, {'first_name': 'Son'})
        result = self.service.create({'code': f'ORDER-{uuid4().hex[:8]}', 'individuals_data': [
            {'individual_id': str(self.head.id), 'role': 'HEAD', 'recipient_type': 'PRIMARY'},
            {'individual_id': str(self.son.id), 'role': 'SON'},
        ]})
        self.assertTrue(result['success'], result)
        self.group = Group.objects.get(id=result['data']['id'])

    def stored_version(self):
        return Group.objects.filter(id=self.group.id).values_list('version', flat=True).get()

    def at_stored_version(self):
        """(history record of the stored version, {person id: membership history record} live at its date)."""
        record = Group.history.filter(id=self.group.id, version=self.stored_version()).latest('history_date')
        memberships = {}
        for membership in GroupIndividual.history.filter(
                group_id=self.group.id, history_date__lte=record.history_date).order_by('history_date'):
            memberships[str(membership.individual_id)] = membership
        live = {person_id: m for person_id, m in memberships.items() if not m.is_deleted}
        return record, live

    def listed(self, *persons, **fields):
        return [{'individual_id': str(person.id), **fields.get(person.first_name, {})} for person in persons]

    def update(self, individuals_data, **fields):
        return self.service.update({'id': str(self.group.id), **fields, 'individuals_data': individuals_data})

    def test_update_adding_a_member_without_role_stores_a_version_holding_it(self):
        daughter = create_individual(self.user.username, {'first_name': 'Daughter'})

        result = self.update(self.listed(self.head, self.son, daughter))

        self.assertTrue(result['success'], result)
        record, live = self.at_stored_version()
        self.assertIn(str(daughter.id), live)
        self.assertIn(str(daughter.id), record.json_ext['members'])
        membership = GroupIndividual.objects.get(group=self.group, individual=daughter)
        self.assertIsNone(membership.role)
        self.assertIsNone(membership.recipient_type)

    def test_update_removing_a_member_stores_a_version_without_it(self):
        result = self.update(self.listed(self.head))

        self.assertTrue(result['success'], result)
        record, live = self.at_stored_version()
        self.assertNotIn(str(self.son.id), live)
        self.assertNotIn(str(self.son.id), record.json_ext['members'])

    def test_update_with_only_membership_changes_stores_a_new_version(self):
        before = self.stored_version()
        daughter = create_individual(self.user.username, {'first_name': 'Daughter'})

        self.assertTrue(self.update(self.listed(self.head, self.son, daughter))['success'])

        self.assertGreater(self.stored_version(), before)

    def test_update_changing_no_field_and_no_membership_stores_no_version(self):
        before = self.stored_version()
        history = Group.history.filter(id=self.group.id).count()

        result = self.update(self.listed(self.head, self.son), code=self.group.code)

        self.assertTrue(result['success'], result)
        self.assertEqual(self.stored_version(), before)
        self.assertEqual(Group.history.filter(id=self.group.id).count(), history)

    def assert_nothing_changed(self, result, code, json_ext, memberships):
        self.assertFalse(result.get('success', True), result)
        self.assertEqual(result.get('message'), 'Failed to update Group')
        self.assertTrue(result.get('detail'), result)
        group = Group.objects.get(id=self.group.id)
        group.refresh_from_db()
        self.assertEqual(group.code, code)
        self.assertEqual(group.json_ext, json_ext)
        self.assertEqual(self.live_members(), memberships)

    def live_members(self):
        return set(GroupIndividual.objects.filter(group=self.group, is_deleted=False)
                   .values_list('individual_id', flat=True))

    def test_update_whose_membership_create_fails_changes_nothing(self):
        self.group.refresh_from_db()
        code, json_ext, memberships = self.group.code, self.group.json_ext, self.live_members()

        result = self.update(self.listed(self.head) + [{'individual_id': str(uuid4()), 'role': 'SON'}],
                             code='CHANGED')

        self.assert_nothing_changed(result, code, json_ext, memberships)

    def test_update_whose_membership_delete_fails_changes_nothing(self):
        self.group.refresh_from_db()
        code, json_ext, memberships = self.group.code, self.group.json_ext, self.live_members()
        failure = {'success': False, 'message': 'Failed to delete GroupIndividual', 'detail': 'refused'}

        with mock.patch.object(GroupIndividualService, 'delete', return_value=failure):
            result = self.update(self.listed(self.head), code='CHANGED')

        self.assert_nothing_changed(result, code, json_ext, memberships)

    def test_update_listing_a_former_member_does_not_add_it_again(self):
        self.assertTrue(self.update(self.listed(self.head))['success'])

        result = self.update(self.listed(self.head, self.son))

        self.assertTrue(result['success'], result)
        self.assertEqual(self.live_members(), {self.head.id})

    def test_update_of_a_household_with_a_former_member_not_listed_succeeds(self):
        self.assertTrue(self.update(self.listed(self.head))['success'])
        daughter = create_individual(self.user.username, {'first_name': 'Daughter'})

        result = self.update(self.listed(self.head, daughter))

        self.assertTrue(result['success'], result)
        self.assertEqual(self.live_members(), {self.head.id, daughter.id})

    def test_update_without_individuals_data_leaves_the_memberships(self):
        memberships = self.live_members()

        for individuals_data in (None, []):
            data = {'id': str(self.group.id), 'code': f'CODE-{uuid4().hex[:6]}'}
            if individuals_data is not None:
                data['individuals_data'] = individuals_data
            result = self.service.update(data)
            self.assertTrue(result['success'], result)
            self.assertEqual(self.live_members(), memberships)

    def test_update_result_carries_the_stored_version(self):
        daughter = create_individual(self.user.username, {'first_name': 'Daughter'})

        result = self.update(self.listed(self.head, self.son, daughter))

        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['version'], self.stored_version())

    def test_create_with_three_members_stores_a_version_holding_them(self):
        daughter = create_individual(self.user.username, {'first_name': 'Daughter'})
        result = self.service.create({'code': f'ORDER-{uuid4().hex[:8]}', 'individuals_data': [
            {'individual_id': str(self.head.id), 'role': 'HEAD', 'recipient_type': 'PRIMARY'},
            {'individual_id': str(self.son.id), 'role': 'SON'},
            {'individual_id': str(daughter.id), 'role': 'DAUGHTER'},
        ]})

        self.assertTrue(result['success'], result)
        self.group = Group.objects.get(id=result['data']['id'])
        self.assertEqual(result['data']['version'], self.stored_version())
        record, live = self.at_stored_version()
        persons = {str(self.head.id), str(self.son.id), str(daughter.id)}
        self.assertEqual(set(live), persons)
        self.assertEqual(set(record.json_ext['members']), persons)

    def test_update_with_only_membership_changes_locks_the_household_first(self):
        daughter = create_individual(self.user.username, {'first_name': 'Daughter'})

        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(self.update(self.listed(self.head, self.son, daughter))['success'])

        statements = [query['sql'] for query in queries.captured_queries]
        lock = next(i for i, sql in enumerate(statements)
                    if 'FOR UPDATE' in sql and '"individual_group"' in sql.split('FROM', 1)[-1].split('WHERE')[0])
        membership_write = next(i for i, sql in enumerate(statements)
                                if sql.startswith(('INSERT', 'UPDATE')) and 'individual_groupindividual' in sql)
        self.assertLess(lock, membership_write)


class GroupStepFailedRowTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

    def test_a_failed_update_is_reported_on_its_row(self):
        head = create_individual(self.user.username, {'first_name': 'Head'})
        result = GroupService(self.user).create({'code': f'STEP-{uuid4().hex[:8]}', 'individuals_data': [
            {'individual_id': str(head.id), 'role': 'HEAD'}]})
        self.assertTrue(result['success'], result)
        upload = IndividualDataSourceUpload(source_name='failed-row', source_type='test')
        upload.save(user=self.user)
        json_ext = {
            'id': result['data']['id'], 'code': result['data']['code'],
            'individuals_data': [{'individual_id': str(head.id)}, {'individual_id': str(uuid4())}],
        }
        source = GroupDataSource(upload=upload, json_ext=dict(json_ext))
        source.save(user=self.user)

        BaseGroupColumnAggregationClass.group_data_sources_into_entities(upload.id, self.user)

        source.refresh_from_db()
        self.assertIsNone(source.group_id)
        self.assertTrue(source.validations.get('group_step'), source.validations)
        self.assertEqual(source.json_ext, json_ext)
