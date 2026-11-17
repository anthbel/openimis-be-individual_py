import copy
import datetime
import logging
import math
from decimal import Decimal

from django.core.exceptions import FieldDoesNotExist, PermissionDenied, ValidationError
from django.db.models import JSONField
from django.db.models.fields.json import KeyTransform
from graphene.types.resolver import dict_or_attr_resolver

from core.apps import CoreConfig
from core.data_masking import DataMaskAbs, MaskingClassStorage
from individual.apps import IndividualConfig

logger = logging.getLogger(__name__)

MASK_PLACEHOLDER = DataMaskAbs().anonymize("")
_ABSENT = object()

# Group json_ext keys holding member names, written from the persons' names by
# GroupService.update_json_ext_for_group.
NAME_COLUMNS = ("first_name", "last_name")
GROUP_NAME_KEYS = ("head", "members", "primary_recipient", "secondary_recipient")
JSON_PREFIX = "json_ext."
MEMBERSHIP_COLUMNS = ("role", "recipient_type")
# json_ext keys holding a copy of a column: the location name and code of an upload row, which the upload
# workflows write into the unit's json_ext, and the location string a person update writes.
COLUMN_COPY_KEYS = {
    column: ("location_name", "location_code", "location_str") for column in ("location", "location_id")}
# Upload row keys holding a membership column: a person row's, and those of each entry of a group row's
# ``individuals_data``.
PERSON_ROW_MEMBERSHIP_KEYS = {"role": "individual_role", "recipient_type": "recipient_info"}
GROUP_ROW_MEMBERS_KEY = "individuals_data"


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
        fields = list(dict.fromkeys(own + self.registered_fields()))
        return list(dict.fromkeys(fields + column_copy_paths(fields)))

    @property
    def masking_enabled(self):
        return bool(self.anon_fields)

    def anonymize(self, value):
        """The string mask for a string, date, number or boolean, at any depth of lists and objects; a number or
        boolean shown as the mask cannot be taken for a stored value."""
        if isinstance(value, (datetime.date, datetime.datetime, bool, int, float, Decimal)):
            return MASK_PLACEHOLDER
        if isinstance(value, list):
            return [self.anonymize(item) for item in value]
        return super().anonymize(value)

    def _anonymize_nested_field(self, obj, field_parts):
        """A masked foreign-key column, by name or column name (``location``, ``location_id``), becomes null:
        neither the related object nor its key is shown."""
        if len(field_parts) == 1:
            foreign_key = _foreign_key(obj, field_parts[0])
            if foreign_key is not None:
                setattr(obj, foreign_key.name, None)
                return obj
        return super()._anonymize_nested_field(obj, field_parts)


class IndividualMask(RegisteredFieldsMask):
    """The person mask. A masked column masks the ``json_ext`` key of the same name too: the upload workflows
    write the whole upload row, the person's columns included, into the person's ``json_ext``."""

    masking_model = 'Individual'
    _sources = {}

    @property
    def anon_fields(self):
        fields = super().anon_fields
        return list(dict.fromkeys(fields + [JSON_PREFIX + field for field in fields if "." not in field]))

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


def column_copy_paths(fields):
    """The json_ext paths holding a copy of the columns among ``fields`` (``COLUMN_COPY_KEYS``)."""
    return [JSON_PREFIX + key for field in fields for key in COLUMN_COPY_KEYS.get(field, ())]


def _foreign_key(obj, name):
    """The many-to-one or one-to-one field of ``obj``'s model named ``name`` or with the column name ``name``,
    or None."""
    return _model_foreign_key(type(obj), name)


def foreign_key_columns(model, fields):
    """The name and column name (``location``, ``location_id``) of each foreign key of ``model`` that ``fields``
    name by either."""
    columns = set()
    for field in fields:
        foreign_key = _model_foreign_key(model, field)
        if foreign_key is not None:
            columns |= {foreign_key.name, foreign_key.attname}
    return columns


def _masked_columns(model, fields):
    """The columns of ``model`` that ``fields`` mask, a foreign key under its name and its column name."""
    return {field.split(".")[0] for field in fields if not field.startswith(JSON_PREFIX)} | \
        foreign_key_columns(model, fields)


def _model_foreign_key(model, name):
    meta = getattr(model, "_meta", None)
    if meta is None:
        return None
    try:
        field = meta.get_field(name)
    except FieldDoesNotExist:
        return None
    return field if field.concrete and (field.many_to_one or field.one_to_one) else None


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
    masked by the mask of its unit and, for the row keys holding a membership column, by the membership mask,
    with the note of a validation error on a masked key. A path reaching a list masks each of its objects."""

    unit_mask = None

    @property
    def anon_fields(self):
        unit_fields = [JSON_PREFIX + (field[len(JSON_PREFIX):] if field.startswith(JSON_PREFIX) else field)
                       for field in self.unit_mask().anon_fields]
        membership = [field for field in GroupIndividualMask().anon_fields if field in MEMBERSHIP_COLUMNS]
        return list(dict.fromkeys(unit_fields + [JSON_PREFIX + key for key in self.membership_keys(membership)]))

    def membership_keys(self, columns):
        """The row paths holding the masked membership ``columns``."""
        return []

    def _anonymize_dict_field(self, dictionary, field_parts):
        if len(field_parts) > 1 and isinstance(dictionary.get(field_parts[0]), list):
            for item in dictionary[field_parts[0]]:
                if isinstance(item, dict):
                    self._anonymize_dict_field(item, field_parts[1:])
            return
        super()._anonymize_dict_field(dictionary, field_parts)

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

    def membership_keys(self, columns):
        return [PERSON_ROW_MEMBERSHIP_KEYS[column] for column in columns]


class GroupUploadRowMask(UploadRowMask):
    masking_model = 'GroupDataSource'
    unit_mask = GroupMask

    def membership_keys(self, columns):
        return [f"{GROUP_ROW_MEMBERS_KEY}.{column}" for column in columns]


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


def _json_path(parts):
    """The json_ext path the lookup parts after ``json_ext`` read; a trailing lookup name (icontains, lt, ...)
    is not a key."""
    if parts and (JSONField.get_lookups().get(parts[-1]) or KeyTransform.get_lookups().get(parts[-1])):
        parts = parts[:-1]
    return ".".join(parts)


def _reads_relation_path(parts, path):
    """Whether the lookup ``parts`` read the dotted mask field ``path`` that reaches a related model's field
    (``group.code``, ``group.json_ext.income``): they reach it or a part of it, or a ``json_ext`` holding it."""
    path = path.split(".")
    if "json_ext" in parts:
        start = parts.index("json_ext") + 1
        parts = [*parts[:start], *filter(None, _json_path(parts[start:]).split("."))]
    if parts[:len(path)] == path:
        return True
    return path[:len(parts)] == parts and "json_ext" in parts


def reads_masked_field(model, lookup):
    """Whether the ``__`` lookup ``lookup`` from ``model`` reads a field the mask registered for a model it
    reaches masks; a whole ``json_ext`` is masked while any of its paths is. A mask field is a column, a
    ``json_ext.<key>`` path, or a dotted path through a relation (``group.code``)."""
    parts = lookup.split("__")
    for index, part in enumerate(parts):
        mask = MaskingClassStorage.get_masking_class(model.__name__)
        fields = mask.anon_fields if mask is not None and mask.masking_enabled else []
        relation_paths = [field for field in fields if "." in field and not field.startswith(JSON_PREFIX)]
        if any(_reads_relation_path(parts[index:], path) for path in relation_paths):
            return True
        if part == "json_ext" and fields:
            path = _json_path(parts[index + 1:])
            masked = [field[len(JSON_PREFIX):] for field in fields if field.startswith(JSON_PREFIX)]
            if not path:
                return bool(masked)
            return any(path == key or path.startswith(f"{key}.") or key.startswith(f"{path}.") for key in masked)
        if part in {field for field in fields if "." not in field} | foreign_key_columns(model, fields):
            return True
        try:
            field = model._meta.get_field(part)
        except FieldDoesNotExist:
            return False
        if not field.is_relation:
            return False
        model = field.related_model
    return False


def check_filter_lookups(user, model, lookups):
    """Refuses to a user without the masked-data right a filter or an ordering on ``model`` by a lookup reading
    a masked field, naming the lookup: its results would tell the masked values."""
    if may_see_masked(user):
        return
    for lookup in lookups:
        if lookup and reads_masked_field(model, lookup):
            raise PermissionDenied(f"filtering or ordering on {lookup} needs the right to view masked data")


_EMPTY_FILTER_VALUES = ([], (), {}, "", None)


def filter_lookups(filterset_class, args):
    """The model lookups of the filter arguments set in ``args``, as ``filterset_class`` applies them."""
    filters = filterset_class.base_filters
    return [filters[name].field_name for name, value in args.items()
            if name in filters and value not in _EMPTY_FILTER_VALUES]


def order_lookups(order_by):
    """The model lookups of an ``orderBy`` argument, as core's ordered connection applies it."""
    from graphene.utils.str_converters import to_snake_case
    from core.schema import OrderedDjangoFilterConnectionField

    if not order_by:
        return []
    lookups = []
    for order in [order_by] if isinstance(order_by, str) else order_by:
        if order == "?":
            continue
        cleaned = to_snake_case(OrderedDjangoFilterConnectionField._filter_order_by(order) or "")
        lookups += [part.lstrip("-+") for part in cleaned.split(",")]
    return lookups


def unmasked_order_by(user, model, order_by):
    """``order_by`` less its terms ordering by a masked field, for a user without the masked-data right: such an
    ordering would tell the masked values, and leaving it out keeps the list readable. The model's primary key
    orders last, so the order stays total and pages neither repeat nor skip rows."""
    if not order_by:
        return order_by
    terms = [order_by] if isinstance(order_by, str) else list(order_by)
    kept = terms if may_see_masked(user) else [
        term for term in terms if not any(reads_masked_field(model, lookup) for lookup in order_lookups([term]))]
    if len(kept) < len(terms):
        logger.info("orderBy %s of %s left out for a user without the masked-data right",
                    [term for term in terms if term not in kept], model.__name__)
    primary_key = model._meta.pk.name
    if primary_key not in order_lookups(kept):
        kept = [*kept, primary_key]
    return kept


def _crosses_undescribed_json(model, lookup):
    """Whether ``lookup`` from ``model`` traverses, through its relations, a JSON field no mask describes (any
    JSON field but the ``json_ext`` of a model with a registered mask), or a name that is no field."""
    for part in lookup.split("__"):
        if model is None:
            return True
        try:
            field = model._meta.get_field(part)
        except FieldDoesNotExist:
            return True
        if isinstance(field, JSONField):
            return part != "json_ext" or MaskingClassStorage.get_masking_class(model.__name__) is None
        if not field.is_relation:
            return False
        model = field.related_model
    return False


def reveals_masked_values(model, lookup, aliases=None):
    """Whether ordering ``model`` by ``lookup`` would tell masked values: its path reads a masked field, a
    masked ``json_ext`` key of a masked model included (``reads_masked_field``), or traverses a JSON field no
    mask describes, which may hold copies of masked values, or a name that is no field, through any relation.
    ``aliases`` maps an annotation of the queryset to the lookup, or the tuple of lookups, it is computed from;
    an empty tuple reads no stored field."""
    alias = (aliases or {}).get(lookup, lookup)
    lookups = (alias,) if isinstance(alias, str) else tuple(alias)
    return any(reads_masked_field(model, path) or _crosses_undescribed_json(model, path) for path in lookups)


def masked_order_by(user, model, order_by, aliases=None):
    """``order_by`` less, for a user without the masked-data right, its terms ``reveals_masked_values`` holds
    for; each left-out term is logged at info level. The model's primary key orders last, so the order stays
    total and pages neither repeat nor skip rows."""
    if not order_by:
        return order_by
    terms = [order_by] if isinstance(order_by, str) else list(order_by)
    kept = terms if may_see_masked(user) else [
        term for term in terms
        if not any(reveals_masked_values(model, lookup, aliases) for lookup in order_lookups([term]))]
    if len(kept) < len(terms):
        logger.info("orderBy %s of %s left out for a user without the masked-data right",
                    [term for term in terms if term not in kept], model.__name__)
    primary_key = model._meta.pk.name
    if primary_key not in order_lookups(kept):
        kept = [*kept, primary_key]
    return kept


def custom_filter_lookups(custom_filters, relation=None):
    """The model lookups of ``customFilters`` conditions, as individual's custom filter wizards apply them."""
    from individual.custom_filters import IndividualCustomFilterWizard

    lookups = []
    for condition in custom_filters or []:
        field_and_filter, _, _ = IndividualCustomFilterWizard.parse_condition(condition)
        lookup = f"json_ext__{field_and_filter}"
        lookups.append(f"{relation}__{lookup}" if relation else lookup)
    return lookups


def _mask_at(mask, value, path):
    """``value`` with the value at the ``.`` path ``path`` masked by ``mask``, through objects and each item of
    lists; a copy."""
    if not path:
        return mask.anonymize(value)
    if isinstance(value, list):
        return [_mask_at(mask, item, path) for item in value]
    if isinstance(value, dict):
        key, _, rest = path.partition(".")
        return {name: _mask_at(mask, item, rest) if name == key else copy.deepcopy(item)
                for name, item in value.items()}
    return value


def export_column_mask(model, lookup):
    """A function masking the values of the export column ``lookup`` from ``model`` as the mask registered for
    the model it reaches masks them, or None when the column reads no masked field. A column of a masked field
    or under a masked path is masked whole; a ``json_ext`` column or path holding masked paths has those masked.
    A column that names no field (an annotation, a misspelt path) may read anything, so it is masked whole."""
    parts = lookup.split("__")
    for index, part in enumerate(parts):
        mask = MaskingClassStorage.get_masking_class(model.__name__)
        fields = mask.anon_fields if mask is not None and mask.masking_enabled else []
        if part == "json_ext" and fields:
            path = ".".join(parts[index + 1:])
            masked = [field[len(JSON_PREFIX):] for field in fields if field.startswith(JSON_PREFIX)]
            if path and any(path == key or path.startswith(f"{key}.") for key in masked):
                return mask.anonymize
            inner = [key[len(path) + 1:] if path else key for key in masked if not path or key.startswith(f"{path}.")]
            if not inner:
                return None

            def mask_inner(value, mask=mask, inner=inner):
                for key in inner:
                    value = _mask_at(mask, value, key)
                return value

            return mask_inner
        if part in _masked_columns(model, fields):
            return mask.anonymize
        try:
            field = model._meta.get_field(part)
        except FieldDoesNotExist:
            return RegisteredFieldsMask().anonymize
        if not field.is_relation:
            return None
        model = field.related_model
    return None


def _is_null(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def mask_export_columns(model, user, data_frame):
    """``data_frame``, an export table with one column per lookup from ``model``, with the values of the columns
    reading a masked field masked, unless ``user`` may see masked data. An export reads the stored values, which
    no other mask reaches; without a user the columns are masked."""
    if user is not None and may_see_masked(user):
        return data_frame
    for column in data_frame.columns:
        masker = export_column_mask(model, column)
        if masker is not None:
            data_frame[column] = data_frame[column].map(lambda value: value if _is_null(value) else masker(value))
    return data_frame


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


def holds_mask_placeholder(value):
    """Whether ``value`` is the string mask or holds it as a leaf of its lists and objects, at any depth."""
    if isinstance(value, str):
        return value == MASK_PLACEHOLDER
    if isinstance(value, dict):
        return any(holds_mask_placeholder(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(holds_mask_placeholder(item) for item in value)
    return False


def _restore_masked_leaves(incoming, stored):
    """(value, refused) for a value given to a masked field: each leaf holding the string mask where the stored
    value has a number or boolean takes that stored leaf back; a mask anywhere else is refused."""
    if isinstance(incoming, str):
        if incoming == MASK_PLACEHOLDER:
            if isinstance(stored, (bool, int, float, Decimal)):
                return copy.deepcopy(stored), False
            return incoming, True
        return incoming, False
    if isinstance(incoming, dict):
        restored, refused = {}, False
        for key, item in incoming.items():
            stored_item = stored.get(key, _ABSENT) if isinstance(stored, dict) else _ABSENT
            restored[key], item_refused = _restore_masked_leaves(item, stored_item)
            refused = refused or item_refused
        return restored, refused
    if isinstance(incoming, (list, tuple)):
        restored, refused = [], False
        for index, item in enumerate(incoming):
            stored_item = stored[index] if isinstance(stored, (list, tuple)) and index < len(stored) else _ABSENT
            value, item_refused = _restore_masked_leaves(item, stored_item)
            restored.append(value)
            refused = refused or item_refused
        return restored, refused
    return incoming, False


def check_masked_values_sent_back(user, masking_model, instance, obj_data):
    """Refuses an update of ``instance`` by a user without the masked-data right that gives a masked field the
    string mask where the stored value is a string or is missing (the value itself, or a leaf of its lists and
    objects), naming the field. A leaf given the mask where a number or boolean is stored, a date column given
    the mask, a masked choice column or foreign key given null (the form its mask shows), and a masked json_ext
    key that the update's json_ext leaves out keep their stored value (``obj_data`` is adjusted in place). Any
    other value is written as given."""
    if instance is None or may_see_masked(user):
        return
    mask = MaskingClassStorage.get_masking_class(masking_model)
    if mask is None or not mask.masking_enabled:
        return
    keep_masked_foreign_keys_given_null(mask.anon_fields, instance, obj_data)
    refused = []
    for field in mask.anon_fields:
        name, *path = field.split(".")
        if name not in obj_data:
            continue
        stored = _value_at(getattr(instance, name, None), path)
        incoming = _value_at(obj_data[name], path)
        if not path and incoming is None and stored is not None and _is_choice_column(instance, name):
            del obj_data[name]
            continue
        if incoming is _ABSENT:
            if path and isinstance(obj_data[name], dict) and stored is not _ABSENT:
                _set_at(obj_data[name], path, copy.deepcopy(stored))
            continue
        if not holds_mask_placeholder(incoming) or _equal(incoming, stored):
            continue
        if not path and isinstance(stored, (datetime.date, datetime.datetime)):
            del obj_data[name]
            continue
        restored, field_refused = _restore_masked_leaves(incoming, stored)
        if field_refused:
            refused.append(field)
        elif path:
            _set_at(obj_data[name], path, restored)
        else:
            obj_data[name] = restored
    if refused:
        raise ValidationError(
            f"masked value sent back for {', '.join(refused)}: send a new value or leave the field out")


def keep_masked_foreign_keys_given_null(fields, instance, obj_data):
    """Removes from ``obj_data`` the masked foreign keys among ``fields`` given null, by name or column name,
    where ``instance`` holds one: the mask shows them as null, so they keep their stored value."""
    for field in fields:
        foreign_key = _foreign_key(instance, field)
        if foreign_key is None or getattr(instance, foreign_key.attname) is None:
            continue
        for key in (foreign_key.name, foreign_key.attname):
            if key in obj_data and obj_data[key] is None:
                del obj_data[key]


def masked_row_fields_holding_the_mask(row, masking_model="IndividualDataSource"):
    """The masked keys of the upload row ``row`` (its values by key) whose value holds the string mask."""
    mask = MaskingClassStorage.get_masking_class(masking_model)
    if mask is None or not mask.masking_enabled:
        return []
    fields = []
    for field in mask.anon_fields:
        key, *path = field[len(JSON_PREFIX):].split(".")
        value = _value_at(row.get(key, _ABSENT) if isinstance(row, dict) else _ABSENT, path)
        if value is not _ABSENT and holds_mask_placeholder(value) and key not in fields:
            fields.append(key)
    return fields
