import argparse
import datetime
import sys
from pathlib import Path
from typing import List, Optional

import humanize

from darwin.client import Client
from darwin.config import Config
from darwin.dataset.identifier import DatasetIdentifier
from darwin.exceptions import (
    InvalidLogin,
    MissingConfig,
    NameTaken,
    NotFound,
    Unauthenticated,
    ValidationError,
)
from darwin.table import Table
from darwin.utils import find_files, persist_client_configuration, prompt, secure_continue_request


def authenticate(
    api_key: str, default_team: Optional[bool] = None, datasets_dir: Optional[Path] = None
) -> Config:
    """Authenticate the API key against the server and creates a configuration file for it

    Parameters
    ----------
    api_key : str
        API key to use for the client login
    default_team: bool
        Flag to make the team the default one
    datasets_dir: Path
        Dataset directory on the file system

    Returns
    -------
    Config
    A configuration object to handle YAML files
    """
    # Resolve the home folder if the dataset_dir starts with ~ or ~user

    try:
        client = Client.from_api_key(api_key=api_key)
        config_path = Path.home() / ".darwin" / "config.yaml"
        config_path.parent.mkdir(exist_ok=True)

        if default_team is None:
            default_team = input(f"Make {client.default_team} the default team? [y/N] ") in [
                "Y",
                "y",
            ]
        if datasets_dir is None:
            datasets_dir = prompt("Datasets directory", "~/.darwin/datasets")

        datasets_dir = Path(datasets_dir).expanduser()
        Path(datasets_dir).mkdir(parents=True, exist_ok=True)

        client.set_datasets_dir(datasets_dir)

        default_team = client.default_team if default_team else None
        return persist_client_configuration(client, default_team=default_team)

    except InvalidLogin:
        _error("Invalid API key")


def current_team():
    """Print the team currently authenticated against"""
    client = _load_client()
    print(client.team)


def list_teams():
    """Print a table of teams to which the client belong to"""
    for team in _config().get_all_teams():
        if team["default"]:
            print(f"{team['slug']} (default)")
        else:
            print(team["slug"])


def set_team(team_slug: str):
    """Switches the client to the selected team and persist the change on the configuration file

    Parameters
    ----------
    team_slug : str
        Slug of the team to switch to
    """

    config = _config()
    config.set_default_team(team_slug)


def create_dataset(name: str, team: Optional[str] = None):
    """Creates a dataset remotely"""
    client = _load_client(team=team)
    try:
        dataset = client.create_dataset(name=name)
        print(
            f"Dataset '{dataset.name}' ({dataset.team}/{dataset.slug}) has been created.\nAccess at {dataset.remote_path}"
        )
    except NameTaken:
        _error(f"Dataset name '{name}' is already taken.")
    except ValidationError:
        _error(f"Dataset name '{name}' is not valid.")


def local():
    """Lists synced datasets, stored in the specified path. """
    table = Table(["name", "images", "sync_date", "size"], [Table.L, Table.R, Table.R, Table.R])
    client = _load_client(offline=True)
    for dataset_path in client.list_local_datasets():
        table.add_row(
            {
                "name": dataset_path.name,
                "images": sum(1 for _ in find_files([dataset_path])),
                "sync_date": humanize.naturaldate(
                    datetime.datetime.fromtimestamp(dataset_path.stat().st_mtime)
                ),
                "size": humanize.naturalsize(
                    sum(p.stat().st_size for p in find_files([dataset_path]))
                ),
            }
        )
    print(table)


def path(dataset_slug: str) -> Path:
    """Returns the absolute path of the specified dataset, if synced"""
    identifier = DatasetIdentifier.parse(dataset_slug)
    client = _load_client(offline=True)
    try:
        for p in client.list_local_datasets(team=identifier.team_slug):
            if identifier.dataset_slug == p.name:
                return p
    except NotFound as e:
        _error(
            f"Dataset '{e.name}' does not exist locally. "
            f"Use 'darwin dataset remote' to see all the available datasets, "
            f"and 'darwin dataset pull' to pull them."
        )


def url(dataset_slug: str) -> Path:
    """Returns the url of the specified dataset"""
    client = _load_client(offline=True)
    try:
        remote_dataset = client.get_remote_dataset(dataset_identifier=dataset_slug)
        print(remote_dataset.remote_path)
    except NotFound as e:
        _error(f"Dataset '{e.name}' does not exist.")


def dataset_report(dataset_slug: str, granularity) -> Path:
    """Returns the url of the specified dataset"""
    client = _load_client(offline=True)
    try:
        remote_dataset = client.get_remote_dataset(dataset_identifier=dataset_slug)
        report = remote_dataset.get_report(granularity)
        print(report)
    except NotFound:
        _error(f"Dataset '{dataset_slug}' does not exist.")


def export_dataset(
    dataset_slug: str, annotation_class_ids: Optional[List] = None, name: Optional[str] = None
):
    """Create a new release for the dataset

    Parameters
    ----------
    dataset_slug: str
        Slug of the dataset to which we perform the operation on
    annotation_class_ids: List
        List of the classes to filter
    name: str
        Name of the release
    """
    client = _load_client(offline=False)
    identifier = DatasetIdentifier.parse(dataset_slug)
    ds = client.get_remote_dataset(identifier)
    ds.export(annotation_class_ids=annotation_class_ids, name=name)
    identifier.version = name
    print(f"Dataset {dataset_slug} successfully exported to {identifier}")


def pull_dataset(dataset_slug: str):
    """Downloads a remote dataset (images and annotations) in the datasets directory.

    Parameters
    ----------
    dataset_slug: str
        Slug of the dataset to which we perform the operation on
    """
    version = DatasetIdentifier.parse(dataset_slug).version or "latest"
    client = _load_client(offline=False)
    try:
        dataset = client.get_remote_dataset(dataset_identifier=dataset_slug)
    except NotFound:
        _error(
            f"dataset '{dataset_slug}' does not exist at {client.url}. "
            f"Use 'darwin remote' to list all the remote datasets."
        )
    except Unauthenticated:
        _error(f"please re-authenticate")
    try:
        release = dataset.get_release(version)
        dataset.pull(release=release)
    except NotFound:
        _error(
            f"Version '{dataset.identifier}:{version}' does not exist "
            f"Use 'darwin dataset releases' to list all available versions."
        )
    print(f"Dataset {release.identifier} downloaded at {dataset.local_path}. ")


def list_remote_datasets(all_teams: bool, team: Optional[str] = None):
    """Lists remote datasets with its annotation progress"""
    # TODO: add listing open datasets
    table = Table(["name", "images", "progress"], [Table.L, Table.R, Table.R])
    datasets = []
    if all_teams:
        for team in _config().get_all_teams():
            client = _load_client(team["slug"])
            datasets += client.list_remote_datasets()
    else:
        client = _load_client(team)
        datasets = client.list_remote_datasets()

    for dataset in datasets:
        table.add_row(
            {
                "name": f"{dataset.team}/{dataset.slug}",
                "images": dataset.image_count,
                "progress": f"{round(dataset.progress*100,1)}%",
            }
        )
    if len(table) == 0:
        print("No dataset available.")
    else:
        print(table)


def remove_remote_dataset(dataset_slug: str):
    """Remove a remote dataset from the workview. The dataset gets archived. """
    client = _load_client(offline=False)
    try:
        dataset = client.get_remote_dataset(dataset_identifier=dataset_slug)
        print(f"About to delete {dataset.identifier} on darwin.")
        if not secure_continue_request():
            print("Cancelled.")
            return

        dataset.remove_remote()
    except NotFound:
        _error(f"No dataset with name '{dataset_slug}'")


def dataset_list_releases(dataset_slug: str):
    client = _load_client(offline=False)
    try:
        dataset = client.get_remote_dataset(dataset_identifier=dataset_slug)
        releases = dataset.get_releases()
        if len(releases) == 0:
            print("No available releases, export one first.")
            return
        table = Table(
            ["name", "images", "classes", "export_date"], [Table.L, Table.R, Table.R, Table.R]
        )
        for release in releases:
            if not release.available:
                continue
            table.add_row(
                {
                    "name": release.identifier,
                    "images": release.image_count,
                    "classes": release.class_count,
                    "export_date": release.export_date,
                }
            )
        print(table)
    except NotFound:
        _error(f"No dataset with name '{dataset_slug}'")


def upload_data(
    dataset_slug: str, files: Optional[List[str]], files_to_exclude: Optional[List[str]], fps: int
):
    """Uploads the files provided as parameter to the remote dataset selected

    Parameters
    ----------
    dataset_slug : str
        Slug of the dataset to retrieve
    files : list[str]
        List of files to upload. Can be None.
    files_to_exclude : list[str]
        List of files to exclude from the file scan (which is done only if files is None)
    fps : int
        Frame rate to split videos in

    Returns
    -------
    generator : function
            Generator for doing the actual uploads. This is None if blocking is True
    count : int
        The files count
    """
    client = _load_client()
    try:
        dataset = client.get_remote_dataset(dataset_identifier=dataset_slug)
        dataset.push(files_to_exclude=files_to_exclude, fps=fps, files_to_upload=files)
    except NotFound as e:
        _error(f"No dataset with name '{e.name}'")
    except ValueError:
        _error(f"No files found")


def help(parser, subparser: Optional[str] = None):
    if subparser:
        parser = next(
            action.choices[subparser]
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction) and subparser in action.choices
        )

    actions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]

    print(parser.description)
    print("\nCommands:")
    for action in actions:
        # get all subparsers and print help
        for choice in sorted(action._choices_actions, key=lambda x: x.dest):
            print("    {:<19} {}".format(choice.dest, choice.help))


def _error(message):
    print(f"Error: {message}")
    sys.exit(1)


def _config():
    return Config(Path.home() / ".darwin" / "config.yaml")


def _load_client(team: Optional[str] = None, offline: bool = False):
    """Fetches a client, potentially offline

    Parameters
    ----------
    offline : bool
        Flag for using an offline client

    Returns
    -------
    Client
    The client requested
    """
    try:
        config_dir = Path.home() / ".darwin" / "config.yaml"
        client = Client.from_config(config_dir, team_slug=team)
        return client
    except MissingConfig:
        _error("Authenticate first")
    except InvalidLogin:
        _error("Please re-authenticate")
    except Unauthenticated:
        _error("Please re-authenticate")
