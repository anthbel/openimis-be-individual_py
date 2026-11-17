# openIMIS Backend individual reference module
This repository holds the files of the openIMIS Backend Individual reference module.
It is dedicated to be deployed as a module of [openimis-be_py](https://github.com/openimis/openimis-be_py).

## ORM mapping:
* individual_individual, individual_historicalindividual > Individual
* individual_individualdatasource, individual_historicalindividualdatasource > IndividualDataSource
* individual_individualdatasourceupload, individual_historicalindividualdatasourceupload > IndividualDataSourceUpload
* individual_group, individual_historicalgroup > Group
* individual_groupindividual, individual_historicalgroupindividual > GroupIndividual

## GraphQl Queries
* individual
* individualDataSource
* individualDataSourceUpload
* group
* groupIndividual
* groupExport
* individualExport
* groupIndividualExport

## GraphQL Mutations - each mutation emits default signals and return standard error lists (cfr. openimis-be-core_py)
* createIndividual
* updateIndividual
* deleteIndividual
* createGroup
* updateGroup
* deleteGroup
* addIndividualToGroup
* editIndividualInGroup
* removeIndividualFromGroup
* createGroupIndividuals

## Services
- Individual
  - create
  - update
  - delete
- IndividualDataSource
  - create
  - update
  - delete
- Group
  - create
  - update
  - delete
  - create_group_individuals
  - update_group_individuals
- GroupIndividualService
  - create
  - update
  - delete

Saving a group that has a location gives that location to its live members (memberships not deleted); a former member keeps their own.

An enrolment (`confirmIndividualEnrollment`, `confirmGroupEnrollment`, through `select_individuals_to_benefit_plan` and `select_groups_to_benefit_plan`) selects only the persons and groups the user may see, as the `individual` and `group` queries scope them by the user's locations; `individualEnrollmentSummary` and `groupEnrollmentSummary` count within the same scope, so the preview matches the enrolment.

## Configuration options (can be changed via core.ModuleConfiguration)
* gql_individual_search_perms: required rights to call individual GraphQL Query (default: ["159001"])
* gql_individual_create_perms: required rights to call createIndividual GraphQL Mutation (default: ["159002"])
* gql_individual_update_perms: required rights to call updateIndividual GraphQL Mutation (default: ["159003"])
* gql_individual_delete_perms: required rights to call deleteIndividual GraphQL Mutation (default: ["159004"])
* gql_group_search_perms: required rights to call group GraphQL Mutation (default: ["180001"])
* gql_group_create_perms: required rights to call createGroup and addIndividualToGroup and createGroupIndividuals GraphQL Mutation (default: ["180002"])
* gql_group_update_perms: required rights to call updateGroup and editIndividualInGroup GraphQL Mutation (default: ["180003"])
* gql_group_delete_perms: required rights to call deleteGroup and removeIndividualFromGroup GraphQL Mutation (default: ["180004"])
* group_json_ext_keys_not_copied_from_head: group json_ext keys that keep the group's value when the head's json_ext is copied onto the group on a membership change (default: []). Other head values overwrite the group's, a head value set to null removes the key from the group, and keys the head does not have are left as they are.
* individual_masking_enabled, individual_mask_fields: whether `individual_mask_fields` (column names or `json_ext.<key>` paths) are masked for users without the masked-data right (`gql_query_enable_viewing_masked_data_perms`). Read when a mask is applied, so a reloaded configuration applies.

Each process compares the stored configuration with the one it applied at the start of every request and of every Celery task (one query) and applies it when it differs, with its custom filters and workflows; a configuration saved by another process (`individual_mask_fields`, `individual_schema`, ...) therefore applies in every process from its next request. A stored configuration that cannot be read is logged and the one in use stays.

## Data masking
`individual/data_masking.py` and `individual/task_masking.py` register ten `core.data_masking` masks: `Individual` and `HistoricalIndividual` (the person mask), `Group` and `HistoricalGroup` (the group mask), `GroupIndividual` and `HistoricalGroupIndividual` (the membership mask), the upload row masks `IndividualDataSource` and `GroupDataSource`, and the task masks `Task` and `HistoricalTask`. For a user without the masked-data right they mask:
* persons: `individual_mask_fields` while `individual_masking_enabled`, and the fields other modules register with `IndividualMask.register_fields(key, source)`; while any of these is masked, the merge journal `json_ext.merge_conflicts` too, whole (a duplicate merge journals there the kept and retired persons' differing values, of any field). Being a masked `json_ext` key, the journal is masked on every path below (reads, history, the group's copy of the head's keys, update tasks, exports), a filter or ordering reading it is refused or ignored, and an update leaving it out of `jsonExt` keeps it;
* memberships: the fields registered with `GroupIndividualMask.register_fields(key, source)`; a masked `role` or `recipient_type` reads as null;
* groups: the fields registered with `GroupMask.register_fields(key, source)`, and the group's copy of the masked person fields: the head's masked `json_ext` keys (less `group_json_ext_keys_not_copied_from_head`), and `head`, `members`, `primary_recipient` and `secondary_recipient` when `first_name` or `last_name` is masked, and the group's `location` when the person `location` is masked.
* tasks: the maker-checker tasks of this module's services (sources `IndividualService`, `GroupService`, `CreateGroupAndMoveIndividualService`, `GroupIndividualService`), whose `data` holds the unit in `incoming_data` and `current_data`: both copies by the mask of that unit (person, group or membership; a group task's `individuals_data` entries by the membership mask), and the entity string of any task on a person while `first_name` or `last_name` is masked. Another module masks the data of its own task source by registering a masker with `TaskMask.register_source(source, masker)` (a callable masking the given task in place; `None` removes it). Tasks of other sources keep their data. Only these task masks may be registered for `Task` and `HistoricalTask` (core applies the last one registered for a model): a second mask class registered for either fails the system checks (`individual.E001`), so `manage.py check`, `migrate`, `runserver` and the test runner stop; a server started through the WSGI application alone (`server.py`) or a celery worker runs no system check. The business data serializers of membership tasks write the person's name and the group's code with each masked column shown as the mask. tasks_management applies the task masks on the task types it resolves.

A source is a callable returning field paths (a column or `json_ext.<key>`), called each time a mask is applied; `None` removes the registration. A masked column masks the `json_ext` key of the same name too, on reads, filters, orderings, exports and writes back: the upload workflows write the whole upload row, the person's `first_name`, `last_name` and `dob` included, into the person's `json_ext`. Registered fields are masked whatever `individual_masking_enabled` says. A string, a number, a boolean and a date become the string mask `***************`, and dictionaries and lists are masked item by item; a masked choice column of a membership reads as null.

A masked foreign-key column (such as `location`, named by field or column name) reads as null, the related object and its key alike, on every path the masks reach: the GraphQL types and the types nesting them, `mask_for_user` (DCI), and the data of this module's tasks (`location` and `location_id` alike). Filters, orderings and export columns treat its name and column name (`locationId`) alike. A person or group list reached through the reverse relation of a masked foreign key (`locations { individuals }`, `locations { groups }` while `location` is masked on the person or group mask) is empty for a user without the masked-data right, since its rows would tell the masked value. A masked person `location` masks the group's `location` too, as members take their household's location. A masked `location` masks the `json_ext` keys holding a copy of it too: `location_name` and `location_code`, which the upload workflows write from the upload row, and `location_str`, which a person update writes; upload rows mask the same keys and the note of a validation error on them. An export column under a masked foreign key holds the string mask. While `location` is masked, the `parentLocation` filter of `individual` and `group` is refused to a user without the masked-data right, as any filter reading a masked field is.

`IndividualGQLType`, `IndividualHistoryGQLType`, `GroupGQLType` and `GroupHistoryGQLType` apply the mask on every path that reaches them (the `individual`, `group` and their history queries, `group.head`, `groupIndividual.individual` and `.group`, the data sources, and the types of other modules pointing to them). `dob` is therefore a string: `YYYY-MM-DD`, or the mask.

Upload rows are masked by the mask of the unit they hold (`IndividualDataSource` by the person mask, `GroupDataSource` by the group mask, each registered under its model name): the row's keys of the masked columns and `json_ext` keys, the keys holding a membership column the membership mask masks (`individual_role` and `recipient_info` of a person row for `role` and `recipient_type`, `role` and `recipient_type` of each entry of a group row's `individuals_data`), and the note of a validation error whose `field_name` is one of them. This applies to `individualDataSource` and `groupDataSource` and the types reaching them, and to the invalid rows download (`download_invalid_items`). The uploaded file (`download_individual_upload_file`) holds the stored values, so it is refused to a user without the masked-data right while the person mask masks any field.

An update upload (`Python Update Individuals`) refuses a row whose masked key (a masked column or `json_ext` key of the person mask, or a membership key the membership mask masks) holds the string mask: its validation records an error with the key as `field_name`, so the update step leaves the row out and updates the other rows. This holds whatever the uploader's rights.

A mask whose fields cannot be computed (a registered source raising) makes the fields of the object it masks resolve to errors, never to the stored values.

For a user without the masked-data right, a person or group update (`updateIndividual`, `updateGroup`, directly or through the update task) refuses a masked field given a value holding the string mask (the value itself, or a leaf of its lists and objects at any depth), naming the field. A masked `role` or `recipient_type` given null in a membership update (`editIndividualInGroup`, directly or through the update task) keeps the stored value, since the membership mask shows it as null; so does a masked foreign key given null (`location_id`) to the person and group update services (`IndividualService` and `GroupService` `update` and `create_update_task`). The `updateIndividual` mutation refuses a null `locationId` for every user before the service is reached. A leaf given the string mask where a number or boolean is stored keeps the stored value, so a masked number or boolean sent back as read changes nothing; any other number or boolean, 0 and false included, is written as given. In `updateIndividual` the person fields are optional and a field left out keeps its value; `dob` (type `DateOrMask`: a `YYYY-MM-DD` date, or the mask) given as the mask keeps the stored date, and a masked `json_ext` key left out of the given `jsonExt` keeps its stored value.

For a user without the masked-data right, a filter or a custom filter reading a masked field is refused, naming the lookup, and an `orderBy` term ordering by one, or crossing a JSON field no mask describes, is ignored by the ordering rule of `masked_order_by` below (logged at info level; a connection has no channel for a message), since their results would tell the masked values: the filter arguments, `orderBy` and `customFilters` of the `individual`, `group`, `groupIndividual`, data source and history connections, of the connections nested in a person, group or upload (`groupindividuals`, `individualdatasourceSet`, `groupdatasourceSet`), the `orderBy` of `individualDataSourceUpload` and `individualDataUploadHistory`, and of the exports; the `firstName` and `lastName` arguments of `group`, `jsonExtHead_Icontains` of `groupHistory`, and the `customFilters` of `individualEnrollmentSummary` and `groupEnrollmentSummary`, and of the enrolments `confirmIndividualEnrollment` and `confirmGroupEnrollment` (checked in `select_individuals_to_benefit_plan` and `select_groups_to_benefit_plan`). A lookup reads a masked field when it reaches a masked column or `json_ext` path through its relations, or a part of a mask field given as a dotted path through a relation (`group.code`, `group.json_ext.income`, as other modules register them); a lookup on the whole `json_ext` reads every key, and a numeric part of a lookup indexes a list (`json_ext__individuals_data__0__role` reads the mask field `json_ext.individuals_data.role`). On these connections an `orderBy` ends with the model's primary key (`id`, or `history_id` on a history connection), for every user, so a list ordered by an ignored term or by a non-unique one is ordered by id and pages neither repeat nor skip rows. The filter set of the person, group, membership and upload row types (and their history types) refuses such a filter on every connection over them, so the connections other modules nest in their own types (`LocationGQLType.individuals` and `.groups`, `UserGQLType.individualUserCreated` and the like) refuse it too; those connections take no `orderBy`. Filters other modules declare on their own types (social_protection's beneficiary filters and `search`) are checked by those modules.

The connections of this module, and those other modules build with `individual.gql_queries.MaskedOrderedConnectionField` (core's ordered connection), share one ordering rule (`individual.data_masking.masked_order_by`): for a user without the masked-data right, an `orderBy` term is left out (logged at info level) when its path, through any relation, reads a masked field (a masked `json_ext` key of a model with a registered mask included: person, group, membership, upload rows, beneficiaries), or traverses a JSON field no mask describes (any other JSON field: task data, evidence, inputs, a `json_ext` of a model without a mask), which may hold copies of masked values, or a name that is no field of the models it crosses. An unmasked `json_ext` key of a model with a registered mask still orders. The primary key orders last, for every user. A subclass names in `order_aliases` the lookups an annotation of its resolver is computed from (an empty tuple: none), so the annotation is checked as those lookups.

`individualExport`, `groupExport` and `groupIndividualExport` read the stored values, which no other mask reaches, so for a user without the masked-data right the file is written with the columns reading a masked field masked, as the queries mask them: a column of a masked field (a column of a person, group or membership reached through a relation included) holds the string mask, and a `jsonExt` column, unfolded or reached through a relation, holds its masked keys masked and its other keys in clear. A column that names no field of the model (an annotation, a misspelt path) is written masked whole. A user with the right exports the stored values. A mask whose fields cannot be computed fails the export and writes no file. The exporters belong to this module's `Query`: in the assembled schema, which combines it with the queries of other modules, the export settings of those modules (`related_field`, `object_type`) do not apply to them, so `customFilters` filter `individualExport` on the persons' and `groupExport` on the groups' `json_ext`.


## openIMIS Modules Dependencies
- core


## Enabling Python Workflows
Module comes with simple workflows for individual data upload. 
They should be used for the development purposes, not in production environment. 
To activate these Python workflows, a configuration change is required. 
Specifically, the `enable_python_workflows` parameter to `true` within module config.

Workflows: 
 * individual upload


## Python import pipeline (opt-in)

An alternative to the SQL upload workflows, chosen per upload by picking the
`Python Pipeline Import Individuals` workflow (group `individual`). The SQL
workflows stay the default. Code: `individual/import_pipeline.py`.

The pipeline workflows are registered only while `enable_python_workflows` is on.
With the `celery` executor and `dispatch: worker`, the workers read the uploaded
file from `default_storage`: the backend and the workers must share its storage
(in openimis-dist_dkr, the `file_storage` volume). Otherwise use `dispatch: request`.

1. **Stage and validate** (one task per chunk, in parallel): parse a chunk,
   check `first_name`, `last_name`, `dob` (YYYY-MM-DD), validation calculations
   and the village, and insert the rows with their result. The resolved
   village id is kept in `validations.location_id`.
2. **Deduplicate** (once, after every chunk): the schema fields marked
   `"uniqueness"` are compared across the whole upload, against existing
   individuals, and against other uploads still pending. Because comparison
   happens after all chunks are in, a duplicate split across chunks is found.
3. **Review**: the usual maker-checker task (or an immediate import).
4. **Import** (per chunk): individuals are created from the accepted valid rows
   and linked to their row by id.

| Config key | Default | Meaning |
|---|---|---|
| `individual_import_pipeline_executor` | `auto` | `auto` (the fastest available: `celery` when async mutations are on, `inline` otherwise), `inline`, `process` (local process pool, for validation; the import runs inline in the approval) or `celery` (one task per chunk for validation and for import; the last import chunk runs the group step) |
| `individual_import_pipeline_chunk_size` | `5000` | rows per chunk |
| `individual_import_pipeline_workers` | `4` | processes for the `process` executor |
| `individual_import_pipeline_dispatch` | `worker` | `celery` executor only: `worker` reads the file and queues its chunks in a worker, so the request returns at once; `request` does it in the request |
| `individual_import_duplicate_policy` | `flag_all` | `flag_all` rejects every copy, as the SQL path does; `keep_first` keeps the first row |

Measured on one machine (14 cores, local PostgreSQL) with 1,000,000 rows, the
`process` executor and 8 workers: stage and validate 96 s, deduplicate 3 s,
import of 970,125 individuals 213 s, about 550 MB in the parent process.

## Following an upload

`individualDataSourceUpload` / `individualDataUploadHistory { dataUpload { status error progress } }`:
`status` moves `TRIGGERED` → `IN_PROGRESS` → `WAITING_FOR_VERIFICATION` →
(approval) `IN_PROGRESS` → `SUCCESS` / `PARTIAL_SUCCESS` / `FAIL`; `progress` is
`{"stage": "validation" | "import", "done", "total", "duplicates"}` for pipeline
uploads (`total` is null while the file is still being read). The approval
mutation completing only means the import is queued: the upload status says when
it is done. The upload history dialog polls while an upload is running.

## Upload visibility

An upload has no location of its own. It is visible to its uploader, to whoever
may see its review task, and to whoever may see an individual created from it.
Rows not yet imported are visible to the uploader and the reviewers; imported
rows follow their individual.

## Additional Field Definition

Individual model comes with a minimal set of fields: `first_name`, `last_name`, `dob`.
To add additional fields, define them in the backend admin interface by adding a Module configuration for `individual`:

1. In the web app, visit URL path `/api/admin/core/moduleconfiguration` in browser
2. Click on ADD MODULE CONFIGURATION
3. Fill in the form with the following values:
    - Module: individual
    - Layer: backend
    - Version: 1
    - Config: `{"individual_schema": "{\"$id\": \"https://example.com/beneficiares.schema.json\", \"type\": \"object\", \"title\": \"Record of beneficiares\", \"$schema\": \"http://json-schema.org/draft-04/schema#\", \"properties\": {\"email\": {\"type\": \"string\", \"description\": \"email address to contact with beneficiary\", \"validationCalculation\": {\"name\": \"EmailValidationStrategy\"}}, \"able_bodied\": {\"type\": \"boolean\", \"description\": \"Flag determining whether someone is able bodied or not\"}, \"national_id\": {\"type\": \"string\", \"description\": \"national id\"}, \"educated_level\": {\"type\": \"string\", \"description\": \"The level of person when it comes to the school/education/studies\"}, \"chronic_illness\": {\"type\": \"boolean\", \"description\": \"Flag determining whether someone has such kind of illness or not\"}, \"national_id_type\": {\"type\": \"string\", \"description\": \"A type of national id\"}, \"number_of_elderly\": {\"type\": \"integer\", \"description\": \"Number of elderly\"}, \"number_of_children\": {\"type\": \"integer\", \"description\": \"Number of children\"}, \"beneficiary_data_source\": {\"type\": \"string\", \"description\": \"The source from where such beneficiary comes\"}}, \"description\": \"This document records the details beneficiares\"}"}`
   Modify the `Config` value accordingly with your individual additional field definitions.
4. Click on SAVE

### Generate dummy individuals for development

**Requirements**

- [openimis-be_py](https://github.com/openimis/openimis-be_py) repo is cloned and setup locally. Django command execution requires this repo.
- Ensure all dependencies are installed, see openimis-be_py [README](https://github.com/openimis/openimis-be_py/blob/develop/README.md#developers-setup)

First, generate a csv file with a list of individuals which can be used for uploading:

```bash
# go to the openimis-be_py repo, openIMIS folder
cd ../openimis-be_py/openIMIS/

python manage.py fake_individuals
```

The `fake_individuals` command generates 100 individuals using the sample `individual_schema` provided above.
Feel free to modify the fields and number of individuals as needed.
The last line of the output should provide the path to the temporary csv file that contains the list of dummy individuals.

Then upload the generated csv file in the web app:
- Go to "Social Protection" > "Individuals" > "UPLOAD" and select the generated csv as the file to upload.
- Choose "Python Import Individuals" as the Workflow, leave "Create groups from column:" empty, then click on UPLOAD INDIVIDUALS.
- Go to "Task Management" > "All Tasks", find and approve the `import_valid_items` task.
- Then you should see the list of individuals appear under "Social Protection" > "Individuals"
