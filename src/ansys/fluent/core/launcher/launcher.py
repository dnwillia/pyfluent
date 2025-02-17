"""Provides a module for launching Fluent.

This module supports both starting Fluent locally and connecting to a
remote instance with gRPC.
"""
from enum import Enum
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import time
from typing import Any, Dict, Union

from ansys.fluent.core.fluent_connection import FluentConnection
from ansys.fluent.core.launcher.fluent_container import (
    configure_container_dict,
    start_fluent_container,
)
import ansys.fluent.core.launcher.watchdog as watchdog
from ansys.fluent.core.scheduler import build_parallel_options, load_machines
from ansys.fluent.core.session import _parse_server_info_file
from ansys.fluent.core.session_meshing import Meshing
from ansys.fluent.core.session_pure_meshing import PureMeshing
from ansys.fluent.core.session_solver import Solver
from ansys.fluent.core.session_solver_icing import SolverIcing
from ansys.fluent.core.utils.networking import find_remoting_ip
import ansys.platform.instancemanagement as pypim

_THIS_DIR = os.path.dirname(__file__)
_OPTIONS_FILE = os.path.join(_THIS_DIR, "fluent_launcher_options.json")
logger = logging.getLogger("pyfluent.launcher")


def _is_windows():
    """Check if the current operating system is windows."""
    return platform.system() == "Windows"


class LaunchMode(Enum):
    """An enumeration over supported Fluent launch modes."""

    STANDALONE = 1
    PIM = 2
    CONTAINER = 3


def check_docker_support():
    """Checks whether Python Docker SDK is supported by the current system."""
    import docker

    try:
        _ = docker.from_env()
    except docker.errors.DockerException:
        return False
    return True


class FluentVersion(Enum):
    """An enumeration over supported Fluent versions."""

    version_24R1 = "24.1.0"
    version_23R2 = "23.2.0"
    version_23R1 = "23.1.0"
    version_22R2 = "22.2.0"

    @classmethod
    def _missing_(cls, version):
        if isinstance(version, (float, str)):
            version = str(version) + ".0"
            for v in FluentVersion:
                if version == v.value:
                    return FluentVersion(version)
            else:
                raise RuntimeError(
                    f"The specified version '{version[:-2]}' is not supported."
                    + " Supported versions are: "
                    + ", ".join([ver.value for ver in FluentVersion][::-1])
                )

    def __str__(self):
        return str(self.value)


def get_ansys_version() -> str:
    """Return the version string corresponding to the most recent, available ANSYS
    installation. The returned value is the string component of one of the members
    of the FluentVersion class.
    """
    for v in FluentVersion:
        if "AWP_ROOT" + "".join(str(v).split("."))[:-1] in os.environ:
            return str(v)

    raise RuntimeError("No ANSYS version can be found.")


def get_fluent_exe_path(**launch_argvals) -> Path:
    """Get Fluent executable path. The path is searched in the following order.

    1. ``product_version`` parameter passed with ``launch_fluent``.
    2. The latest ANSYS version from ``AWP_ROOTnnn``` environment variables.

    Returns
    -------
    Path
        Fluent executable path
    """

    def get_fluent_root(version: FluentVersion) -> Path:
        awp_root = os.environ["AWP_ROOT" + "".join(str(version).split("."))[:-1]]
        return Path(awp_root) / "fluent"

    def get_exe_path(fluent_root: Path) -> Path:
        if _is_windows():
            return fluent_root / "ntbin" / "win64" / "fluent.exe"
        else:
            return fluent_root / "bin" / "fluent"

    # (DEV) "PYFLUENT_FLUENT_ROOT" environment variable
    fluent_root = os.getenv("PYFLUENT_FLUENT_ROOT")
    if fluent_root:
        return get_exe_path(Path(fluent_root))

    # Look for Fluent exe path in the following order:
    # 1. product_version parameter passed with launch_fluent
    product_version = launch_argvals.get("product_version")
    if product_version:
        return get_exe_path(get_fluent_root(FluentVersion(product_version)))

    # 2. the latest ANSYS version from AWP_ROOT environment variables
    ansys_version = get_ansys_version()
    return get_exe_path(get_fluent_root(FluentVersion(ansys_version)))


class FluentMode(Enum):
    """An enumeration over supported Fluent modes."""

    # Tuple: Name, Solver object type, Meshing flag, Launcher options
    MESHING_MODE = ("meshing", Meshing, True, [])
    PURE_MESHING_MODE = ("pure-meshing", PureMeshing, True, [])
    SOLVER = ("solver", Solver, False, [])
    SOLVER_ICING = ("solver-icing", SolverIcing, False, [("fluent_icing", True)])

    @staticmethod
    def get_mode(mode: str):
        """Returns the FluentMode based on the provided mode string."""
        for m in FluentMode:
            if mode == m.value[0]:
                return m
        else:
            raise RuntimeError(
                f"The passed mode '{mode}' matches none of the allowed modes."
            )


def _get_server_info_filepath():
    server_info_dir = os.getenv("SERVER_INFO_DIR")
    dir_ = Path(server_info_dir) if server_info_dir else tempfile.gettempdir()
    fd, filepath = tempfile.mkstemp(suffix=".txt", prefix="serverinfo-", dir=str(dir_))
    os.close(fd)
    return filepath


def _get_subprocess_kwargs_for_fluent(env: Dict[str, Any]) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    kwargs.update(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if _is_windows():
        kwargs.update(shell=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs.update(shell=True, start_new_session=True)
    fluent_env = os.environ.copy()
    fluent_env.update({k: str(v) for k, v in env.items()})
    fluent_env["REMOTING_THROW_LAST_TUI_ERROR"] = "1"
    from ansys.fluent.core import INFER_REMOTING_IP

    if INFER_REMOTING_IP and not "REMOTING_SERVER_ADDRESS" in fluent_env:
        remoting_ip = find_remoting_ip()
        if remoting_ip:
            fluent_env["REMOTING_SERVER_ADDRESS"] = remoting_ip
    kwargs.update(env=fluent_env)
    return kwargs


def _build_fluent_launch_args_string(**kwargs) -> str:
    """Build Fluent's launch arguments string from keyword arguments.

    Returns
    -------
    str
        Fluent's launch arguments string.
    """
    all_options = None
    with open(_OPTIONS_FILE, encoding="utf-8") as fp:
        all_options = json.load(fp)
    launch_args_string = ""
    for k, v in all_options.items():
        argval = kwargs.get(k)
        default = v.get("default")
        if argval is None and v.get("fluent_required") is True:
            argval = default
        if argval is not None:
            allowed_values = v.get("allowed_values")
            if allowed_values and argval not in allowed_values:
                if default is not None:
                    old_argval = argval
                    argval = default
                    logger.warning(
                        f"Specified value '{old_argval}' for argument '{k}' is not an allowed value ({allowed_values}), default value '{argval}' is going to be used instead."
                    )
                else:
                    logger.warning(
                        f"{k} = {argval} is discarded as it is not an allowed value. Allowed values: {allowed_values}"
                    )
                    continue
            fluent_map = v.get("fluent_map")
            if fluent_map:
                if isinstance(argval, str):
                    json_key = argval
                else:
                    json_key = json.dumps(argval)
                argval = fluent_map[json_key]
            launch_args_string += v["fluent_format"].replace("{}", str(argval))
    addArgs = kwargs["additional_arguments"]
    if "-t" not in addArgs and "-cnf=" not in addArgs:
        mlist = load_machines(ncores=kwargs["processor_count"])
        launch_args_string += " " + build_parallel_options(mlist)
    return launch_args_string


def launch_remote_fluent(
    session_cls,
    start_transcript: bool,
    start_timeout: int = 100,
    product_version: str = None,
    cleanup_on_exit: bool = True,
    meshing_mode: bool = False,
    dimensionality: str = None,
    launcher_args: Dict[str, Any] = None,
) -> Union[Meshing, PureMeshing, Solver, SolverIcing]:
    """Launch Fluent remotely using `PyPIM <https://pypim.docs.pyansys.com>`.

    When calling this method, you must ensure that you are in an
    environment where PyPIM is configured. You can use the :func:
    `pypim.is_configured <ansys.platform.instancemanagement.is_configured>`
    method to verify that PyPIM is configured.

    Parameters
    ----------
    session_cls: Union[type(Meshing), type(PureMeshing), type(Solver), type(SolverIcing)]
        Session type.
    start_transcript: bool
        Whether to start streaming the Fluent transcript in the client. The
        default is ``True``. You can stop and start the streaming of the
        Fluent transcript subsequently via method calls on the session object.
    start_timeout : int, optional
        Maximum allowable time in seconds for connecting to the Fluent
        server. The default is ``100``.
    product_version : str, optional
        Select an installed version of ANSYS. The string must be in a format like
        ``"23.2.0"`` (for 2023 R2) matching the documented version format in the
        FluentVersion class. The default is ``None``, in which case the newest installed
        version is used.
    cleanup_on_exit : bool, optional
        Whether to clean up and exit Fluent when Python exits or when garbage
        is collected for the Fluent Python instance. The default is ``True``.
    meshing_mode : bool, optional
        Whether to launch Fluent remotely in meshing mode. The default is
        ``False``.
    dimensionality : str, optional
        Geometric dimensionality of the Fluent simulation. The default is ``None``,
        in which case ``"3d"`` is used. Options are ``"3d"`` and ``"2d"``.

    Returns
    -------
    :obj:`~typing.Union` [:class:`Meshing<ansys.fluent.core.session_meshing.Meshing>`, \
    :class:`~ansys.fluent.core.session_pure_meshing.PureMeshing`, \
    :class:`~ansys.fluent.core.session_solver.Solver`, \
    :class:`~ansys.fluent.core.session_solver_icing.SolverIcing`]
        Session object.
    """
    pim = pypim.connect()
    instance = pim.create_instance(
        product_name="fluent-meshing"
        if meshing_mode
        else "fluent-2ddp"
        if dimensionality == "2d"
        else "fluent-3ddp",
        product_version=product_version,
    )
    instance.wait_for_ready()
    # nb pymapdl sets max msg len here:
    channel = instance.build_grpc_channel()
    return session_cls(
        fluent_connection=FluentConnection(
            channel=channel,
            cleanup_on_exit=cleanup_on_exit,
            remote_instance=instance,
            start_timeout=start_timeout,
            start_transcript=start_transcript,
            launcher_args=launcher_args,
        )
    )


def _get_session_info(argvals, mode: Union[FluentMode, str, None] = None):
    """Updates the session information."""
    if mode is None:
        mode = FluentMode.SOLVER

    if isinstance(mode, str):
        mode = FluentMode.get_mode(mode)
    new_session = mode.value[1]
    meshing_mode = mode.value[2]
    for k, v in mode.value[3]:
        argvals[k] = v

    return new_session, meshing_mode, argvals, mode


def _raise_exception_g_gu_in_windows_os(additional_arguments: str) -> None:
    """If -g or -gu is passed in Windows OS, the exception should be raised."""
    additional_arg_list = additional_arguments.split()
    if _is_windows() and (
        ("-g" in additional_arg_list) or ("-gu" in additional_arg_list)
    ):
        raise ValueError("'-g' and '-gu' is not supported on windows platform.")


def _update_launch_string_wrt_gui_options(
    launch_string: str, show_gui: bool = None, additional_arguments: str = ""
) -> str:
    """Checks for all gui options in additional arguments and updates the
    launch string with hidden, if none of the options are met."""

    if (show_gui is False) or (
        show_gui is None and (os.getenv("PYFLUENT_SHOW_SERVER_GUI") != "1")
    ):
        if not {"-g", "-gu"} & set(additional_arguments.split()):
            launch_string += " -hidden"

    return launch_string


def _await_fluent_launch(
    server_info_filepath: str, start_timeout: int, sifile_last_mtime: float
):
    """Wait for successful fluent launch or raise an error."""
    while True:
        if Path(server_info_filepath).stat().st_mtime > sifile_last_mtime:
            time.sleep(1)
            logger.info("Fluent has been successfully launched.")
            break
        if start_timeout == 0:
            raise RuntimeError("The launch process has been timed out.")
        time.sleep(1)
        start_timeout -= 1
        logger.info(f"Waiting for Fluent to launch...{start_timeout} seconds remaining")


def _get_server_info(
    server_info_filepath: str, ip: str = None, port: int = None, password: str = None
):
    """Get server connection information of an already running session."""
    if ip and port:
        logger.debug(
            "The server-info file was not parsed because ip and port were provided explicitly."
        )
    elif server_info_filepath:
        ip, port, password = _parse_server_info_file(server_info_filepath)
    elif os.getenv("PYFLUENT_FLUENT_IP") and os.getenv("PYFLUENT_FLUENT_PORT"):
        ip = port = None
    else:
        raise RuntimeError(
            "Please provide either ip and port data or server-info file."
        )

    return ip, port, password


def _get_running_session_mode(
    fluent_connection: FluentConnection, mode: FluentMode = None
):
    """Get the mode of the running session if the mode has not been mentioned
    explicitly."""
    if mode:
        session_mode = mode
    else:
        try:
            session_mode = FluentMode.get_mode(
                "solver"
                if fluent_connection.scheme_eval.scheme_eval("(cx-solver-mode?)")
                else "meshing"
            )
        except Exception as ex:
            raise RuntimeError("Fluent session password mismatch") from ex
    return session_mode.value[1]


def _generate_launch_string(
    argvals,
    meshing_mode: bool,
    show_gui: bool,
    additional_arguments: str,
    server_info_filepath: str,
):
    """Generates the launch string to launch fluent."""
    if _is_windows():
        exe_path = '"' + str(get_fluent_exe_path(**argvals)) + '"'
    else:
        exe_path = str(get_fluent_exe_path(**argvals))
    launch_string = exe_path
    launch_string += _build_fluent_launch_args_string(**argvals)
    if meshing_mode:
        launch_string += " -meshing"
    launch_string += f" {additional_arguments}"
    launch_string += f' -sifile="{server_info_filepath}"'
    launch_string += " -nm"
    launch_string = _update_launch_string_wrt_gui_options(
        launch_string, show_gui, additional_arguments
    )
    return launch_string


def scm_to_py(topy):
    """Convert journal filenames to Python filename."""
    if not isinstance(topy, (str, list)):
        raise TypeError("Journal name should be of str or list type.")
    if isinstance(topy, str):
        topy = [topy]
    fluent_jou_arg = "".join([f'-i "{journal}" ' for journal in topy])
    return f" {fluent_jou_arg} -topy"


class LaunchFluentError(Exception):
    """Exception class representing launch errors."""

    def __init__(self, launch_string):
        """__init__ method of LaunchFluentError class."""
        details = "\n" + "Fluent Launch string: " + launch_string
        super().__init__(details)


#   pylint: disable=unused-argument
def launch_fluent(
    product_version: str = None,
    version: str = None,
    precision: str = None,
    processor_count: int = None,
    journal_filepath: str = None,
    start_timeout: int = 60,
    additional_arguments: str = None,
    env: Dict[str, Any] = None,
    start_container: bool = None,
    container_dict: dict = None,
    dry_run: bool = False,
    cleanup_on_exit: bool = True,
    start_transcript: bool = True,
    show_gui: bool = None,
    case_filepath: str = None,
    case_data_filepath: str = None,
    lightweight_mode: bool = None,
    mode: Union[FluentMode, str, None] = None,
    py: bool = None,
    gpu: bool = None,
    cwd: str = None,
    topy: Union[str, list] = None,
    start_watchdog: bool = None,
    **kwargs,
) -> Union[Meshing, PureMeshing, Solver, SolverIcing, dict]:
    """Launch Fluent locally in server mode or connect to a running Fluent
    server instance.

    Parameters
    ----------
    product_version : str, optional
        Select an installed version of ANSYS. The string must be in a format like
        ``"23.2.0"`` (for 2023 R2) matching the documented version format in the
        FluentVersion class. The default is ``None``, in which case the newest installed
        version is used.
    version : str, optional
        Geometric dimensionality of the Fluent simulation. The default is ``None``,
        in which case ``"3d"`` is used. Options are ``"3d"`` and ``"2d"``.
    precision : str, optional
        Floating point precision. The default is ``None``, in which case ``"double"``
        is used. Options are ``"double"`` and ``"single"``.
    processor_count : int, optional
        Number of processors. The default is ``None``, in which case ``1``
        processor is used.  In job scheduler environments the total number of
        allocated cores is clamped to this value.
    journal_filepath : str, optional
        Name of the journal file to read. The default is ``None``.
    start_timeout : int, optional
        Maximum allowable time in seconds for connecting to the Fluent
        server. The default is ``60``.
    additional_arguments : str, optional
        Additional arguments to send to Fluent as a string in the same
        format they are normally passed to Fluent on the command line.
    env : dict[str, str], optional
        Mapping to modify environment variables in Fluent. The default
        is ``None``.
    start_container : bool, optional
        Specifies whether to launch a Fluent Docker container image. For more details about containers, see
        :mod:`~ansys.fluent.core.launcher.fluent_container`.
    container_dict : dict, optional
        Dictionary for Fluent Docker container configuration. If specified,
        setting ``start_container = True`` as well is redundant.
        Will launch Fluent inside a Docker container using the configuration changes specified.
        See also :mod:`~ansys.fluent.core.launcher.fluent_container`.
    dry_run : bool, optional
        Defaults to False. If True, will not launch Fluent, and will instead print configuration information
        that would be used as if Fluent was being launched. If dry running a container start,
        ``launch_fluent()`` will return the configured ``container_dict``.
    cleanup_on_exit : bool, optional
        Whether to shut down the connected Fluent session when PyFluent is
        exited, or the ``exit()`` method is called on the session instance,
        or if the session instance becomes unreferenced. The default is ``True``.
    start_transcript : bool, optional
        Whether to start streaming the Fluent transcript in the client. The
        default is ``True``. You can stop and start the streaming of the
        Fluent transcript subsequently via the method calls, ``transcript.start()``
        and ``transcript.stop()`` on the session object.
    show_gui : bool, optional
        Whether to display the Fluent GUI. The default is ``None``, which does not
        cause the GUI to be shown. If a value of ``False`` is
        not explicitly provided, the GUI will also be shown if
        the environment variable ``PYFLUENT_SHOW_SERVER_GUI`` is set to 1.
    case_filepath : str, optional
        If provided, the case file at ``case_filepath`` is read into the Fluent session.
    case_data_filepath : str, optional
        If provided, the case and data files at ``case_data_filepath`` are read into the Fluent session.
    lightweight_mode : bool, optional
        Whether to run in lightweight mode. In lightweight mode, the lightweight settings are read into the
        current Fluent solver session. The mesh is read into a background Fluent solver session which will
        replace the current Fluent solver session once the mesh read is complete and the lightweight settings
        made by the user in the current Fluent solver session have been applied in the background Fluent
        solver session. This is all orchestrated by PyFluent and requires no special usage.
        This parameter is used only when ``case_filepath`` is provided. The default is ``False``.
    mode : str, optional
        Launch mode of Fluent to point to a specific session type.
        The default value is ``None``. Options are ``"meshing"``,
        ``"pure-meshing"`` and ``"solver"``.
    py : bool, optional
        If True, Fluent will run in Python mode. Default is None.
    gpu : bool, optional
        If True, Fluent will start with GPU Solver.
    cwd : str, Optional
        Working directory for the Fluent client.
    topy : str or list, optional
        The string path to a Fluent journal file, or a list of such paths. Fluent will execute the
        journal(s) and write the equivalent Python journal(s).
    start_watchdog : bool, optional
        When ``cleanup_on_exit`` is True, ``start_watchdog`` defaults to True,
        which means an independent watchdog process is run to ensure
        that any local GUI-less Fluent sessions started by PyFluent are properly closed (or killed if frozen)
        when the current Python process ends.

    Returns
    -------
    :obj:`~typing.Union` [:class:`Meshing<ansys.fluent.core.session_meshing.Meshing>`, \
    :class:`~ansys.fluent.core.session_pure_meshing.PureMeshing`, \
    :class:`~ansys.fluent.core.session_solver.Solver`, \
    :class:`~ansys.fluent.core.session_solver_icing.SolverIcing`, dict]
        Session object or configuration dictionary if ``dry_run = True``.

    Notes
    -----
    In job scheduler environments such as SLURM, LSF, PBS, etc... the allocated
    machines and core counts are queried from the scheduler environment and
    passed to Fluent.
    """
    if kwargs:
        if "meshing_mode" in kwargs:
            raise RuntimeError(
                "'meshing_mode' argument is no longer used."
                " Please use launch_fluent(mode='meshing') to launch in meshing mode."
            )
        else:
            raise TypeError(
                f"launch_fluent() got an unexpected keyword argument {next(iter(kwargs))}"
            )
    del kwargs

    if pypim.is_configured():
        fluent_launch_mode = LaunchMode.PIM
    elif start_container is True or (
        start_container is None
        and (container_dict or os.getenv("PYFLUENT_LAUNCH_CONTAINER") == "1")
    ):
        if check_docker_support():
            fluent_launch_mode = LaunchMode.CONTAINER
        else:
            raise SystemError(
                "Docker is not working correctly in this system, "
                "yet a Fluent Docker container launch was specified."
            )
    else:
        fluent_launch_mode = LaunchMode.STANDALONE

    del start_container

    if additional_arguments is None:
        additional_arguments = ""
    elif fluent_launch_mode == LaunchMode.PIM:
        logger.warning(
            "'additional_arguments' option for 'launch_fluent' is currently not supported "
            "when starting a remote Fluent PyPIM client."
        )

    if fluent_launch_mode == LaunchMode.PIM and start_watchdog:
        logger.warning(
            "'start_watchdog' argument for 'launch_fluent' is currently not supported "
            "when starting a remote Fluent PyPIM client."
        )

    if (
        start_watchdog is None
        and cleanup_on_exit
        and (fluent_launch_mode in (LaunchMode.CONTAINER, LaunchMode.STANDALONE))
    ):
        start_watchdog = True

    if dry_run and fluent_launch_mode != LaunchMode.CONTAINER:
        logger.warning(
            "'dry_run' argument for 'launch_fluent' currently is only "
            "supported when starting containers."
        )

    argvals = locals().copy()
    argvals.pop("fluent_launch_mode")

    if fluent_launch_mode != LaunchMode.STANDALONE:
        arg_names = [
            "env",
            "cwd",
            "topy",
            "case_filepath",
            "lightweight_mode",
            "journal_filepath",
            "case_data_filepath",
        ]
        invalid_arg_names = list(
            filter(lambda arg_name: argvals[arg_name] is not None, arg_names)
        )
        if len(invalid_arg_names) != 0:
            invalid_str_names = ", ".join(invalid_arg_names)
            logger.warning(
                f"These specified arguments are only supported when starting "
                f"local standalone Fluent clients: {invalid_str_names}."
            )

    new_session, meshing_mode, argvals, mode = _get_session_info(argvals, mode)

    if fluent_launch_mode == LaunchMode.STANDALONE:
        if lightweight_mode is None:
            # note argvals is no longer locals() here due to _get_session_info() pass
            argvals.pop("lightweight_mode")
            lightweight_mode = False

        _raise_exception_g_gu_in_windows_os(additional_arguments)

        if os.getenv("PYFLUENT_FLUENT_DEBUG") == "1":
            argvals["fluent_debug"] = True

        server_info_filepath = _get_server_info_filepath()
        launch_string = _generate_launch_string(
            argvals, meshing_mode, show_gui, additional_arguments, server_info_filepath
        )

        sifile_last_mtime = Path(server_info_filepath).stat().st_mtime
        if env is None:
            env = {}
        if mode != FluentMode.SOLVER_ICING:
            env["APP_LAUNCHED_FROM_CLIENT"] = "1"  # disables flserver datamodel
        kwargs = _get_subprocess_kwargs_for_fluent(env)
        if cwd:
            kwargs.update(cwd=cwd)
        if topy:
            launch_string += scm_to_py(topy)

        if _is_windows():
            # Using 'start.exe' is better, otherwise Fluent is more susceptible to bad termination attempts
            launch_cmd = 'start "" ' + launch_string
        else:
            launch_cmd = launch_string

        try:
            logger.debug(f"Launching Fluent with command: {launch_cmd}")

            subprocess.Popen(launch_cmd, **kwargs)

            try:
                _await_fluent_launch(
                    server_info_filepath, start_timeout, sifile_last_mtime
                )
            except RuntimeError as ex:
                if _is_windows():
                    logger.warning(f"Exception caught - {type(ex).__name__}: {ex}")
                    launch_cmd = launch_string.replace('"', "", 2)
                    kwargs.update(shell=False)
                    logger.warning(
                        f"Retrying Fluent launch with less robust command: {launch_cmd}"
                    )
                    subprocess.Popen(launch_cmd, **kwargs)
                    _await_fluent_launch(
                        server_info_filepath, start_timeout, sifile_last_mtime
                    )
                else:
                    raise ex

            session = new_session.create_from_server_info_file(
                server_info_filepath=server_info_filepath,
                cleanup_on_exit=cleanup_on_exit,
                start_transcript=start_transcript,
                launcher_args=argvals,
                inside_container=False,
            )
            if start_watchdog:
                logger.info("Launching Watchdog for local Fluent client...")
                ip, port, password = _get_server_info(server_info_filepath)
                watchdog.launch(os.getpid(), port, password, ip)
            if case_filepath:
                if meshing_mode:
                    session.tui.file.read_case(case_filepath)
                else:
                    session.read_case(case_filepath, lightweight_mode)
            if journal_filepath:
                if meshing_mode:
                    session.tui.file.read_journal(journal_filepath)
                else:
                    session.file.read_journal(journal_filepath)
            if case_data_filepath:
                if not meshing_mode:
                    session.file.read(
                        file_type="case-data", file_name=case_data_filepath
                    )
                else:
                    raise RuntimeError(
                        "Case and data file cannot be read in meshing mode."
                    )

            return session
        except Exception as ex:
            logger.error(f"Exception caught - {type(ex).__name__}: {ex}")
            raise LaunchFluentError(launch_cmd) from ex
        finally:
            server_info_file = Path(server_info_filepath)
            if server_info_file.exists():
                server_info_file.unlink()
    elif fluent_launch_mode == LaunchMode.PIM:
        logger.info(
            "Starting Fluent remotely. The startup configuration will be ignored."
        )

        if product_version:
            fluent_product_version = "".join(product_version.split("."))[:-1]
        else:
            fluent_product_version = None

        return launch_remote_fluent(
            session_cls=new_session,
            start_timeout=start_timeout,
            start_transcript=start_transcript,
            product_version=fluent_product_version,
            cleanup_on_exit=cleanup_on_exit,
            meshing_mode=meshing_mode,
            dimensionality=version,
            launcher_args=argvals,
        )

    elif fluent_launch_mode == LaunchMode.CONTAINER:
        args = _build_fluent_launch_args_string(**argvals).split()
        if meshing_mode:
            args.append(" -meshing")

        if dry_run:
            if container_dict is None:
                container_dict = {}
            config_dict, *_ = configure_container_dict(args, **container_dict)
            from pprint import pprint

            print("\nDocker container run configuration:\n")
            print("config_dict = ")
            if os.getenv("PYFLUENT_HIDE_LOG_SECRETS") != "1":
                pprint(config_dict)
            else:
                config_dict_h = config_dict.copy()
                config_dict_h.pop("environment")
                pprint(config_dict_h)
                del config_dict_h
            return config_dict

        port, password = start_fluent_container(args, container_dict)

        session = new_session(
            fluent_connection=FluentConnection(
                start_timeout=start_timeout,
                port=port,
                password=password,
                cleanup_on_exit=cleanup_on_exit,
                start_transcript=start_transcript,
                launcher_args=argvals,
                inside_container=True,
            )
        )

        if start_watchdog:
            logger.debug("Launching Watchdog for Fluent container...")
            watchdog.launch(os.getpid(), port, password)

        return session


def connect_to_fluent(
    ip: str = None,
    port: int = None,
    cleanup_on_exit: bool = False,
    start_transcript: bool = True,
    server_info_filepath: str = None,
    password: str = None,
    start_watchdog: bool = None,
) -> Union[Meshing, PureMeshing, Solver, SolverIcing]:
    """Connect to an existing Fluent server instance.

    Parameters
    ----------
    ip : str, optional
        IP address for connecting to an existing Fluent instance. The
        IP address defaults to ``"127.0.0.1"``. You can also use the environment
        variable ``PYFLUENT_FLUENT_IP=<ip>`` to set this parameter.
    port : int, optional
        Port to listen on for an existing Fluent instance. You can use the
        environment variable ``PYFLUENT_FLUENT_PORT=<port>`` to set a default
        value.
    cleanup_on_exit : bool, optional
        Whether to shut down the connected Fluent session when PyFluent is
        exited, or the ``exit()`` method is called on the session instance,
        or if the session instance becomes unreferenced. The default is ``False``.
    start_transcript : bool, optional
        Whether to start streaming the Fluent transcript in the client. The
        default is ``True``. You can stop and start the streaming of the
        Fluent transcript subsequently via the method calls, ``transcript.start()``
        and ``transcript.stop()`` on the session object.
    server_info_filepath: str
        Path to server-info file written out by Fluent server. The default is
        ``None``. PyFluent uses the connection information in the file to
        connect to a running Fluent session.
    password : str, optional
        Password to connect to existing Fluent instance.
    start_watchdog: bool, optional
        When ``cleanup_on_exit`` is True, ``start_watchdog`` defaults to True,
        which means an independent watchdog process is run to ensure
        that any local Fluent connections are properly closed (or terminated if frozen) when Python process ends.

    Returns
    -------
    :obj:`~typing.Union` [:class:`Meshing<ansys.fluent.core.session_meshing.Meshing>`, \
    :class:`~ansys.fluent.core.session_pure_meshing.PureMeshing`, \
    :class:`~ansys.fluent.core.session_solver.Solver`, \
    :class:`~ansys.fluent.core.session_solver_icing.SolverIcing`]
        Session object.

    """
    ip, port, password = _get_server_info(server_info_filepath, ip, port, password)
    fluent_connection = FluentConnection(
        ip=ip,
        port=port,
        password=password,
        cleanup_on_exit=cleanup_on_exit,
        start_transcript=start_transcript,
    )
    new_session = _get_running_session_mode(fluent_connection)

    if start_watchdog is None and cleanup_on_exit:
        start_watchdog = True

    if start_watchdog:
        logger.info("Launching Watchdog for existing Fluent connection...")
        ip, port, password = _get_server_info(server_info_filepath, ip, port, password)
        watchdog.launch(os.getpid(), port, password, ip)

    return new_session(fluent_connection=fluent_connection)
