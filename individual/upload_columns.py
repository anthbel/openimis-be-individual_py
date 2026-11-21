"""Upload columns that other modules read from the staged rows, outside ``individual_schema``.

A module registers a provider under a key: an object with ``template_columns()``, the column names added
to the upload template, ``accepts(name)``, whether an uploaded column passes the header check as one of
its columns, and optionally ``removes(name)``, whether a staged column is kept off the person
(``accepts`` when absent). A removed column stays in the row's ``IndividualDataSource.json_ext`` and never
reaches ``Individual.json_ext``; a property of ``individual_schema`` is never removed.
"""

_providers = {}


def register_upload_columns(key, provider):
    """Registers ``provider`` under ``key``; None removes it."""
    if provider is None:
        _providers.pop(key, None)
    else:
        _providers[key] = provider


def template_columns():
    """The registered columns of the template, in registration order, each once."""
    columns = [column for provider in list(_providers.values()) for column in (provider.template_columns() or [])]
    return list(dict.fromkeys(columns))


def is_registered_column(name):
    """Whether a provider accepts ``name``; a header that is not text, such as a number read from a spreadsheet, is
    never a registered column."""
    return isinstance(name, str) and any(provider.accepts(name) for provider in list(_providers.values()))


def _removes(provider, name):
    removes = getattr(provider, "removes", None)
    return removes(name) if removes is not None else provider.accepts(name)


def removed_columns(names, schema_properties):
    """The names among ``names`` that are kept off the person."""
    return [name for name in names if isinstance(name, str) and name not in schema_properties
            and any(_removes(provider, name) for provider in list(_providers.values()))]


def person_json_ext(row, schema_properties):
    """A staged row's json_ext as the person's, without the removed columns."""
    removed = set(removed_columns(row, schema_properties))
    return {key: value for key, value in row.items() if key not in removed}
