import copy
import datetime

from django.core.exceptions import FieldDoesNotExist, PermissionDenied, ValidationError
from graphene.types.resolver import dict_or_attr_resolver

from core.apps import CoreConfig
from core.data_masking import DataMaskAbs, MaskingClassStorage
from individual.apps import IndividualConfig

MASK_PLACEHOLDER = DataMaskAbs().anonymize("")
_ABSENT = object()

# Group json_ext keys holding member names, written from the persons' names by
# GroupService.update_json_ext_for_group.
NAME_COLUMNS = ("first_name", "last_name")
GROUP_NAME_KEYS = ("head", "members", "primary_recipient", "secondary_recipient")
JSON_PREFIX = "json_ext."


class RegisteredFieldsMask(DataMaskAbs):
    """A mask of its own fields and of the fields other modules register with ``register_fields``.

    A source is a callable returning field paths (a column or ``json_ext.<key>``), called each time the
    mask is applied. The registered fields are masked whatever the module's own masking switch says.
    """

    _sources = {}

    @property
    def own_enabled(self):
        return True

    @classmethod
    def register_fields(cls, key, source):
        """Registers ``source`` under ``key``; None removes it. History masks share their live mask's sources."""
        if source is None:
            cls._sources.pop(key, None)
        else:
            cls._sources[key] = source

    def own_fields(self):
        return []

    def registered_fields(self):
        return [field for source in list(self._sources.values()) for field in (source() or [])]

    @property
    def anon_fields(self):
        own = list(self.own_fields() or []) if self.own_enabled else []
        return list(dict.fromkeys(own + self.registered_fields()))

    @property
    def masking_enabled(self):
        return bool(self.anon_fields)

    def anonymize(self, value):
        if isinstance(value, (datetime.date, datetime.datetime)):
            return super().anonymize("")
        if isinstance(value, list):
            return [self.anonymize(item) for item in value]
        return super().anonymize(value)


class IndividualMask(RegisteredFieldsMask):
    masking_model = 'Individual'
    _sources = {}

    @property
    def own_enabled(self):
        return bool(IndividualConfig.individual_masking_enabled)

    def own_fields(self):
        return IndividualConfig.individual_mask_fields


class IndividualHistoryMask(IndividualMask):
    masking_model = 'HistoricalIndividual'


def head_copy_fields(person_fields):
    """The group json_ext paths holding a copy of the masked person fields ``person_fields``: the head's
    json_ext keys copied onto the group, and the member names when a name column is masked."""
    not_copied = set(IndividualConfig.group_json_ext_keys_not_copied_from_head or ())
    paths = [field for field in person_fields
             if field.startswith(JSON_PREFIX) and field[len(JSON_PREFIX):].split(".")[0] not in not_copied]
    if any(field in NAME_COLUMNS for field in person_fields):
        paths += [JSON_PREFIX + key for key in GROUP_NAME_KEYS]
    return paths


class GroupMask(RegisteredFieldsMask):
    """The registered group fields, and the group's copy of the fields the person mask masks."""

    masking_model = 'Group'
    _sources = {}

    def own_fields(self):
        return head_copy_fields(IndividualMask().anon_fields)


class GroupHistoryMask(GroupMask):
    masking_model = 'HistoricalGroup'


class GroupIndividualMask(RegisteredFieldsMask):
    """The membership fields other modules register. A masked choice column (role, recipient_type) becomes null,
    the one value its GraphQL enum carries besides the choices."""

    masking_model = 'GroupIndividual'
    _sources = {}

    def apply_mask(self, data):
        for field in self.anon_fields:
            parts = field.split(".")
            if len(parts) == 1 and _is_choice_column(data, parts[0]):
                setattr(data, parts[0], None)
            else:
                self._anonymize_nested_field(data, parts)
        return data


class GroupIndividualHistoryMask(GroupIndividualMask):
    masking_model = 'HistoricalGroupIndividual'


def _is_choice_column(obj, name):
    meta = getattr(type(obj), "_meta", None)
    if meta is None:
        return False
    try:
        return bool(meta.get_field(name).choices)
    except FieldDoesNotExist:
        return False


class UploadRowMask(DataMaskAbs):
    """An upload row (a data source's json_ext, holding its unit's columns and json_ext keys at its top level)
    masked by the mask of its unit, with the note of a validation error on a masked key."""

    unit_mask = None

    @property
    def anon_fields(self):
        return [JSON_PREFIX + (field[len(JSON_PREFIX):] if field.startswith(JSON_PREFIX) else field)
                for field in self.unit_mask().anon_fields]

    @property
    def masking_enabled(self):
        return bool(self.anon_fields)

    def anonymize(self, value):
        return self.unit_mask().anonymize(value)

    def apply_mask(self, data):
        fields = self.anon_fields
        for field in fields:
            self._anonymize_nested_field(data, field.split("."))
        masked_keys = {field[len(JSON_PREFIX):].split(".")[0] for field in fields}
        validations = getattr(data, "validations", None)
        errors = validations.get("validation_errors") if isinstance(validations, dict) else None
        for error in errors if isinstance(errors, list) else ():
            if isinstance(error, dict) and error.get("field_name") in masked_keys:
                error["note"] = self.anonymize(error.get("note"))
        return data


class IndividualUploadRowMask(UploadRowMask):
    masking_model = 'IndividualDataSource'
    unit_mask = IndividualMask


class GroupUploadRowMask(UploadRowMask):
    masking_model = 'GroupDataSource'
    unit_mask = GroupMask


def may_see_masked(user):
    return user.has_perms(CoreConfig.gql_query_enable_viewing_masked_data_perms)


def mask_for_user(user, masking_model, obj):
    """Masks ``obj`` in place by the mask registered for ``masking_model`` unless ``user`` may see masked data."""
    if not may_see_masked(user):
        mask = MaskingClassStorage.get_masking_class(masking_model)
        if mask is not None and mask.masking_enabled:
            mask.apply_mask(obj)
    return obj


def context_may_see_masked(info):
    """Whether the user of the GraphQL request may see masked data, checked once per request."""
    context = info.context
    if not hasattr(context, "_individual_may_see_masked"):
        context._individual_may_see_masked = may_see_masked(context.user)
    return context._individual_may_see_masked


def mask_once(info, obj, masking_model):
    """Masks ``obj`` in place by the mask registered for ``masking_model``, once per object, unless the user
    of the request may see masked data. The object counts as checked only once the mask is applied, so a mask
    that fails raises on every field of the object."""
    if getattr(obj, "_data_mask_checked", False):
        return obj
    if not context_may_see_masked(info):
        mask = MaskingClassStorage.get_masking_class(masking_model)
        if mask is not None and mask.masking_enabled:
            mask.apply_mask(obj)
    obj._data_mask_checked = True
    return obj


def masking_resolver(masking_model):
    """A default field resolver masking the object first, so the type is masked on every path reaching it."""

    def resolve(attname, default_value, root, info, **args):
        mask_once(info, root, masking_model)
        return dict_or_attr_resolver(attname, default_value, root, info, **args)

    return resolve


def reads_masked_field(model, lookup):
    """Whether the ``__`` lookup ``lookup`` from ``model`` reads a field the mask registered for a model it
    reaches masks; a whole ``json_ext`` is masked while any of its paths is."""
    parts = lookup.split("__")
    for index, part in enumerate(parts):
        mask = MaskingClassStorage.get_masking_class(model.__name__)
        fields = mask.anon_fields if mask is not None and mask.masking_enabled else []
        if part == "json_ext" and fields:
            path = ".".join(parts[index + 1:])
            masked = [field[len(JSON_PREFIX):] for field in fields if field.startswith(JSON_PREFIX)]
            if not path:
                return bool(masked)
            return any(path == key or path.startswith(f"{key}.") or key.startswith(f"{path}.") for key in masked)
        if part in {field.split(".")[0] for field in fields if not field.startswith(JSON_PREFIX)}:
            return True
        try:
            field = model._meta.get_field(part)
        except FieldDoesNotExist:
            return False
        if not field.is_relation:
            return False
        model = field.related_model
    return False


def check_export_columns(user, model, lookups):
    """Refuses an export column reading a masked field to a user without the masked-data right: an export
    reads the stored values, which no mask reaches."""
    if may_see_masked(user):
        return
    for lookup in lookups:
        if reads_masked_field(model, lookup):
            raise PermissionDenied(f"the column {lookup} needs the right to view masked data")


def _value_at(value, path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return _ABSENT
        value = value[key]
    return value


def _set_at(value, path, item):
    for key in path[:-1]:
        value = value.setdefault(key, {})
    value[path[-1]] = item


def _equal(left, right):
    """``left == right``, False for values whose types cannot be compared (core's dates raise on a string)."""
    try:
        return bool(left == right)
    except TypeError:
        return False


def check_masked_values_sent_back(user, masking_model, instance, obj_data):
    """Refuses an update of ``instance`` by a user without the masked-data right that gives a masked field the
    value the mask shows for it, naming the field. A date column given the mask, and a masked json_ext key that
    the update's json_ext leaves out, keep their stored value (``obj_data`` is adjusted in place)."""
    if instance is None or may_see_masked(user):
        return
    mask = MaskingClassStorage.get_masking_class(masking_model)
    if mask is None or not mask.masking_enabled:
        return
    refused = []
    for field in mask.anon_fields:
        name, *path = field.split(".")
        if name not in obj_data:
            continue
        stored = _value_at(getattr(instance, name, None), path)
        if stored is _ABSENT:
            continue
        incoming = _value_at(obj_data[name], path)
        if incoming is _ABSENT:
            if path and isinstance(obj_data[name], dict):
                _set_at(obj_data[name], path, copy.deepcopy(stored))
            continue
        if not _equal(incoming, mask.anonymize(copy.deepcopy(stored))) or _equal(incoming, stored):
            continue
        if not path and isinstance(stored, (datetime.date, datetime.datetime)):
            del obj_data[name]
            continue
        refused.append(field)
    if refused:
        raise ValidationError(
            f"masked value sent back for {', '.join(refused)}: send a new value or leave the field out")
