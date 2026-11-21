"""Upload columns that other modules read from the staged rows, outside ``individual_schema``.

A module registers a provider under a key: an object with ``template_columns()``, the column names added
to the upload template, and ``accepts(name)``, whether an uploaded column is one of its columns. An
accepted column passes the header check and stays in the row's ``IndividualDataSource.json_ext``; like
``group_code``, it is removed from the person's ``json_ext`` once the group step has run.
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
