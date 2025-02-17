import os
import sys

import pytest
from util.solver_workflow import new_solver_session  # noqa: F401

import ansys.fluent.core as pyfluent
from ansys.fluent.core import examples


@pytest.mark.nightly
@pytest.mark.fluent_version(">=23.2")
@pytest.mark.skipif(
    os.getenv("PYFLUENT_LAUNCH_CONTAINER") == "1"
    or os.getenv("PYFLUENT_LAUNCH_CONTAINER") != "1"
    and sys.platform.startswith("linux"),
    reason="Linux Fluent server issues, see #1490",
)
def test_batch_ops_create_mesh(new_solver_session):
    solver = new_solver_session
    case_filename = examples.download_file(
        "mixing_elbow.cas.h5", "pyfluent/mixing_elbow"
    )
    with pyfluent.BatchOps(solver):
        solver.file.read_case(file_name=case_filename)
        solver.results.graphics.mesh["mesh-1"] = {}
        assert not solver.scheme_eval.scheme_eval("(case-valid?)")
        assert "mesh-1" not in solver.results.graphics.mesh.get_object_names()
    assert solver.scheme_eval.scheme_eval("(case-valid?)")
    assert "mesh-1" in solver.results.graphics.mesh.get_object_names()


@pytest.mark.nightly
@pytest.mark.fluent_version(">=23.2")
def test_batch_ops_create_mesh_and_access_fails(new_solver_session):
    solver = new_solver_session
    case_filename = examples.download_file(
        "mixing_elbow.cas.h5", "pyfluent/mixing_elbow"
    )
    with pytest.raises(KeyError):
        with pyfluent.BatchOps(solver):
            solver.file.read_case(file_name=case_filename)
            solver.results.graphics.mesh["mesh-1"] = {}
            solver.results.graphics.mesh["mesh-1"].surfaces_list = ["wall-elbow"]
    assert not solver.scheme_eval.scheme_eval("(case-valid?)")
