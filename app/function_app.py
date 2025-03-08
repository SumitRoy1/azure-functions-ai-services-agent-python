import azure.functions as func
import json
import logging
import os
import requests
import time
from datetime import datetime, timedelta
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.queue import QueueClient, BinaryBase64EncodePolicy, BinaryBase64DecodePolicy

app = func.FunctionApp()
session = requests.Session()

# Name of the queues to get and send the function call messages
input_queue_name = "input"
output_queue_name = "output"

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
ORG = "your_organization"  # Replace with your Azure DevOps organization name
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
        instructions="You are a helpful support agent with 3 capabilities. Finding failing pipelines in a project, Disabling a pipeline with given id and enabling a pipelline with given id. You need to invoke the correct function depending on the prompt.eg For finding failing pipelines invoke 'GetFailingPipelinesInLast24Hours'.For enabling a pipeline with id and project invoke 'EnablePipeline' For disabling a pipeline with id and project invoke 'DisablePipeline'.",
        description="An agent that can find faling pipelines in last 24 hours or enables or disables a pipeline as per user requests.",
        headers={"x-ms-enable-preview": "true"},
        tools = [
            {
                "type": "azure_function",
                "azure_function": {
                    "function": {
                        "name": "GetFailingPipelinesInLast24Hours",
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
    logging.info(f"Created agent, agent ID: {agent.id}")

    # Create a thread
    thread = project_client.agents.create_thread()
    logging.info(f"Created thread, thread ID: {thread.id}")

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
    logging.info(f"Created message, message ID: {message.id}")

    # Run the agent
    run = project_client.agents.create_run(thread_id=thread.id, assistant_id=agent.id)
    # Monitor and process the run status
    while run.status in ["queued", "in_progress", "requires_action"]:
        time.sleep(1)
        run = project_client.agents.get_run(thread_id=thread.id, run_id=run.id)

        if run.status not in ["queued", "in_progress", "requires_action"]:
            break

    logging.info(f"Run finished with status: {run.status}")

    if run.status == "failed":
        logging.error(f"Run failed: {run.last_error}")

    # Get messages from the assistant thread
    messages = project_client.agents.get_messages(thread_id=thread.id)
    logging.info(f"Messages: {messages}")

    # Get the last message from the assistant
    last_msg = messages.get_last_text_message_by_sender("assistant")
    if last_msg:
        logging.info(f"Last Message: {last_msg.text.value}")

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
    logging.info(f"Project: {project}, PipelineId: {pipelineId}")
    correlation_id = messagepayload['CorrelationId']

    result  = enable_disable_pipeline(project, pipelineId, "enable")
    # Send message to queue. Sends a mock message for the weather
    result_message = {
        'Value': str(result),
        'CorrelationId': correlation_id
    }
    queue_client.send_message(json.dumps(result_message).encode('utf-8'))

    logging.info(f"Sent message to queue: outpute with message {result_message}")

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

    logging.info(f"Project: {project}, PipelineId: {pipelineId}")
    correlation_id = messagepayload['CorrelationId']

    result  = enable_disable_pipeline(project, pipelineId, "disable")
    # Send message to queue. Sends a mock message for the weather
    result_message = {
        'Value': str(result),
        'CorrelationId': correlation_id
    }
    queue_client.send_message(json.dumps(result_message).encode('utf-8'))

    logging.info(f"Sent message to queue: outputd with message {result_message}")

# Function to get failing pipelines in the last 24 hours
@app.function_name(name="GetFailingPipelinesInLast24Hours")
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
    correlation_id = messagepayload['CorrelationId']
    logging.info(f"Project: {project}")

    # Get the failing pipelines
    failing_pipelines = get_failing_pipelines(project)

    # Send message to queue. Sends a mock message for the weather
    result_message = {
        'Value': 'Failing pipelines in the last 24 hours: ' + str(failing_pipelines),
        'CorrelationId': correlation_id
    }
    queue_client.send_message(json.dumps(result_message).encode('utf-8'))
    logging.info(f"Sent message to queue with message {result_message}")

def get_failing_pipelines(project: str):
    """Get failing pipelines in the last 24 hours using Azure DevOps REST API."""
    # Get the list of pipelines
    """Get pipeline runs to check their status."""
    try:
        # Get the response from the Azure DevOps API
        # Create a session object
        pipelines = get_pipelines(project)
        failing_pipelines = []
        logging.info(f"Number of pipelines: {len(pipelines)}")

        for pipeline in pipelines[-100:]:
            pipelineIndex = pipeline['id']

            pipelineinfo = get_pipeline(project, pipelineIndex)

            if 'configuration' in pipelineinfo and 'designerJson' in pipelineinfo['configuration'] and 'queueStatus' in pipelineinfo['configuration']['designerJson']:
                isPipelineEnabled = pipelineinfo['configuration']['designerJson']['queueStatus']
                if(isPipelineEnabled == 'disabled'):
                    continue
                    
            runs_url = f"https://dev.azure.com/{ORG}/{project}/_apis/pipelines/{pipelineIndex}/runs?api-version=6.0-preview.1"

            response = session.get(runs_url, headers=get_auth_headers())
            runs = response.json().get('value', [])

            for run in runs[0:15]:
                # Check if the run was completed and failed in the last 24 hours
                if run['state'] == 'completed':
                    run_time = get_days_count_from_current_date(run['finishedDate'])
                    if run_time <= 2 and run['result'] == 'failed':
                        failing_pipelines.append({
                            "result": run['result'],
                            "name": pipeline['name'],
                            "last_run": run['finishedDate'],
                            "build_id": run['id'],
                            "build_url" : run['url']
                        })
                        logging.info(f"Pipeline {pipeline['name']} failed on {run['finishedDate']}, buildid = {run['id']}")
        response = session.get(runs_url, headers=get_auth_headers())
        response.raise_for_status()  # Raise an exception for HTTP errors
        return failing_pipelines
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching pipeline runs: {e}")
        return []

def get_pipelines(project: str):
    """Get pipelines from Azure DevOps."""
    try:
        # Get the response from the Azure DevOps API
        url = f"https://dev.azure.com/{ORG}/{project}/_apis/pipelines?api-version=6.0"
        response = session.get(url, headers=get_auth_headers())
        response.raise_for_status()  # Raise an exception for HTTP errors
        return response.json().get('value', [])
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching pipelines: {e}")
        return []

def get_pipeline(project: str, pipelineId: str):
    """Get pipeline from Azure DevOps."""
    try:
        # Get the response from the Azure DevOps API
        url = f"https://dev.azure.com/{ORG}/{project}/_apis/pipelines/{pipelineId}?api-version=6.0"
        response = session.get(url, headers=get_auth_headers())
        logging.info(f"project {project}, pipelineId {pipelineId}")
        response.raise_for_status()  # Raise an exception for HTTP errors
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching pipeline: {e}")
        return []

# Function to enable disable a pipeline
def enable_disable_pipeline(project: str, pipelineId: str, shouldEnable: str):
    """Enable or disable pipeline"""
    try:
        
        base_url = f"https://dev.azure.com/{ORG}/{project}/_apis/build/definitions/{pipelineId}?api-version=6.0"
        
        # Fetch the current build definition
        response = session.get(base_url, headers=get_auth_headers())
        
        if response.status_code != 200:
            logging.error(f"Failed to fetch pipeline: {response.status_code}, {response.text}")
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
        logging.error(f"Error creating work item: {e}")

def get_auth_headers():
    """Generate authentication headers using Managed Identity."""
    # Get the access token using DefaultAzureCredential (Managed Identity)
    credential = DefaultAzureCredential()
    ADO_API_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"
    token = credential.get_token(ADO_API_SCOPE)

    # Return the Authorization header with the token
    return {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json"
    }
