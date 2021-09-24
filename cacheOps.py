from ast import literal_eval
import csv
import json

import requests

from cbrainAPI import (
    cbrain_list_data_provider,
    cbrain_post_task,
    cbrain_get_task_info_from_list,
    cbrain_download_text,
    cbrain_sync_file,
)

#############################################


def download_cache(cache_file, CCI_token, latest_artifacts_url):
    """Downloads newest cache file to json, or if it's not found in the circleCI artifacts, creates a new cache file."""

    headers = {"Circle-Token": CCI_token}
    response = requests.get(
        str(latest_artifacts_url), headers=headers
    )  # finds the link to the cache file amongst all the artifacts
    # example URL for this repo: https://circleci.com/api/v1.1/project/github/jacobsanz97/NDR-CI/latest/artifacts

    link_to_cache = "http://"
    if response.status_code == 200:
        literal_list = literal_eval(
            response.text
        )  # convert text to dictionary so we can browse it
        for file in literal_list:
            if cache_file in file["url"]:
                link_to_cache = file["url"]
    else:
        print("Error loading CircleCI artifacts")
        print(response.text)

    try:
        response = requests.get(
            link_to_cache, headers=headers
        )  # download the cache file to json
    except:
        json_cache = json.loads(
            "{}"
        )  # Cache file couldn't be loaded, so we create an empty json
        print("Cache file not found...Creating a new one.")
    else:
        json_cache = json.loads(response.text)

    with open(cache_file, "w") as outfile:  # create cache file for CI
        json.dump(json_cache, outfile)
    print("written cache to temp file")


def generate_cache_subject(
    nifti_file, cbrain_userfile_ID, pipeline, experiment_definition
):
    """Creates a template for a cache entry (cbrain data provider file), for a specific pipeline. Provides a userfile ID as a starting point for task computations."""

    data = {nifti_file: {pipeline: {}}}

    result = {"result": None, "isUsed": None}

    component_number = (
        0  # Keeps track of the order of the component (we need to flag the first one)
    )
    for pipeline_component in experiment_definition["Pipelines"][pipeline][
        "Components"
    ]:

        if component_number == 0:
            component_record = {
                "inputID": cbrain_userfile_ID,  # only do this for first component
                "toolConfigID": None,
                "taskID": None,
                "status": None,
                "outputID": None,
                "isUsed": None,
            }
        else:
            component_record = {
                "inputID": None,
                "toolConfigID": None,
                "taskID": None,
                "status": None,
                "outputID": None,
                "isUsed": None,
            }

        data[nifti_file][pipeline][
            pipeline_component
        ] = component_record  # add this component to the cache
        component_number = component_number + 1

    data[nifti_file][pipeline][
        "Result"
    ] = result  # add the results section after all the component sections
    return data


def populate_cache_filenames(
    cache_file,
    cbrain_token,
    blocklist,
    pipeline,
    data_provider_id,
    experiment_definition,
):
    """Generates the template for every file in a cache, for a specific pipeline."""

    filelist = []
    data_provider_browse = cbrain_list_data_provider(
        str(data_provider_id), cbrain_token
    )  # Query CBRAIN to list all files in data provider.

    try:
        for entry in data_provider_browse:
            if "userfile_id" in entry:  # if it's a registered file, add to filelist.
                filelist.append([entry["name"], entry["userfile_id"]])
    except Exception as e:
        print(
            "Error in browsing data provider, will continue using the filelist from the previous CI run"
        )
        return  # skips the function without crashing

    with open(cache_file, "r+") as file:
        data = json.load(file)
        for entry in filelist:

            if (
                entry[0] not in data and entry[0] not in blocklist
            ):  # if entry[name] is not in cache AND is not in the blocklist...add to cache
                leaf = generate_cache_subject(
                    entry[0], entry[1], pipeline, experiment_definition
                )
                data.update(leaf)

            if (
                entry[0] not in blocklist and pipeline not in data[entry[0]]
            ):  # if already in cache, just add entry for new pipeline.
                leaf = generate_cache_subject(
                    entry[0], entry[1], pipeline, experiment_definition
                )
                data[entry[0]][pipeline] = leaf[entry[0]][pipeline]

        file.seek(0)  # rewind
        json.dump(data, file, indent=2)
        file.truncate()
        return data


def update_statuses(cache_filename, task_list):
    """Updates a cache file with the newest task statuses from CBRAIN."""

    with open(cache_filename, "r+") as cache_file:
        data = json.load(cache_file)
        for (file, pipeline) in data.items():  # Parse the json
            for (pipeline_name, task_name) in pipeline.items():
                for (task_name_str, params) in task_name.items():

                    if (
                        task_name_str != "Result"
                        and params["taskID"] != None
                        and params["status"] != "Completed"
                    ):  # If this is a task (not a result) with an existent ID on CBRAIN, and hasn't yet run to completion

                        try:

                            jayson = cbrain_get_task_info_from_list(
                                task_list, params["taskID"]
                            )

                            if jayson["status"] == "Completed":
                                # Task completed, update status and get output file ID
                                data[file][pipeline_name][task_name_str][
                                    "status"
                                ] = jayson["status"]
                                # differentiate between one and many outputs
                                if "_cbrain_output_outputs" in jayson["params"]:
                                    data[file][pipeline_name][task_name_str][
                                        "outputID"
                                    ] = jayson["params"]["_cbrain_output_outputs"][0]
                                if "_cbrain_output_output" in jayson["params"]:
                                    data[file][pipeline_name][task_name_str][
                                        "outputID"
                                    ] = jayson["params"]["_cbrain_output_output"][0]
                                if "_cbrain_output_outfile" in jayson["params"]:
                                    data[file][pipeline_name][task_name_str][
                                        "outputID"
                                    ] = jayson["params"]["_cbrain_output_outfile"][0]
                                if "outfile_id" in jayson["params"]:
                                    data[file][pipeline_name][task_name_str][
                                        "outputID"
                                    ] = jayson["params"]["outfile_id"]

                            else:
                                # Task not completed, just update status
                                data[file][pipeline_name][task_name_str][
                                    "status"
                                ] = jayson["status"]

                        except Exception as e:
                            pass

        cache_file.seek(0)
        json.dump(data, cache_file, indent=2)
        cache_file.truncate()


def pipeline_manager(
    cbrain_token, experiment_definition, cbrain_ids, pipeline, dataset
):
    """Iterates over each component in a pipeline, organizes, and feeds the necessary data to the functions which post tasks on CBRAIN and update the caches."""

    component_number = (
        0  # Keeps track of the order of the component (we need to flag the first one)
    )

    for pipeline_component in experiment_definition["Pipelines"][pipeline][
        "Components"
    ]:

        with open(
            experiment_definition["Pipelines"][pipeline]["Components"][
                pipeline_component
            ]["Parameter_dictionary"],
            "r+",
        ) as param_file:  # Load parameters for current pipeline component
            parameter_dictionary = json.load(param_file)

            if component_number == 0:
                first_task_handler(
                    cbrain_token,
                    parameter_dictionary,
                    cbrain_ids["Tool_Config_IDs"][pipeline_component],
                    dataset + ".json",
                    pipeline_component,
                    pipeline,
                )
            else:
                nth_task_handler(
                    cbrain_token,
                    parameter_dictionary,
                    cbrain_ids["Tool_Config_IDs"][pipeline_component],
                    dataset + ".json",
                    pipeline_component,
                    previous_pipeline_component,
                    pipeline,
                )

            if (
                len(experiment_definition["Resubmit_tasks"]["taskIDs"]) > 0
            ):  # if there are any tasks to resubmit...
                task_resubmission_handler(
                    cbrain_token,
                    parameter_dictionary,
                    cbrain_ids["Tool_Config_IDs"][pipeline_component],
                    dataset + ".json",
                    pipeline_component,
                    pipeline,
                    experiment_definition["Resubmit_tasks"]["taskIDs"],
                )

        previous_pipeline_component = pipeline_component
        component_number = component_number + 1


def first_task_handler(
    cbrain_token,
    parameter_dictionary,
    tool_config_id,
    cache_file,
    pipeline_component,
    pipeline_name,
):
    """Handles the cache writing for the first task in a pipeline, and calls to post the task to CBRAIN."""

    with open(cache_file, "r+") as file:
        data = json.load(file)
        for filename in data:
            if data[filename][pipeline_name][pipeline_component]["isUsed"] == None:

                try:

                    userfile_id = data[filename][pipeline_name][pipeline_component][
                        "inputID"
                    ]
                    jayson = cbrain_post_task(
                        cbrain_token, userfile_id, tool_config_id, parameter_dictionary
                    )
                    data[filename][pipeline_name][pipeline_component][
                        "toolConfigID"
                    ] = jayson[0]["tool_config_id"]
                    data[filename][pipeline_name][pipeline_component][
                        "taskID"
                    ] = jayson[0]["id"]
                    data[filename][pipeline_name][pipeline_component][
                        "status"
                    ] = jayson[0]["status"]
                    data[filename][pipeline_name][pipeline_component]["isUsed"] = True

                except Exception as e:
                    pass

        file.seek(0)  # rewind
        json.dump(data, file, indent=2)
        file.truncate()


def nth_task_handler(
    cbrain_token,
    parameter_dictionary,
    tool_config_id,
    cache_file,
    pipeline_component,
    previous_pipeline_component,
    pipeline_name,
):
    """Handles the cache writing and task posting for any pipeline component except the first task."""

    with open(cache_file, "r+") as file:
        data = json.load(file)
        for filename in data:
            if (
                data[filename][pipeline_name][pipeline_component]["isUsed"] == None
                and data[filename][pipeline_name][previous_pipeline_component]["status"]
                == "Completed"
            ):

                try:

                    userfile_id = data[filename][pipeline_name][
                        previous_pipeline_component
                    ][
                        "outputID"
                    ]  # output of last task
                    jayson = cbrain_post_task(
                        cbrain_token, userfile_id, tool_config_id, parameter_dictionary
                    )
                    data[filename][pipeline_name][pipeline_component][
                        "inputID"
                    ] = userfile_id
                    data[filename][pipeline_name][pipeline_component][
                        "toolConfigID"
                    ] = jayson[0]["tool_config_id"]
                    data[filename][pipeline_name][pipeline_component][
                        "taskID"
                    ] = jayson[0]["id"]
                    data[filename][pipeline_name][pipeline_component][
                        "status"
                    ] = jayson[0]["status"]
                    data[filename][pipeline_name][pipeline_component]["isUsed"] = True

                except Exception as e:
                    pass

        file.seek(0)  # rewind
        json.dump(data, file, indent=2)
        file.truncate()


def task_resubmission_handler(
    cbrain_token,
    parameter_dictionary,
    tool_config_id,
    cache_file,
    pipeline_component,
    pipeline_name,
    rerun_ID_list,
):
    """Resubmits a task, and sets all subsequent pipeline component dependencies  to null in the cache."""

    with open(cache_file, "r+") as file:
        data = json.load(file)
        for filename in data:

            if "taskID" in data[filename][pipeline_name][pipeline_component]:

                if (
                    data[filename][pipeline_name][pipeline_component]["taskID"]
                    in rerun_ID_list
                ):

                    try:
                        userfile_id = data[filename][pipeline_name][pipeline_component][
                            "inputID"
                        ]
                        jayson = cbrain_post_task(
                            cbrain_token,
                            userfile_id,
                            tool_config_id,
                            parameter_dictionary,
                        )
                        data[filename][pipeline_name][pipeline_component][
                            "toolConfigID"
                        ] = jayson[0]["tool_config_id"]
                        data[filename][pipeline_name][pipeline_component][
                            "taskID"
                        ] = jayson[0]["id"]
                        data[filename][pipeline_name][pipeline_component][
                            "status"
                        ] = jayson[0]["status"]
                        data[filename][pipeline_name][pipeline_component][
                            "isUsed"
                        ] = True
                        print(
                            "Reposting "
                            + str(
                                data[filename][pipeline_name][pipeline_component][
                                    "taskID"
                                ]
                            )
                        )

                    except Exception as e:
                        pass

                    # The code section sets all the subsequent pipeline components following the reposted task to null
                    pipeline_length = len(
                        data[filename][pipeline_name].items()
                    )  # total number of components in pipeline
                    curr_index = list(data[filename][pipeline_name].keys()).index(
                        pipeline_component
                    )  # index of current (reposted) component

                    index_counter = 0
                    for component in data[filename][pipeline_name].items():

                        # if we are on a component after the one being submitted, and not the result
                        if (
                            index_counter > curr_index
                            and index_counter < pipeline_length - 1
                        ):
                            data[filename][pipeline_name][component[0]][
                                "inputID"
                            ] = None
                            data[filename][pipeline_name][component[0]][
                                "toolConfigID"
                            ] = None
                            data[filename][pipeline_name][component[0]]["taskID"] = None
                            data[filename][pipeline_name][component[0]]["status"] = None
                            data[filename][pipeline_name][component[0]][
                                "outputID"
                            ] = None
                            data[filename][pipeline_name][component[0]]["isUsed"] = None

                        # if we are on the result component of the pipeline
                        if (
                            index_counter > curr_index
                            and index_counter == pipeline_length - 1
                        ):
                            data[filename][pipeline_name][component[0]]["result"] = None
                            data[filename][pipeline_name][component[0]]["isUsed"] = None

                        index_counter += 1

        file.seek(0)  # rewind
        json.dump(data, file, indent=2)
        file.truncate()


def populate_results(cache_filename, cbrain_token):
    """Fetches the text from a file on CBRAIN and writes it to the cache.
	
	Originally this is designed to extract a hippocampal volume from an FSL Stats text output.
	"""

    with open(cache_filename, "r+") as cache_file:
        data = json.load(cache_file)
        for (file, pipeline) in data.items():
            for (pipeline_name, pipeline_component) in pipeline.items():
                previous_string = None
                for (pipeline_component_str, params) in pipeline_component.items():

                    if (
                        pipeline_component_str == "Result"
                    ):  # Find the task before the result in the json

                        if (
                            data[file][pipeline_name]["Result"]["isUsed"] == None
                            and data[file][pipeline_name][previous_string]["status"]
                            == "Completed"
                        ):

                            fileID = data[file][pipeline_name][previous_string][
                                "outputID"
                            ]
                            print("Streaming text for fileID: " + str(fileID))
                            cbrain_sync_file(str(fileID), cbrain_token)

                            try:

                                # Note that result population is hardcoded, as the pipelines all produce different outputs that need different parsing procedures.
                                if pipeline_name == "FSL":
                                    vol_string = cbrain_download_text(
                                        fileID, cbrain_token
                                    )
                                    vol = vol_string.split()[0]  # get first word

                                if pipeline_name == "FreeSurfer":
                                    asegstats_string = cbrain_download_text(
                                        fileID, cbrain_token
                                    )
                                    vol = retrieve_FreeSurfer_volume(
                                        asegstats_string, "Left-Hippocampus"
                                    )

                                data[file][pipeline_name]["Result"]["result"] = vol
                                data[file][pipeline_name]["Result"]["isUsed"] = True

                            except Exception as e:
                                pass

                    previous_string = pipeline_component_str

        cache_file.seek(0)  # rewind
        json.dump(data, cache_file, indent=2)
        cache_file.truncate()


def retrieve_FreeSurfer_volume(asegstats_string, structName):
    # Take as input the aseg.stats file from the freesurfer output as a string, and the StructName field.
    lines = asegstats_string.splitlines()
    reader = csv.reader(lines, delimiter=" ")
    for row in reader:
        if structName in row:
            index = row.index(structName)
            return row[
                index - 2
            ]  # Returns the word which is two before the name of the structure.
