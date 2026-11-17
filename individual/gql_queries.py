import django_filters
import graphene
from django.contrib.auth.models import AnonymousUser
from graphene_django import DjangoObjectType
from graphene_django.types import DjangoObjectTypeOptions
from graphene_django.filter import DjangoFilterConnectionField
import graphene_django_optimizer as gql_optimizer

from core import prefix_filterset, ExtendedConnection
from core.gql_queries import UserGQLType
from core.schema import OrderedDjangoFilterConnectionField
from individual.apps import IndividualConfig
from individual.data_masking import (
    check_filter_lookups, custom_filter_lookups, filter_lookups, lists_masked_reverse_relation, mask_once,
    masked_order_by, masking_resolver,
)
from individual.models import Individual, IndividualDataSource, Group, GroupIndividual, \
    IndividualDataSourceUpload, IndividualDataUploadRecords, GroupDataSource
from core.gql import ScopedQuerysetMixin


def _have_permissions(user, permission):
    if isinstance(user, AnonymousUser):
        return False
    if not user.id:
        return False
    return user.has_perms(permission)


class JsonExtMixin:
    def resolve_json_ext(self, info):
        if _have_permissions(info.context.user, IndividualConfig.gql_individual_search_perms):
            return self.json_ext
        return None


class MaskedFilterSet(django_filters.FilterSet):
    """A filter set refusing to a user without the masked-data right a filter reading a masked field, naming the
    lookup (``check_filter_lookups``), on every connection using it."""

    def filter_queryset(self, queryset):
        user = getattr(self.request, "user", None)
        if user is not None:
            check_filter_lookups(user, self._meta.model, filter_lookups(type(self), self.form.cleaned_data))
        return super().filter_queryset(queryset)


class MaskedFilterTypeOptions(DjangoObjectTypeOptions):
    """Options whose filter set, when none is given, is a ``MaskedFilterSet`` of the type's ``filter_fields``;
    ``DjangoObjectType`` sets ``filter_fields`` before ``filterset_class``."""

    def __setattr__(self, name, value):
        if name == "filterset_class" and value is None and getattr(self, "filter_fields", None):
            meta = type("Meta", (), {"model": self.model, "fields": self.filter_fields})
            value = type(f"{self.class_type.__name__}FilterSet", (MaskedFilterSet,), {"Meta": meta})
        super().__setattr__(name, value)


class MaskedFilterObjectType(DjangoObjectType):
    """A type whose connections refuse masked-field filters (``MaskedFilterSet``), those other modules nest in
    their own types included (``LocationGQLType.individuals``, ``UserGQLType.individualUserCreated``); its
    ``filter_fields`` stay for the types prefixing them."""

    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(cls, _meta=None, **options):
        super().__init_subclass_with_meta__(_meta=_meta or MaskedFilterTypeOptions(cls), **options)


class MaskedFilterMixin:
    """A connection refusing to a user without the masked-data right its filter arguments and ``customFilters``
    when they read a masked field (``check_filter_lookups``), and leaving out the ``orderBy`` terms that would
    tell masked values (``masked_order_by``)."""

    @classmethod
    def resolve_queryset(cls, connection, iterable, info, args, filtering_args, filterset_class):
        user, model = info.context.user, filterset_class._meta.model
        lookups = filter_lookups(filterset_class, {name: value for name, value in args.items()
                                                   if name in filtering_args})
        lookups += custom_filter_lookups(args.get("customFilters"))
        check_filter_lookups(user, model, lookups)
        if args.get("orderBy"):
            args = {**args, "orderBy": masked_order_by(user, model, args["orderBy"])}
        return super().resolve_queryset(connection, iterable, info, args, filtering_args, filterset_class)


class MaskedOrderedFilterConnectionField(MaskedFilterMixin, OrderedDjangoFilterConnectionField):
    pass


class MaskedFilterConnectionField(MaskedFilterMixin, DjangoFilterConnectionField):
    pass


class MaskedOrderedConnectionField(OrderedDjangoFilterConnectionField):
    """Core's ordered connection leaving out, for a user without the masked-data right, the ``orderBy`` terms
    whose path reads a masked field or traverses a JSON field through any relation (``masked_order_by``); the
    primary key orders last. ``order_aliases`` maps an annotation the resolver adds to the lookup, or tuple of
    lookups, it is computed from; a subclass sets it for its annotations."""

    order_aliases = {}

    @classmethod
    def resolve_queryset(cls, connection, iterable, info, args, filtering_args, filterset_class):
        if args.get("orderBy"):
            args = {**args, "orderBy": masked_order_by(
                info.context.user, filterset_class._meta.model, args["orderBy"], cls.order_aliases)}
        return super().resolve_queryset(connection, iterable, info, args, filtering_args, filterset_class)


def _masked_dob(root, info, masking_model):
    dob = mask_once(info, root, masking_model).dob
    return dob.isoformat() if hasattr(dob, "isoformat") else dob


class IndividualGQLType(MaskedFilterObjectType):
    """A person, masked by the person mask on every path reaching it for a user without the masked-data
    right; ``dob`` is a string so that it can carry the mask."""

    uuid = graphene.String(source='uuid')
    dob = graphene.String(required=True, description="YYYY-MM-DD, or the mask when masked")
    groupindividuals = MaskedFilterConnectionField("individual.gql_queries.GroupIndividualGQLType", required=True)
    individualdatasource_set = MaskedFilterConnectionField(
        "individual.gql_queries.IndividualDataSourceGQLType", required=True)

    def resolve_dob(self, info):
        return _masked_dob(self, info, "Individual")

    class Meta:
        model = Individual
        interfaces = (graphene.relay.Node,)
        default_resolver = masking_resolver("Individual")
        filter_fields = {
            "id": ["exact", "isnull"],
            "first_name": ["iexact", "istartswith", "icontains"],
            "last_name": ["iexact", "istartswith", "icontains"],
            "dob": ["exact", "lt", "lte", "gt", "gte"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "location": ["isnull"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        if lists_masked_reverse_relation(info, Individual, "Individual"):
            return Individual.objects.none()
        return Individual.get_queryset(queryset, info.context.user)


class IndividualHistoryGQLType(MaskedFilterObjectType):
    """A person record, masked as ``IndividualGQLType`` is."""

    uuid = graphene.String(source='uuid')
    user_updated = graphene.Field(UserGQLType)
    dob = graphene.String(required=True, description="YYYY-MM-DD, or the mask when masked")

    def resolve_user_updated(self, info):
        return self.user_updated

    def resolve_dob(self, info):
        return _masked_dob(self, info, "HistoricalIndividual")

    class Meta:
        model = Individual.history.model
        interfaces = (graphene.relay.Node,)
        default_resolver = masking_resolver("HistoricalIndividual")
        filter_fields = {
            "id": ["exact"],
            "first_name": ["iexact", "istartswith", "icontains"],
            "last_name": ["iexact", "istartswith", "icontains"],
            "dob": ["exact", "lt", "lte", "gt", "gte"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("user_updated__", UserGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        accessible_individual_query = Individual.get_queryset(None, info.context.user)
        accessible_individuals = gql_optimizer.query(accessible_individual_query, info)
        accessible_uuids = set(accessible_individuals.values_list('uuid', flat=True))
        return queryset.filter(id__in=accessible_uuids)


class IndividualDataSourceUploadGQLType(ScopedQuerysetMixin, DjangoObjectType):
    uuid = graphene.String(source='uuid')
    json_ext = graphene.JSONString()
    progress = graphene.JSONString()
    individualdatasource_set = MaskedFilterConnectionField(
        "individual.gql_queries.IndividualDataSourceGQLType", required=True)
    groupdatasource_set = MaskedFilterConnectionField("individual.gql_queries.GroupDataSourceGQLType", required=True)

    def resolve_json_ext(self, info):
        from individual.import_pipeline import public_json_ext

        return public_json_ext(self)

    def resolve_progress(self, info):
        from individual.import_pipeline import progress

        return progress(self)

    class Meta:
        model = IndividualDataSourceUpload
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "status": ["iexact", "istartswith", "icontains"],
            "source_type": ["iexact", "istartswith", "icontains"],
            "source_name": ["iexact", "istartswith", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class IndividualDataSourceGQLType(ScopedQuerysetMixin, MaskedFilterObjectType):
    """An upload row, masked with its validation errors by the person mask on every path reaching it."""

    uuid = graphene.String(source='uuid')

    class Meta:
        model = IndividualDataSource
        interfaces = (graphene.relay.Node,)
        default_resolver = masking_resolver("IndividualDataSource")
        filter_fields = {
            "id": ["exact", "isnull"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),
            **prefix_filterset("upload__", IndividualDataSourceUploadGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class GroupGQLType(MaskedFilterObjectType):
    uuid = graphene.String(source='uuid')
    head = graphene.Field(IndividualGQLType)
    groupindividuals = MaskedFilterConnectionField("individual.gql_queries.GroupIndividualGQLType", required=True)
    groupdatasource_set = MaskedFilterConnectionField("individual.gql_queries.GroupDataSourceGQLType", required=True)

    def resolve_head(self, info):
        return Individual.objects.filter(
            groupindividuals__group__id=self.id,
            groupindividuals__role=GroupIndividual.Role.HEAD,
            groupindividuals__is_deleted=False,
        ).first()

    class Meta:
        model = Group
        interfaces = (graphene.relay.Node,)
        default_resolver = masking_resolver("Group")
        filter_fields = {
            "id": ["exact", "isnull"],
            "code": ["iexact", "istartswith", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "location": ["isnull"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        if lists_masked_reverse_relation(info, Group, "Group"):
            return Group.objects.none()
        return Group.get_queryset(queryset, info.context.user)


class GroupHistoryGQLType(MaskedFilterObjectType):
    uuid = graphene.String(source='uuid')
    user_updated = graphene.Field(UserGQLType)
    head = graphene.Field(IndividualGQLType)

    def resolve_head(self, info):
        return Individual.objects.filter(
            groupindividuals__group__id=self.id,
            groupindividuals__role=GroupIndividual.Role.HEAD,
            groupindividuals__is_deleted=False,
        ).first()

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = Group.history.model
        interfaces = (graphene.relay.Node,)
        default_resolver = masking_resolver("HistoricalGroup")
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("user_updated__", UserGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        accessible_group_query = Group.get_queryset(None, info.context.user)
        accessible_groups = gql_optimizer.query(accessible_group_query, info)
        accessible_uuids = set(accessible_groups.values_list('uuid', flat=True))
        return queryset.filter(id__in=accessible_uuids)


class GroupIndividualGQLType(MaskedFilterObjectType):
    """A membership, masked by the membership mask on every path reaching it."""

    uuid = graphene.String(source='uuid')

    class Meta:
        model = GroupIndividual
        interfaces = (graphene.relay.Node,)
        default_resolver = masking_resolver("GroupIndividual")
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "role": ["exact", "iexact", "istartswith", "icontains"],
            "recipient_type": ["exact", "iexact", "istartswith", "icontains"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),
            **prefix_filterset("group__", GroupGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return GroupIndividual.get_queryset(queryset, info.context.user)


class GroupIndividualHistoryGQLType(MaskedFilterObjectType):
    uuid = graphene.String(source='uuid')
    user_updated = graphene.Field(UserGQLType)

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = GroupIndividual.history.model
        interfaces = (graphene.relay.Node,)
        default_resolver = masking_resolver("HistoricalGroupIndividual")
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "role": ["exact", "iexact", "istartswith", "icontains"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),
            **prefix_filterset("group__", GroupGQLType._meta.filter_fields),
            **prefix_filterset("user_updated__", UserGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        accessible_group_query = Group.get_queryset(None, info.context.user)
        accessible_groups = gql_optimizer.query(accessible_group_query, info)
        accessible_uuids = set(accessible_groups.values_list('uuid', flat=True))
        return GroupIndividual.without_hidden_persons(
            queryset.filter(group__id__in=accessible_uuids), info.context.user)


class IndividualDataUploadQGLType(ScopedQuerysetMixin, DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = IndividualDataUploadRecords
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "workflow": ["exact", "iexact", "startswith", "istartswith", "contains", "icontains"],
            **prefix_filterset("data_upload__", IndividualDataSourceUploadGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class GroupDataSourceGQLType(ScopedQuerysetMixin, MaskedFilterObjectType):
    """A group upload row, masked with its validation errors by the group mask on every path reaching it."""

    uuid = graphene.String(source='uuid')

    class Meta:
        model = GroupDataSource
        interfaces = (graphene.relay.Node,)
        default_resolver = masking_resolver("GroupDataSource")
        filter_fields = {
            "id": ["exact", "isnull"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("group__", GroupGQLType._meta.filter_fields),
            **prefix_filterset("upload__", IndividualDataSourceUploadGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class IndividualSummaryEnrollmentGQLType(graphene.ObjectType):
    number_of_selected_individuals = graphene.String()
    total_number_of_individuals = graphene.String()
    number_of_individuals_not_assigned_to_programme = graphene.String()
    number_of_individuals_assigned_to_programme = graphene.String()
    number_of_individuals_assigned_to_selected_programme = graphene.String()
    number_of_individuals_to_upload = graphene.String()


class GroupSummaryEnrollmentGQLType(graphene.ObjectType):
    number_of_selected_groups = graphene.String()
    total_number_of_groups = graphene.String()
    number_of_groups_not_assigned_to_programme = graphene.String()
    number_of_groups_assigned_to_programme = graphene.String()
    number_of_groups_assigned_to_selected_programme = graphene.String()
    number_of_groups_to_upload = graphene.String()


class GlobalSchemaType(graphene.ObjectType):
    schema = graphene.JSONString()
