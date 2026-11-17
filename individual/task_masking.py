from django.apps import apps
from django.contrib.contenttypes.models import ContentType

from core.data_masking import DataMaskAbs
from individual.data_masking import (
    GroupIndividualMask, GroupMask, IndividualMask, MASK_PLACEHOLDER, NAME_COLUMNS, foreign_key_columns,
    may_see_masked,
)
from individual.models import Individual

# The two copies of the unit a maker-checker task holds in its data.
TASK_DATA_KEYS = ("incoming_data", "current_data")
GROUP_MEMBERS_KEY = "individuals_data"

# For each task source of this module (the name of the service class creating the task), the masks of the
# unit its data holds, each with the data path prefix it applies under.
TASK_SOURCE_MASKS = {
    "IndividualService": ((IndividualMask, ""),),
    "GroupService": ((GroupMask, ""), (GroupIndividualMask, f"{GROUP_MEMBERS_KEY}.")),
    "CreateGroupAndMoveIndividualService": ((GroupMask, ""), (GroupIndividualMask, f"{GROUP_MEMBERS_KEY}.")),
    "GroupIndividualService": ((GroupIndividualMask, ""),),
}


def _mask_path(replace, value, parts):
    """Replaces in place the value at the key path ``parts`` of ``value`` by ``replace(value)``, through objects
    and each item of lists."""
    if isinstance(value, list):
        for item in value:
            _mask_path(replace, item, parts)
        return
    if not isinstance(value, dict) or not parts or parts[0] not in value:
        return
    if len(parts) == 1:
        value[parts[0]] = replace(value[parts[0]])
    else:
        _mask_path(replace, value[parts[0]], parts[1:])


def _null(value):
    return None


def _masked_names():
    return [column for column in NAME_COLUMNS if column in IndividualMask().anon_fields]


class TaskMask(DataMaskAbs):
    """The tasks of this module's services, their data masked by the mask of the unit it holds (both its
    incoming and current copies); the tasks of the sources other modules register with ``register_source``,
    by the masker they register; and the entity string of any task on a person, masked while a name column is.
    Tasks of other sources keep their data."""

    masking_model = "Task"
    _maskers = {}

    @classmethod
    def register_source(cls, source, masker):
        """Registers ``masker``, a callable masking in place the task of ``source`` it is given (for a reader
        without the masked-data right), under ``source``; None removes it. The history mask shares them."""
        if masker is None:
            TaskMask._maskers.pop(source, None)
        else:
            TaskMask._maskers[source] = masker

    @property
    def masking_enabled(self):
        units = {mask for masks in TASK_SOURCE_MASKS.values() for mask, _ in masks}
        return bool(TaskMask._maskers) or any(mask().masking_enabled for mask in units)

    def apply_mask(self, task):
        source = getattr(task, "source", None)
        data = getattr(task, "data", None)
        if isinstance(data, dict):
            for mask_class, prefix in TASK_SOURCE_MASKS.get(source, ()):
                mask = mask_class()
                fields = mask.anon_fields
                for field in fields:
                    for key in TASK_DATA_KEYS:
                        _mask_path(mask.anonymize, data.get(key), f"{prefix}{field}".split("."))
                # A masked foreign key reads as null, under its name and its column name.
                for column in foreign_key_columns(apps.get_model("individual", mask.masking_model), fields):
                    for key in TASK_DATA_KEYS:
                        _mask_path(_null, data.get(key), f"{prefix}{column}".split("."))
        masker = TaskMask._maskers.get(source)
        if masker is not None:
            masker(task)
        if hasattr(task, "entity_string") and self._on_person(task) and _masked_names():
            task.entity_string = MASK_PLACEHOLDER
        return task

    @staticmethod
    def _on_person(task):
        entity_type_id = getattr(task, "entity_type_id", None)
        return bool(entity_type_id) and ContentType.objects.get_for_id(entity_type_id).model_class() is Individual


class HistoricalTaskMask(TaskMask):
    masking_model = "HistoricalTask"


def check_task_masks(app_configs=None, **kwargs):
    """System check: an error for each of Task and HistoricalTask holding more than one registered mask class.
    Core applies only the mask registered last for a model, so a second class would replace these task masks;
    another module masks its tasks through ``TaskMask.register_source``."""
    from django.core.checks import Error
    from core.data_masking import MaskingClassRegistryPoint

    classes = {}
    for entry in MaskingClassRegistryPoint.REGISTERED_MASKING_CLASS:
        if entry["model"] in (TaskMask.masking_model, HistoricalTaskMask.masking_model):
            mask_class = type(entry["class_reference"])
            classes.setdefault(entry["model"], {})[f"{mask_class.__module__}.{mask_class.__qualname__}"] = None
    return [
        Error(
            f"{len(names)} mask classes are registered for {model}: {', '.join(names)}",
            hint="Register a task masker with individual.task_masking.TaskMask.register_source instead.",
            id="individual.E001",
        )
        for model, names in classes.items() if len(names) > 1
    ]


def person_name(user, individual):
    """The person's name as ``user`` may see it, each masked name column showing the mask."""
    masked = [] if user is not None and may_see_masked(user) else _masked_names()
    return " ".join(MASK_PLACEHOLDER if column in masked else f"{getattr(individual, column)}"
                    for column in NAME_COLUMNS)


def group_code(user, group):
    """The group's code as ``user`` may see it."""
    if (user is None or not may_see_masked(user)) and "code" in GroupMask().anon_fields:
        return MASK_PLACEHOLDER
    return group.code
