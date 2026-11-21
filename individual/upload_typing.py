"""How an upload file is read and its staged rows typed, by the pipeline and the SQL import path alike.

A column whose values are text is read as text, so that a code keeps its leading zeros and stays a code:
``location_code``, the registered upload columns, and the ``individual_schema`` properties typed
``string``. The other columns are read as pandas infers them; in a staged row, a whole number read as a
float in an ``integer`` property (a column with a blank cell is read as floats) becomes an int.
"""
import json

import pandas as pd

from individual.apps import IndividualConfig
from individual.upload_columns import is_registered_column

TEXT_COLUMNS = ("location_code",)


def schema_properties():
    return json.loads(IndividualConfig.individual_schema).get("properties", {})


def _types(prop):
    kind = (prop or {}).get("type")
    return set(kind) if isinstance(kind, list) else {kind}


def text_dtypes(columns, properties=None):
    """{column: str} for the columns of ``columns`` read as text."""
    properties = schema_properties() if properties is None else properties
    return {
        column: str for column in columns
        if column in TEXT_COLUMNS or is_registered_column(column) or "string" in _types(properties.get(column))
    }


def _header(handle, read):
    columns = list(read(handle, nrows=0).columns)
    handle.seek(0)
    return columns


def read_csv(handle, **kwargs):
    """``pd.read_csv`` with the text columns of the file's header read as text."""
    return pd.read_csv(handle, dtype=text_dtypes(_header(handle, pd.read_csv)), **kwargs)


def read_excel(handle, **kwargs):
    """``pd.read_excel`` with the text columns of the sheet's header read as text."""
    return pd.read_excel(handle, dtype=text_dtypes(_header(handle, pd.read_excel)), **kwargs)


def typed_record(record, properties):
    """The staged row ``record`` with each whole float of an ``integer`` property as an int."""
    return {
        key: int(value) if isinstance(value, float) and value.is_integer() and "integer" in _types(properties.get(key))
        else value
        for key, value in record.items()
    }
