import re
import azure.functions as func
import json
import logging
import os
import requests
import time
from datetime import datetime, timedelta, timezone
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, AzureCliCredential, ChainedTokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.storage.queue import QueueClient, BinaryBase64EncodePolicy, BinaryBase64DecodePolicy

app = func.FunctionApp()
session = requests.Session()

# Name of the queues to get and send the function call messages
input_queue_name = "input"
output_queue_name = "output"

ADO_API_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"

credential = ChainedTokenCredential(
    AzureCliCredential(),
    DefaultAzureCredential()
)


def get_days_count_from_current_date(date_string="2022-06-21T17:42:07Z"):
    if date_string:
        # Get todays date
        todaysDate = ""
        build_date = datetime.strptime(date_string.split("T")[0], "%Y-%m-%d").date()
        return (datetime.today().date() - build_date).days
    else:
        logging.info("Date string not defined")
        return -1

def get_difference_in_days(date_string1="2022-06-21T17:42:07Z", date_string2="2022-07-21T17:42:07Z"):
    if date_string1 and date_string2:
        build_date1 = datetime.strptime(date_string1.split("T")[0], "%Y-%m-%d").date()
        build_date2 = datetime.strptime(date_string2.split("T")[0], "%Y-%m-%d").date()
        return (build_date2 - build_date1).days + 1
    else:
        logging.info("Date string not defined")
        return None
    
# Azure DevOps Configuration
DEVOPS_API_URL = "{}/{}/_apis/pipelines?api-version=6.0-preview.1"
ORG = "{ORG}"  # Replace with your Azure DevOps organization name
HARDCODEDE = "https://dev.azure.com/{ORG}//_apis/pipelines?api-version=6.0-preview.1"  # Replace with your Azure DevOps project name

# Function to initialize the agent client and the tools Azure Functions that the agent can use
def initialize_client():
    # Create a project client using the connection string from local.settings.json
    project_client = AIProjectClient.from_connection_string(
        credential=DefaultAzureCredential(),
        conn_str=os.environ["PROJECT_CONNECTION_STRING"]
    )

    # Get the connection string from local.settings.json
    storage_connection_string = os.environ["STORAGE_CONNECTION__queueServiceUri"]

    # Create an agent with the Azure Function tool to get the weather
    agent = project_client.agents.create_agent(
        model="gpt-4o",
        name="azure-function-agent-get-failing-pipelines-or-enable-disables-a-pipeline",
        instructions="You are a helpful support agent with 3 capabilities. Finding failing pipelines in a project, Disabling a pipeline with given id and enabling a pipelline with given id. You need to invoke the correct function depending on the prompt.eg For finding failing pipelines invoke 'GetFailingPipelinesInfo'.For enabling a pipeline with id and project invoke 'EnablePipeline' For disabling a pipeline with id and project invoke 'DisablePipeline'.",
        description="An agent that can find faling pipelines in last 24 hours or enables or disables a pipeline as per user requests.",
        headers={"x-ms-enable-preview": "true"},
        tools = [
            {
                "type": "azure_function",
                "azure_function": {
                    "function": {
                        "name": "GetFailingPipelinesInfo",
                        "description": "Get the failing pipelines in the last 24 hours.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "project": {"type": "string", "description": "The project name."}
                            },
                            "required": ["project"]
                        }
                    },
                    "input_binding": {
                        "type": "storage_queue",
                        "storage_queue": {
                            "queue_service_uri": storage_connection_string,
                            "queue_name": f"{input_queue_name}"
                        }
                    },
                    "output_binding": {
                        "type": "storage_queue",
                        "storage_queue": {
                            "queue_service_uri": storage_connection_string,
                            "queue_name": f"{output_queue_name}"
                        }
                    }
                }
            },
            {
                "type": "azure_function",
                "azure_function": {
                    "function": {            
                        "name": "DisablePipeline",
                        "description": "Disables a pipeline if project and pipelineId is provided.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "project": {"type": "string", "description": "The project name."},
                                "pipelineId": {"type": "string", "description": "The pipeline ID."}
                            },
                            "required": ["project", "pipelineId"]
                        }
                    },
                    "input_binding": {
                        "type": "storage_queue",
                        "storage_queue": {
                            "queue_service_uri": storage_connection_string,
                            "queue_name": "inputd"
                        }
                    },
                    "output_binding": {
                        "type": "storage_queue",
                        "storage_queue": {
                            "queue_service_uri": storage_connection_string,
                            "queue_name": "outputd"
                        }
                    }
                }
            },
            {
                "type": "azure_function",
                "azure_function": {
                    "function": {
                        "name": "EnablePipeline",
                        "description": "Enables a pipeline if project and pipelineId is provided.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "project": {"type": "string", "description": "The project name."},
                                "pipelineId": {"type": "string", "description": "The pipeline ID."}
                            },
                            "required": ["project", "pipelineId"]
                        }
                    },
                    "input_binding": {
                        "type": "storage_queue",
                        "storage_queue": {
                            "queue_service_uri": storage_connection_string,
                            "queue_name": "inpute"
                        }
                    },
                    "output_binding": {
                        "type": "storage_queue",
                        "storage_queue": {
                            "queue_service_uri": storage_connection_string,
                            "queue_name": "outpute"
                        }
                    }
                }
            }
        ]

    )
    logging.info(f"======Created agent, agent ID: {agent.id}")

    # Create a thread
    thread = project_client.agents.create_thread()
    logging.info(f"======Created thread, thread ID: {thread.id}")

    return project_client, thread, agent

@app.route(route="prompt", auth_level=func.AuthLevel.FUNCTION)
def prompt(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    # Get the prompt from the request body
    req_body = req.get_json()
    prompt = req_body.get('Prompt')

    # Initialize the agent client
    project_client, thread, agent = initialize_client()

    # Send the prompt to the agent
    message = project_client.agents.create_message(
        thread_id=thread.id,
        role="user",
        content=prompt,
    )
    logging.info(f"======Created message, message ID: {message.id}")

    # Run the agent
    run = project_client.agents.create_run(thread_id=thread.id, assistant_id=agent.id)
    # Monitor and process the run status
    while run.status in ["queued", "in_progress", "requires_action"]:
        time.sleep(1)
        run = project_client.agents.get_run(thread_id=thread.id, run_id=run.id)

        if run.status not in ["queued", "in_progress", "requires_action"]:
            break

    logging.info(f"======Run finished with status: {run.status}")

    if run.status == "failed":
        logging.error(f"======Run failed: {run.last_error}")

    # Get messages from the assistant thread
    messages = project_client.agents.get_messages(thread_id=thread.id)
    logging.info(f"======Messages: {messages}")

    # Get the last message from the assistant
    last_msg = messages.get_last_text_message_by_sender("assistant")
    if last_msg:
        logging.info(f"======Last Message: {last_msg.text.value}")

    # Delete the agent once done
    project_client.agents.delete_agent(agent.id)
    logging.info("Deleted agent")

    return func.HttpResponse(last_msg.text.value)


@app.function_name(name="EnablePipeline")
@app.queue_trigger(arg_name="msg", queue_name="inpute", connection="STORAGE_CONNECTION")
def process_queue_message(msg: func.QueueMessage) -> None:
    logging.info('Python queue trigger function processed a queue item')

    # Queue to send message to
    queue_client = QueueClient(
        os.environ["STORAGE_CONNECTION__queueServiceUri"],
        queue_name="outpute",
        credential=DefaultAzureCredential(),
        message_encode_policy=BinaryBase64EncodePolicy(),
        message_decode_policy=BinaryBase64DecodePolicy()
    )

    messagepayload = json.loads(msg.get_body().decode('utf-8'))
    project = messagepayload['project']
    pipelineId = messagepayload['pipelineId']
    logging.info(f"======Project: {project}, PipelineId: {pipelineId}")
    correlation_id = messagepayload['CorrelationId']

    result  = enable_disable_pipeline(project, pipelineId, "enable")
    # Send message to queue. Sends a mock message for the weather
    result_message = {
        'Value': str(result),
        'CorrelationId': correlation_id
    }
    queue_client.send_message(json.dumps(result_message).encode('utf-8'))

    logging.info(f"======Sent message to queue: outpute with message {result_message}")

# Function to find the pipeline
@app.function_name(name="DisablePipeline")
@app.queue_trigger(arg_name="msg", queue_name="inputd", connection="STORAGE_CONNECTION")
def process_queue_message(msg: func.QueueMessage) -> None:
    logging.info('Python queue trigger function processed a queue item')

    # Queue to send message to
    queue_client = QueueClient(
        os.environ["STORAGE_CONNECTION__queueServiceUri"],
        queue_name="outputd",
        credential=DefaultAzureCredential(),
        message_encode_policy=BinaryBase64EncodePolicy(),
        message_decode_policy=BinaryBase64DecodePolicy()
    )

    messagepayload = json.loads(msg.get_body().decode('utf-8'))
    project = messagepayload['project']
    pipelineId = messagepayload['pipelineId']

    logging.info(f"======Project: {project}, PipelineId: {pipelineId}")
    correlation_id = messagepayload['CorrelationId']

    result  = enable_disable_pipeline(project, pipelineId, "disable")
    # Send message to queue. Sends a mock message for the weather
    result_message = {
        'Value': str(result),
        'CorrelationId': correlation_id
    }
    queue_client.send_message(json.dumps(result_message).encode('utf-8'))

    logging.info(f"======Sent message to queue: outputd with message {result_message}")

# Function to get failing pipelines in the last 24 hours
@app.function_name(name="GetFailingPipelinesInfo")
@app.queue_trigger(arg_name="msg", queue_name="input", connection="STORAGE_CONNECTION")
def process_queue_message(msg: func.QueueMessage) -> None:
    logging.info('Python queue trigger function processed a queue item')

    # Queue to send message to
    queue_client = QueueClient(
        os.environ["STORAGE_CONNECTION__queueServiceUri"],
        queue_name="output",
        credential=DefaultAzureCredential(),
        message_encode_policy=BinaryBase64EncodePolicy(),
        message_decode_policy=BinaryBase64DecodePolicy()
    )

    messagepayload = json.loads(msg.get_body().decode('utf-8'))
    project = messagepayload['project']
    branch_patterns = messagepayload.get('branchPatterns', r'refs/heads/(release|product|master|main|develop|osmain)')
    days_to_lookback = int(messagepayload.get('daysToLookback', 7))
    max_builds = int(messagepayload.get('maxBuilds', 100))
    correlation_id = messagepayload['CorrelationId']

    logging.info(f"======Project: {project}, BranchPatterns: {branch_patterns}, DaysToLookback: {days_to_lookback}, MaxBuilds: {max_builds}")

    # Get the failing pipelines
    failing_pipelines = get_failing_pipelines(project, branch_patterns, days_to_lookback, max_builds)

    # Send message to queue
    result_message = {
        'Value': 'Failing pipelines: ' + str(failing_pipelines),
        'CorrelationId': correlation_id
    }
    queue_client.send_message(json.dumps(result_message).encode('utf-8'))
    logging.info(f"======Sent message to queue with message {result_message}")



def get_failing_pipelines(project: str, branch_patterns: str, days_to_lookback: int, max_builds: int = 100):
    """Get failing pipelines in the specified time frame using Azure DevOps Builds API."""
    try:
        # Calculate the start time (timezone-aware)
        start_time = (datetime.now(timezone.utc) - timedelta(days=days_to_lookback)).isoformat()
        logging.info(f"Start time for fetching builds: {start_time}")

        # Fetch builds using the Builds API
        builds_url = f"https://dev.azure.com/{ORG}/{project}/_apis/build/builds?api-version=7.1&startTime={start_time}&maxBuilds={max_builds}"
        try:
            response = session.get(builds_url, headers=get_auth_headers())
            response.raise_for_status()
            builds = response.json().get('value', [])
        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching builds: {e}")
            return []

        if not builds:
            logging.info(f"No builds found for project '{project}' in the last {days_to_lookback} days.")
            return []

        failing_pipelines = []
        pipeline_failure_count = {}  # Keep track of failures per pipeline
        logging.info(f"Processing {len(builds)} builds for project: {project}")

        for build in builds:
            # Check if the build failed
            if build.get('result') == 'failed':
                build_id = build.get('id')
                pipeline_name = build.get('definition', {}).get('name', 'Unknown')
                
                # Skip if we already have 2 failures for this pipeline
                if pipeline_failure_count.get(pipeline_name, 0) >= 2:
                    continue
                    
                branch_name = build.get('sourceBranch', '')
                finish_time = build.get('finishTime')

                if not finish_time:
                    logging.warning(f"Build missing 'finishTime': {build}")
                    continue

                # Use truncate_fractional_seconds to handle timestamps
                try:
                    finish_time = truncate_fractional_seconds(finish_time)
                except ValueError as e:
                    logging.error(f"Error parsing finish_time: {finish_time}, Error: {e}")
                    continue

                # Compare the finish time with the start time
                if finish_time >= datetime.fromisoformat(start_time):
                    # Compare the branch name with the branch patterns
                    if re.search(branch_patterns, branch_name):
                        # Fetch failure details for the build
                        failure_details = get_build_failure_details(project, build_id)

                        # Append failure details to the pipeline information
                        failing_pipelines.append({
                            "result": build['result'],
                            "name": pipeline_name,
                            "last_run": finish_time.isoformat(),
                            "build_id": build_id,
                            "build_url": build.get('_links', {}).get('web', {}).get('href', 'N/A'),
                            "source_branch": branch_name,
                            "failure_details": failure_details  # Include failure details
                        })
                        
                        # Increment the failure count for this pipeline
                        pipeline_failure_count[pipeline_name] = pipeline_failure_count.get(pipeline_name, 0) + 1
                        logging.info(f"Build {build_id} failed on branch {branch_name} at {finish_time.isoformat()}")

        if not failing_pipelines:
            logging.info(f"No failing pipelines found for project: {project}")
        return failing_pipelines[:10]  # Limit to the first 10 failing pipelines

    except Exception as e:
        logging.error(f"Unexpected error in get_failing_pipelines: {e}")
        return []

def truncate_fractional_seconds(run_time):
    run_time = run_time[:26] + 'Z'  # Keep only the first 6 digits of the fraction
    run_time = run_time.replace('ZZ', 'Z')  # Remove the 'Z' to avoid parsing issues
    run_time = datetime.strptime(run_time, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
    return run_time

def get_pipelines(project: str):
    """Get pipelines from Azure DevOps."""
    try:
        # Get the response from the Azure DevOps API
        url = f"https://dev.azure.com/{ORG}/{project}/_apis/pipelines?api-version=6.0"
        response = session.get(url, headers=get_auth_headers())
        response.raise_for_status()  # Raise an exception for HTTP errors
        return response.json().get('value', [])
    except requests.exceptions.RequestException as e:
        logging.error(f"======Error fetching pipelines: {e}")
        return []

def get_pipeline(project: str, pipelineId: str):
    """Get pipeline from Azure DevOps."""
    try:
        # Get the response from the Azure DevOps API
        url = f"https://dev.azure.com/{ORG}/{project}/_apis/pipelines/{pipelineId}?api-version=6.0"
        response = session.get(url, headers=get_auth_headers())
        logging.info(f"======project {project}, pipelineId {pipelineId}")
        response.raise_for_status()  # Raise an exception for HTTP errors
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"======Error fetching pipeline: {e}")
        return []

# Function to enable disable a pipeline
def enable_disable_pipeline(project: str, pipelineId: str, shouldEnable: str):
    """Enable or disable pipeline"""
    try:
        
        base_url = f"https://dev.azure.com/{ORG}/{project}/_apis/build/definitions/{pipelineId}?api-version=6.0"
        
        # Fetch the current build definition
        response = session.get(base_url, headers=get_auth_headers())
        
        if response.status_code != 200:
            logging.error(f"======Failed to fetch pipeline: {response.status_code}, {response.text}")
            response.raise_for_status()
            return
        
        definition = response.json()
        
        # Modify the queueStatus to disable
        if(shouldEnable == "disable"):
            definition["queueStatus"] = "disabled"
        else:
            definition["queueStatus"] = "enabled"
        
        # Send the updated definition back
        update_response = session.put(base_url, headers=get_auth_headers(), json=definition)
        response.raise_for_status()

        return f"Pipeline {pipelineId} {shouldEnable}d successfully."

    except requests.exceptions.RequestException as e:
        logging.error(f"======Error creating work item: {e}")

def get_build_failure_details(project: str, build_id: str):
    """
    Extract build failure information such as job failure details, job failure URLs, etc.
    using the Azure DevOps Builds API and Timeline API.
    """
    try:
        # Timeline API URL
        timeline_url = f"https://dev.azure.com/{ORG}/{project}/_apis/build/builds/{build_id}/timeline?api-version=7.1"

        # Fetch timeline details
        try:
            response = session.get(timeline_url, headers=get_auth_headers())
            response.raise_for_status()
            timeline_details = response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching timeline details for build ID {build_id}: {e}")
            return None

        # Extract failed tasks and jobs
        failed_tasks = [
            record for record in timeline_details.get('records', [])
            if record.get('type') == 'Task' and record.get('result') == 'failed'
        ]
        failed_jobs = [
            record for record in timeline_details.get('records', [])
            if record.get('type') == 'Job' and record.get('result') == 'failed'
        ]

        # Helper function to find the ancestor stage
        def get_ancestor_stage(record, all_records):
            current = record
            while current and current.get('parentId'):
                parent = next((r for r in all_records if r.get('id') == current.get('parentId')), None)
                if parent and parent.get('type') == 'Stage':
                    return parent
                current = parent
            return None

        # Initialize variables
        message = ""
        first_failed_job_with_message = None

        # Find the first failed task where the parent job and grandparent stage have also failed
        for failed_task in failed_tasks:
            failed_parent_job = next(
                (job for job in failed_jobs if job.get('id') == failed_task.get('parentId')), None
            )
            if not failed_parent_job:
                continue

            failed_parent_stage = get_ancestor_stage(failed_parent_job, timeline_details.get('records', []))
            if not failed_parent_stage or failed_parent_stage.get('result') != 'failed':
                continue

            # Found a task with failed job and failed stage
            if failed_task.get('issues'):
                message = f"Failed Task: {failed_task.get('name')}\n"
                for issue in failed_task.get('issues')[:10]:  # Limit to the first 10 issues
                    message += f" - [{issue.get('type')}] {issue.get('message')}\n"

            first_failed_job_with_message = failed_parent_job
            break  # Only need the first one

        # Extract job details
        job_name = first_failed_job_with_message.get('name') if first_failed_job_with_message else ""
        job_start_time = first_failed_job_with_message.get('startTime') if first_failed_job_with_message else ""
        job_logs_url = (
            first_failed_job_with_message.get('log', {}).get('url') if first_failed_job_with_message else ""
        )
        job_result = first_failed_job_with_message.get('result') if first_failed_job_with_message else ""

        # Build failure details
        failure_details = {
            "build_id": build_id,
            "job_name": job_name,
            "job_start_time": job_start_time,
            "job_logs_url": job_logs_url,
            "job_result": job_result,
            "job_message": message,
        }

        logging.info(f"Failure details for build ID {build_id}: {failure_details}")
        return failure_details

    except Exception as e:
        logging.error(f"Unexpected error in get_build_failure_details for build ID {build_id}: {e}")
        return None


def get_auth_headers():
    """Return Authorization header for Azure DevOps API."""
    token = credential.get_token(ADO_API_SCOPE)
    return {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json"
    }

def get_contributors_group_descriptor(project_id):
    """Get the descriptor for the Contributors group of a project."""
    try:
        # First, get the project descriptor
        project_desc_url = f"https://vssps.dev.azure.com/{ORG}/_apis/graph/descriptors/{project_id}?api-version=7.0"
        project_desc_resp = requests.get(project_desc_url, headers=get_auth_headers())
        project_desc_resp.raise_for_status()
        
        project_descriptor = project_desc_resp.json().get('value')
        if not project_descriptor:
            raise Exception(f"Could not find descriptor for project ID: {project_id}")

        # Then get the groups using the project descriptor
        groups_url = f"https://vssps.dev.azure.com/{ORG}/_apis/graph/groups?scopeDescriptor={project_descriptor}&api-version=7.0"
        groups_resp = requests.get(groups_url, headers=get_auth_headers())
        groups_resp.raise_for_status()
        
        # Find the Contributors group
        for group in groups_resp.json().get('value', []):
            if group.get('displayName') == 'Contributors':
                logging.info(f"Found Contributors group descriptor: {group.get('descriptor')}")
                return group.get('descriptor')
                
        raise Exception("Contributors group not found in project")
        
    except requests.exceptions.RequestException as e:
        error_message = f"Error getting contributors group: {str(e)}"
        logging.error(error_message)
        if hasattr(e.response, 'text'):
            logging.error(f"Response content: {e.response.text}")
        raise Exception(error_message)

def add_contributor_permission(email: str, project_name: str = '', repo_name: str = ''):
    """
    Add contributor permissions for a user to a specific project and repository
    """
    try:
        logging.info(f"Adding contributor permissions for {email} to {project_name}/{repo_name}")
        
        # Get project ID with error handling
        try:
            project_id = get_project_id(project_name)
            logging.info(f"Project ID: {project_id}")
        except Exception as e:
            logging.error(f"Failed to get project ID: {str(e)}")
            return {"status": "error", "message": f"Failed to get project ID: {str(e)}"}

        # Get user descriptor with error handling
        try:
            user_desc = get_user_descriptor(email)
            logging.info(f"User descriptor: {user_desc}")
        except Exception as e:
            logging.error(f"Failed to get user descriptor: {str(e)}")
            return {"status": "error", "message": f"Failed to get user descriptor: {str(e)}"}

        # Get contributors group descriptor with error handling
        try:
            group_desc = get_contributors_group_descriptor(project_id)
            logging.info(f"Contributors group descriptor: {group_desc}")
        except Exception as e:
            logging.error(f"Failed to get contributors group descriptor: {str(e)}")
            return {"status": "error", "message": f"Failed to get contributors group descriptor: {str(e)}"}

        # Add user to project contributors group
        try:
            add_user_to_project(user_desc, group_desc)
        except Exception as e:
            logging.error(f"Failed to add user to project: {str(e)}")
            return {"status": "error", "message": f"Failed to add user to project: {str(e)}"}

        # Set repository permissions
        try:
            set_repo_permission(user_desc, project_id, repo_name)
        except Exception as e:
            logging.error(f"Failed to set repository permissions: {str(e)}")
            return {"status": "error", "message": f"Failed to set repository permissions: {str(e)}"}

        return {
            "status": "success",
            "message": f"Successfully added contributor permissions for {email} to {project_name}/{repo_name}"
        }
        
    except Exception as e:
        error_message = f"Error adding contributor permissions: {str(e)}"
        logging.error(error_message)
        return {
            "status": "error",
            "message": error_message
        }
# Update the helper functions to use get_auth_headers() instead of headers
def get_project_id(project: str):
    url = f"https://dev.azure.com/{ORG}/_apis/projects/{project}?api-version=7.1-preview.1"
    resp = requests.get(url, headers=get_auth_headers())
    resp.raise_for_status()
    return resp.json()["id"]
def get_contributors_group_descriptor(project_id):
    """Get the descriptor for the Contributors group of a project."""
    try:
        # First, get the project descriptor using preview API version
        project_desc_url = f"https://vssps.dev.azure.com/{ORG}/_apis/graph/descriptors/{project_id}?api-version=7.0-preview.1"
        project_desc_resp = requests.get(project_desc_url, headers=get_auth_headers())
        project_desc_resp.raise_for_status()
        
        project_descriptor = project_desc_resp.json().get('value')
        if not project_descriptor:
            raise Exception(f"Could not find descriptor for project ID: {project_id}")
        
        logging.info(f"Project descriptor: {project_descriptor}")

        # Then get the groups using the project descriptor with preview API version
        groups_url = f"https://vssps.dev.azure.com/{ORG}/_apis/graph/groups?scopeDescriptor={project_descriptor}&api-version=7.0-preview.1"
        logging.info(f"Fetching groups from URL: {groups_url}")
        
        groups_resp = requests.get(groups_url, headers=get_auth_headers())
        groups_resp.raise_for_status()
        
        groups_data = groups_resp.json()
        logging.info(f"Groups response: {groups_data}")
        
        # Find the Contributors group
        for group in groups_data.get('value', []):
            if group.get('displayName') == 'Contributors':
                group_descriptor = group.get('descriptor')
                logging.info(f"Found Contributors group descriptor: {group_descriptor}")
                return group_descriptor
                
        raise Exception("Contributors group not found in project")
        
    except requests.exceptions.RequestException as e:
        error_message = f"Error getting contributors group: {str(e)}"
        logging.error(error_message)
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Response status code: {e.response.status_code}")
            logging.error(f"Response content: {e.response.text}")
        raise Exception(error_message)
    except Exception as e:
        error_message = f"Unexpected error getting contributors group: {str(e)}"
        logging.error(error_message)
        raise Exception(error_message)

def get_user_descriptor(email):
    url = f"https://vssps.dev.azure.com/{ORG}/_apis/graph/users?filterValue={email}&api-version=7.1-preview.1"
    resp = requests.get(url, headers=get_auth_headers())
    resp.raise_for_status()
    users = resp.json()['value']
    if not users:
        raise Exception(f"No user found with email: {email}")
    return users[0]["descriptor"]

def add_user_to_project(user_desc, group_desc):
    url = f"https://vssps.dev.azure.com/{ORG}/_apis/graph/memberships/{user_desc}/{group_desc}?api-version=7.1-preview.1"
    resp = requests.put(url, headers=get_auth_headers())
    if resp.status_code in [200, 204]:
        logging.info(f"Added user to Contributors group in project.")
    elif resp.status_code == 409:
        logging.info(f"User is already in group.")
    else:
        resp.raise_for_status()

def set_repo_permission(user_descriptor, project_id, repo_name):
    # Get the repo ID
    url = f"https://dev.azure.com/{ORG}/{project_id}/_apis/git/repositories/{repo_name}?api-version=7.1-preview.1"
    resp = requests.get(url, headers=get_auth_headers())
    resp.raise_for_status()
    repo_id = resp.json()["id"]

    # Security namespace for Git repos
    security_namespace_id = "52d39943-cb85-4d7f-8fa8-c6baac873819"
    token = f"repoV2/{project_id}/{repo_id}"

    # Allow Read/Contribute permissions (bitmask)
    permissions_to_set = {
        "allow": 15,
        "deny": 0
    }

    ace_url = f"https://dev.azure.com/{ORG}/_apis/accesscontrolentries/{security_namespace_id}?api-version=7.1-preview.1"
    ace_payload = {
        "token": token,
        "merge": True,
        "accessControlEntries": [
            {
                "descriptor": user_descriptor,
                "allow": permissions_to_set["allow"],
                "deny": permissions_to_set["deny"]
            }
        ]
    }

    ace_resp = requests.post(ace_url, headers=get_auth_headers(), json=ace_payload)
    ace_resp.raise_for_status()
    logging.info("Repository permissions set.")
