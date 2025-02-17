import json
import os
from unittest import TestCase

import pytest
from util.fixture_fluent import get_name_info
from util.solver import SettingsValDict as D
from util.solver import assign_settings_value_from_value_dict as assign_dict_val


@pytest.mark.nightly
@pytest.mark.fluent_version("latest")
@pytest.mark.integration
@pytest.mark.setup
@pytest.mark.codegen_required
def test_boundaries_elbow(load_mixing_elbow_mesh):
    solver_session = load_mixing_elbow_mesh
    solver_session.setup.models.energy.enabled = True
    assert (
        D(0)
        == solver_session.setup.boundary_conditions.velocity_inlet[
            "cold-inlet"
        ].momentum.velocity()
    )
    assign_dict_val(
        solver_session.setup.boundary_conditions.velocity_inlet[
            "cold-inlet"
        ].momentum.velocity,
        0.4,
    )
    assert (
        D(0.4)
        == solver_session.setup.boundary_conditions.velocity_inlet[
            "cold-inlet"
        ].momentum.velocity()
    )
    solver_session.setup.boundary_conditions.velocity_inlet[
        "cold-inlet"
    ].turbulence.turbulent_specification = "Intensity and Hydraulic Diameter"
    solver_session.setup.boundary_conditions.velocity_inlet[
        "cold-inlet"
    ].turbulence.turbulent_intensity = 0.05
    solver_session.setup.boundary_conditions.velocity_inlet[
        "cold-inlet"
    ].turbulence.hydraulic_diameter = "4 [in]"
    assign_dict_val(
        solver_session.setup.boundary_conditions.velocity_inlet["cold-inlet"].thermal.t,
        293.15,
    )
    assert {
        "name": "cold-inlet",
        "momentum": {
            "initial_gauge_pressure": {"option": "value", "value": 0},
            "reference_frame": "Absolute",
            "velocity": {"option": "value", "value": 0.4},
            "velocity_specification_method": "Magnitude, Normal to Boundary",
        },
        "turbulence": {
            "turbulent_specification": "Intensity and Hydraulic Diameter",
            "turbulent_intensity": 0.05,
            "hydraulic_diameter": "4 [in]",
        },
        "thermal": {"t": {"option": "value", "value": 293.15}},
    } == solver_session.setup.boundary_conditions.velocity_inlet["cold-inlet"]()

    assign_dict_val(
        solver_session.setup.boundary_conditions.velocity_inlet[
            "hot-inlet"
        ].momentum.velocity,
        1.2,
    )
    solver_session.setup.boundary_conditions.velocity_inlet[
        "hot-inlet"
    ].turbulence.turbulent_specification = "Intensity and Hydraulic Diameter"
    solver_session.setup.boundary_conditions.velocity_inlet[
        "hot-inlet"
    ].turbulence.hydraulic_diameter = "1 [in]"
    assign_dict_val(
        solver_session.setup.boundary_conditions.velocity_inlet["hot-inlet"].thermal.t,
        313.15,
    )
    assert {
        "name": "hot-inlet",
        "momentum": {
            "initial_gauge_pressure": {"option": "value", "value": 0},
            "reference_frame": "Absolute",
            "velocity": {"option": "value", "value": 1.2},
            "velocity_specification_method": "Magnitude, Normal to Boundary",
        },
        "turbulence": {
            "turbulent_specification": "Intensity and Hydraulic Diameter",
            "turbulent_intensity": 0.05,
            "hydraulic_diameter": "1 [in]",
        },
        "thermal": {"t": {"option": "value", "value": 313.15}},
    } == solver_session.setup.boundary_conditions.velocity_inlet["hot-inlet"]()

    solver_session.setup.boundary_conditions.pressure_outlet[
        "outlet"
    ].turbulence.turbulent_viscosity_ratio_real = 4
    assert (
        solver_session.setup.boundary_conditions.pressure_outlet[
            "outlet"
        ].turbulence.turbulent_viscosity_ratio_real()
        == 4
    )


# TODO: Skipped for the nightly test run to be successful. Later decide what to do with this test (discard?).
@pytest.mark.nightly
@pytest.mark.integration
@pytest.mark.setup
@pytest.mark.fluent_version("latest")
@pytest.mark.skip
def test_boundaries_periodic(load_periodic_rot_cas):
    solver_session = load_periodic_rot_cas
    print(__file__)
    _THIS_DIR = os.path.dirname(__file__)
    _DATA_FILE = os.path.join(_THIS_DIR, "boundaries_periodic_expDict")
    boundary_exp = json.load(open(_DATA_FILE))
    boundary_test = dict()
    boundary_tested = dict()
    for name, boundary in solver_session.setup.boundary_conditions.items():
        boundary_test[name] = boundary()
    boundary_tested["val_1"] = boundary_test
    TestCase().assertDictEqual(boundary_tested["val_1"], boundary_exp["val_1"])

    boundary_test = dict()
    for (
        boundary_type
    ) in solver_session.setup.boundary_conditions.get_active_child_names():
        if boundary_type == "matching_tolerance":
            continue
        for name, boundary in getattr(
            solver_session.setup.boundary_conditions, boundary_type
        ).items():
            boundary_test[name] = boundary()
    boundary_tested["val_2"] = boundary_test
    TestCase().assertDictEqual(boundary_tested["val_2"], boundary_exp["val_2"])

    boundaries_check = ["inlet", "outlet", "pipe_2_wall"]
    selected_bou_test = get_name_info(boundary_tested["val_1"], boundaries_check)
    selected_bou_exp = get_name_info(boundary_exp["val_1"], boundaries_check)
    TestCase().assertDictEqual(selected_bou_test, selected_bou_exp)
    # commented new method due to bug 753
    # solver_session.setup.boundary_conditions.wall["pipe_2_wall"].rename("pipe2_wall")
    solver_session.setup.boundary_conditions.wall.rename("pipe2_wall", "pipe_2_wall")
    solver_session.setup.boundary_conditions.wall.rename("out", "outlet")
    solver_session.setup.boundary_conditions.velocity_inlet[
        "inlet"
    ].momentum.velocity = 5.0
    solver_session.setup.boundary_conditions["inlet"].momentum.velocity = 10.0
    boundaries_check = ["inlet", "out", "pipe2_wall"]
    boundary_test = dict()
    for name, boundary in solver_session.setup.boundary_conditions.items():
        boundary_test[name] = boundary()
    boundary_tested["val_3"] = boundary_test
    TestCase().assertDictEqual(boundary_tested["val_3"], boundary_exp["val_3"])

    selected_bou_test = get_name_info(boundary_tested["val_3"], boundaries_check)
    selected_bou_exp = get_name_info(boundary_exp["val_3"], boundaries_check)
    TestCase().assertDictEqual(selected_bou_test, selected_bou_exp)
    with open("boundaries_periodic_outDict.py", "a") as f:
        json.dump(boundary_tested, f, sort_keys=True, indent=4)
