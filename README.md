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

## Data masking
`individual/data_masking.py` registers eight `core.data_masking` masks: `Individual` and `HistoricalIndividual` (the person mask), `Group` and `HistoricalGroup` (the group mask), `GroupIndividual` and `HistoricalGroupIndividual` (the membership mask), and the upload row masks `IndividualDataSource` and `GroupDataSource`. For a user without the masked-data right they mask:
* persons: `individual_mask_fields` while `individual_masking_enabled`, and the fields other modules register with `IndividualMask.register_fields(key, source)`;
* memberships: the fields registered with `GroupIndividualMask.register_fields(key, source)`; a masked `role` or `recipient_type` reads as null;
* groups: the fields registered with `GroupMask.register_fields(key, source)`, and the group's copy of the masked person fields: the head's masked `json_ext` keys (less `group_json_ext_keys_not_copied_from_head`), and `head`, `members`, `primary_recipient` and `secondary_recipient` when `first_name` or `last_name` is masked.

A source is a callable returning field paths (a column or `json_ext.<key>`), called each time a mask is applied; `None` removes the registration. Registered fields are masked whatever `individual_masking_enabled` says. A string, a number, a boolean and a date become the string mask `***************`, and dictionaries and lists are masked item by item; a masked choice column of a membership reads as null.

`IndividualGQLType`, `IndividualHistoryGQLType`, `GroupGQLType` and `GroupHistoryGQLType` apply the mask on every path that reaches them (the `individual`, `group` and their history queries, `group.head`, `groupIndividual.individual` and `.group`, the data sources, and the types of other modules pointing to them). `dob` is therefore a string: `YYYY-MM-DD`, or the mask.

Upload rows are masked by the mask of the unit they hold (`IndividualDataSource` by the person mask, `GroupDataSource` by the group mask, each registered under its model name): the row's keys of the masked columns and `json_ext` keys, the keys holding a membership column the membership mask masks (`individual_role` and `recipient_info` of a person row for `role` and `recipient_type`, `role` and `recipient_type` of each entry of a group row's `individuals_data`), and the note of a validation error whose `field_name` is one of them. This applies to `individualDataSource` and `groupDataSource` and the types reaching them, and to the invalid rows download (`download_invalid_items`). The uploaded file (`download_individual_upload_file`) holds the stored values, so it is refused to a user without the masked-data right while the person mask masks any field.

A mask whose fields cannot be computed (a registered source raising) makes the fields of the object it masks resolve to errors, never to the stored values.

For a user without the masked-data right, a person or group update (`updateIndividual`, `updateGroup`, directly or through the update task) refuses a masked field given a value holding the string mask (the value itself, or a leaf of its lists and objects at any depth), naming the field. A leaf given the string mask where a number or boolean is stored keeps the stored value, so a masked number or boolean sent back as read changes nothing; any other number or boolean, 0 and false included, is written as given. In `updateIndividual` the person fields are optional and a field left out keeps its value; `dob` (type `DateOrMask`: a `YYYY-MM-DD` date, or the mask) given as the mask keeps the stored date, and a masked `json_ext` key left out of the given `jsonExt` keeps its stored value.

`individualExport`, `groupExport` and `groupIndividualExport` read the stored values, so a column reading a masked field (a whole `jsonExt` while any of its keys is masked, a column of a person or group reached through a relation included) is refused to a user without the masked-data right.


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
