import copy
import datetime as dt
import json
import os
import pathlib
import shutil
from typing import Dict, Any, List, Optional

import click
import git
import mlflow
import pkg_resources
import requests
from databricks_cli.configure.config import _get_api_client
from databricks_cli.configure.provider import DEFAULT_SECTION, ProfileConfigProvider, EnvironmentVariableConfigProvider
from databricks_cli.dbfs.api import DbfsService
from databricks_cli.sdk.api_client import ApiClient
from databricks_cli.workspace.api import WorkspaceService
from path import Path
from retry import retry
from setuptools import sandbox

DBX_PATH = ".dbx"
INFO_FILE_PATH = "%s/project.json" % DBX_PATH
LOCK_FILE_PATH = "%s/lock.json" % DBX_PATH
DATABRICKS_MLFLOW_URI = "databricks"
DEPLOYMENT_TEMPLATE_PATH = pkg_resources.resource_filename('dbx', 'template/deployment.json')
CONF_PATH = "conf"
DEFAULT_DEPLOYMENT_FILE_PATH = "%s/deployment.json" % CONF_PATH


def parse_multiple(multiple_argument: List[str]) -> Dict[str, str]:
    tags_splitted = [t.split("=") for t in multiple_argument]
    tags_dict = {t[0]: t[1] for t in tags_splitted}
    return tags_dict


def read_json(file_path: str) -> Dict[str, Any]:
    with open(file_path, "r") as f:
        return json.load(f)


def write_json(content: Dict[str, Any], file_path: str):
    with open(file_path, "w") as f:
        json.dump(content, f, indent=4)


def update_json(new_content: Dict[str, Any], file_path: str):
    try:
        content = read_json(file_path)
    except FileNotFoundError:
        content = {}
    content.update(new_content)
    write_json(content, file_path)


class ContextLockFile:

    @staticmethod
    def set_context(context_id: str) -> None:
        update_json({"context_id": context_id}, LOCK_FILE_PATH)

    @staticmethod
    def get_context() -> Any:
        if pathlib.Path(LOCK_FILE_PATH).exists():
            return read_json(LOCK_FILE_PATH).get("context_id")
        else:
            return None


class DeploymentFile:
    def __init__(self, path):
        self._path = path

    def get_environment(self, environment: str) -> Any:
        return read_json(self._path).get(environment)

    def update_environment(self, environment: str, content):
        environment_data = self.get_environment(environment)
        environment_data.update(content)
        update_json({environment: environment_data}, self._path)


class InfoFile:

    @staticmethod
    def _create_dir() -> None:
        if not Path(DBX_PATH).exists():
            dbx_echo("dbx directory is not present, creating it")
            os.mkdir(DBX_PATH)

    @staticmethod
    def _create_deployment_file() -> None:
        if not Path(CONF_PATH).exists():
            os.mkdir(CONF_PATH)
        if not Path(DEFAULT_DEPLOYMENT_FILE_PATH).exists():
            dbx_echo("dbx deployment file is not present, creating it from template")
            shutil.copy(DEPLOYMENT_TEMPLATE_PATH, DEFAULT_DEPLOYMENT_FILE_PATH)

    @staticmethod
    def _create_lock_file() -> None:
        if not Path(LOCK_FILE_PATH).exists():
            pathlib.Path(LOCK_FILE_PATH).write_text("{}")

    @staticmethod
    def initialize():

        InfoFile._create_dir()
        InfoFile._create_deployment_file()
        InfoFile._create_lock_file()

        init_content = {"environments": {}}
        write_json(init_content, INFO_FILE_PATH)

    @staticmethod
    def update(content: Dict[str, Any]) -> None:
        update_json(content, INFO_FILE_PATH)

    @staticmethod
    def get(item: str) -> Any:
        if not pathlib.Path(INFO_FILE_PATH).exists():
            raise Exception("Your project is not yet configured, please configure it via `dbx configure`")
        return read_json(INFO_FILE_PATH).get(item)


class ApiV1Client:
    def __init__(self, api_client: ApiClient):
        self.v1_client = copy.deepcopy(api_client)
        self.v1_client.url = self.v1_client.url.replace('/api/2.0', '/api/1.2')

    def get_command_status(self, payload) -> Dict[Any, Any]:
        result = self.v1_client.perform_query(method='GET', path='/commands/status', data=payload)
        return result

    def cancel_command(self, payload) -> None:
        self.v1_client.perform_query(method='POST', path='/commands/cancel', data=payload)

    def execute_command(self, payload) -> Dict[Any, Any]:
        result = self.v1_client.perform_query(method='POST',
                                              path='/commands/execute',
                                              data=payload)
        return result

    def get_context_status(self, payload):
        try:
            result = self.v1_client.perform_query(method='GET',
                                                  path='/contexts/status', data=payload)
            return result
        except requests.exceptions.HTTPError:
            return None

    # sometimes cluster is already in the status="RUNNING", however it couldn't yet provide execution context
    # to make the execute command stable is such situations, we add retry handler.
    @retry(tries=10, delay=5, backoff=5)
    def create_context(self, payload):
        result = self.v1_client.perform_query(method='POST',
                                              path='/contexts/create',
                                              data=payload)
        return result


def dbx_echo(message: str):
    formatted_message = "[dbx][%s] %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], message)
    click.echo(formatted_message)


def generate_filter_string(env: str, tags: Dict[str, str]) -> str:
    general_filters = [
        'tags.dbx_environment="%s"' % env,
        'tags.dbx_status="SUCCESS"',
        'tags.dbx_action_type="deploy"'
    ]

    branch_name = get_current_branch_name()
    if branch_name:
        general_filters.append(f'tags.dbx_branch_name="{branch_name}"')

    tags_filters = ['tags.%s="%s"' % (k, v) for k, v in tags.items()]
    all_filters = general_filters + tags_filters
    filter_string = " and ".join(all_filters)
    return filter_string


def environment_option(f):
    return click.option('-e', '--environment', required=False, default="default",
                        help='Environment name.')(f)


def profile_option(f):
    return click.option('--profile', required=False, default=DEFAULT_SECTION,
                        help='CLI connection profile to use. The default profile is "DEFAULT".')(f)


def _prepare_workspace_dir(api_client: ApiClient, ws_dir: str):
    p = str(pathlib.PurePosixPath(ws_dir).parent)
    service = WorkspaceService(api_client)
    service.mkdirs(p)


def prepare_environment(environment: str):
    environment_data = InfoFile.get("environments").get(environment)

    if not environment_data:
        raise Exception("No environment %s provided in the project file" % environment)

    config = EnvironmentVariableConfigProvider().get_config()
    if config:
        config_type = "ENV"
        dbx_echo("Using configuration from the environment variables")
    else:
        dbx_echo("No environment variables provided, using the ~/.databrickscfg")
        config = ProfileConfigProvider(environment_data["profile"]).get_config()
        config_type = "PROFILE"
        if not config:
            raise Exception("Couldn't get profile with name: %s. Please check the config settings" % environment_data["profile"])

    api_client = _get_api_client(config, command_name="cicdtemplates-")
    _prepare_workspace_dir(api_client, environment_data["workspace_dir"])

    if config_type == "ENV":
        mlflow.set_tracking_uri(DATABRICKS_MLFLOW_URI)
    elif config_type == "PROFILE":
        mlflow.set_tracking_uri("%s://%s" % (DATABRICKS_MLFLOW_URI, environment_data["profile"]))
    else:
        raise NotImplementedError("Config type: %s is not implemented" % config_type)

    experiment = mlflow.get_experiment_by_name(environment_data["workspace_dir"])

    if not experiment:
        mlflow.create_experiment(environment_data["workspace_dir"], environment_data["artifact_location"])

    mlflow.set_experiment(environment_data["workspace_dir"])

    return api_client


def get_package_file() -> Optional[pathlib.Path]:
    dbx_echo("Locating package file")
    file_locator = list(pathlib.Path("dist").glob("*.whl"))
    if file_locator:
        file_path = file_locator[0]
        dbx_echo(f"Package file located in: {file_path}")
        return file_path
    else:
        dbx_echo("Package file was not found")
        return None


def handle_package(rebuild_arg):
    if rebuild_arg:
        dbx_echo("No rebuild will be done, please ensure that the package distribution is in dist folder")
    else:
        dbx_echo("Re-building package")
        if not pathlib.Path("setup.py").exists():
            raise Exception(
                "No setup.py provided in project directory. Please create one, or disable rebuild via --no-rebuild")
        sandbox.run_setup('setup.py', ['-q', 'clean', 'bdist_wheel'])
        dbx_echo("Package re-build finished")


class FileUploader:
    def __init__(self, api_client: ApiClient):
        self._dbfs_service = DbfsService(api_client)

    def file_exists(self, file_path: str):
        try:
            self._dbfs_service.get_status(file_path)
            return True
        except:
            return False

    @retry(tries=3, delay=1, backoff=0.3)
    def upload_file(self, file_path: pathlib.Path):
        posix_path_str = file_path.as_posix()
        posix_path = pathlib.PurePosixPath(posix_path_str)
        dbx_echo(f"Deploying file: {file_path}")
        mlflow.log_artifact(str(file_path), str(posix_path.parent))


def get_current_branch_name() -> Optional[str]:
    if "GITHUB_REF" in os.environ:
        ref = os.environ["GITHUB_REF"].split("/")
        return ref[-1]
    else:
        try:
            repo = git.Repo(".")
            if repo.head.is_detached:
                return None
            else:
                return repo.active_branch.name
        except git.InvalidGitRepositoryError:
            return None