"""Gate tests for the manual SC perf jobs on the asymmetric 3/1/1 `preferred_rack` cluster.

Each job is resolved the way Jenkins and `sct.py lint-pipelines` resolve it, offline: the
jenkinsfile is parsed, turned into the SCT environment, and resolved into an `SCTConfiguration`
under the linter's cloud-API stubs.
"""

import functools
import json
import socket

import pytest
import yaml

from sdcm.sct_config import SCTConfiguration
from sdcm.utils.common import get_sct_root_path
from sdcm.utils.lint.env_builder import build_env
from sdcm.utils.lint.jenkins_parser import parse_jenkinsfile
from sdcm.utils.lint.validator import resolve_pipeline_config

SCT_ROOT = get_sct_root_path()
PERF_DIR = SCT_ROOT / "jenkins-pipelines/performance/branch-perf-v17/scylla-enterprise/perf-regression"
NEW_JOBS = {
    workload: PERF_DIR / f"scylla-enterprise-perf-regression-predefined-throughput-steps-cql-stress-{workload}"
    "-i8g-tablets-sc-asymmetric-preferred-rack.jenkinsfile"
    for workload in ("write", "read", "mixed")
}
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


@pytest.fixture(autouse=True)
def refuse_remote_connections(monkeypatch):
    """Keep job resolution offline: refuse every non-loopback connect and fail the test if one was tried.

    `resolve_job` caches, so a job is resolved inside the first test that asks for it; the guard
    therefore covers every test here. The failure is a `pytest.fail`, not an `AssertionError`, so
    an `xfail(raises=AssertionError)` test cannot absorb it, and a refusal the code under test
    swallows still fails the test.
    """
    attempts = []

    def guard(real_connect):
        def connect(self, address, *args, **kwargs):
            if isinstance(address, tuple) and address and address[0] not in LOCAL_HOSTS:
                attempts.append(address)
                raise RuntimeError(f"tried to connect to {address!r}")
            return real_connect(self, address, *args, **kwargs)

        return connect

    monkeypatch.setattr(socket.socket, "connect", guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(socket.socket.connect_ex))
    yield
    if attempts:
        pytest.fail(f"job resolution tried the network: {attempts!r}")


@functools.cache
def resolve_job(workload: str) -> SCTConfiguration:
    """Resolve a new job's SCT configuration once per process."""
    return resolve_pipeline_config(build_env(parse_jenkinsfile(NEW_JOBS[workload])))


def test_at_1_write_job_resolves_to_five_db_nodes_laid_out_3_1_1():
    config = resolve_job("write")

    assert config.get("n_db_nodes") == [5]
    assert config.get("simulated_racks") == 3
    assert config.get("n_db_nodes_per_rack") == [3, 1, 1]


def test_at_24_three_manual_predefined_steps_jobs():
    expected_params = {
        "backend": "aws",
        "region": "us-east-1",
        "provision_type": "on_demand",
        "test_name": "performance_regression_gradual_grow_throughput.PerformanceRegressionPredefinedStepsTest",
    }

    assert sorted(PERF_DIR.glob("*-asymmetric-preferred-rack.jenkinsfile")) == sorted(NEW_JOBS.values())
    for workload, path in NEW_JOBS.items():
        job = parse_jenkinsfile(path)
        assert job.pipeline_function == "perfRegressionParallelPipeline"
        assert {key: job.params.get(key) for key in expected_params} == expected_params
        assert json.loads(job.params["sub_tests"]) == [f"test_{workload}_gradual_increase_load"]


def test_at_25_new_jobs_on_no_trigger_matrix_or_schedule():
    trigger_files = [
        path
        for trigger_dir in (
            "configurations/triggers",
            "jenkins-pipelines/master-triggers",
            "jenkins-pipelines/oss/sct_triggers",
        )
        for path in (SCT_ROOT / trigger_dir).rglob("*")
        if path.is_file()
    ]
    job_names = [path.stem for path in NEW_JOBS.values()]

    assert trigger_files
    assert [
        (str(path), name) for path in trigger_files for name in job_names if name in path.read_text(encoding="utf-8")
    ] == []
    for path in NEW_JOBS.values():
        text = path.read_text(encoding="utf-8")
        assert [directive for directive in ("cron(", "triggers {", "pipelineTriggers") if directive in text] == []


def test_at_26_job_names_show_workload_and_setup():
    names = {workload: path.name for workload, path in NEW_JOBS.items()}
    every_jenkinsfile_name = [path.name for path in (SCT_ROOT / "jenkins-pipelines").rglob("*.jenkinsfile")]

    assert len(set(names.values())) == 3
    for workload, name in names.items():
        assert f"-{workload}-" in name
        assert "asymmetric-preferred-rack" in name
        assert every_jenkinsfile_name.count(name) == 1


@pytest.mark.parametrize("workload", NEW_JOBS)
def test_at_10_jobs_use_the_pinned_leader_aware_image(workload):
    pinned_image_file = SCT_ROOT / "configurations/stress_images/cql-stress-leader-aware.yaml"
    pinned = yaml.safe_load(pinned_image_file.read_text(encoding="utf-8"))["stress_image"]
    resolved = resolve_job(workload).get("stress_image")

    assert resolved["cql-stress-cassandra-stress"] == pinned["cql-stress-cassandra-stress"]


@pytest.mark.parametrize("workload", NEW_JOBS)
def test_at_6_leases_on_under_the_build_option_names(workload):
    expected = {
        "strong_consistency_enable_leases": True,
        "strong_consistency_lease_duration_in_ms": 1000,
        "strong_consistency_lease_clock_source": "clockbound",
    }
    config = resolve_job(workload)
    resolved = config.get("append_scylla_yaml")

    assert {key: resolved.get(key) for key in expected} == expected
    assert "strongly_consistent_raft_leader_" not in json.dumps(config.model_dump(exclude_none=True), default=str)


@pytest.mark.parametrize("workload", NEW_JOBS)
def test_at_7_tablet_balancing_stays_on(workload):
    assert not resolve_job(workload).get("disable_tablet_balancing")


@pytest.mark.parametrize("workload", NEW_JOBS)
def test_at_8_clockbound_rc0_built_on_aarch64_db_nodes(workload):
    """The offline resolution reports every instance as x86_64, so the instance type stands for aarch64."""
    config = resolve_job(workload)

    assert config.get("clockbound_version") == "3.0.0-rc.0"
    assert config.get("clockbound_from_source") is True
    assert config.get("instance_type_db") == "i8g.4xlarge"


def test_at_13_mixed_load_as_in_its_reference():
    config = resolve_job("mixed")

    assert config.get("n_loaders") == [4]
    assert config.get("perf_gradual_threads")["mixed"] == [1900]
    assert config.get("perf_gradual_throttle_steps")["mixed"] == ["50000", "150000", "300000", "450000", "unthrottled"]
    assert config.get("perf_gradual_step_duration")["mixed"] == "30m"


def test_at_11_write_load_100k_then_unthrottled():
    config = resolve_job("write")

    assert config.get("n_loaders") == [4]
    assert config.get("perf_gradual_threads")["write"] == [1000]
    assert config.get("perf_gradual_throttle_steps")["write"] == ["100000", "unthrottled"]
    assert config.get("perf_gradual_step_duration")["write"] == "15m"


def test_at_12_read_load_with_throttled_steps():
    config = resolve_job("read")

    assert config.get("n_loaders") == [6]
    assert config.get("perf_gradual_threads")["read"] == [620]
    assert config.get("perf_gradual_throttle_steps")["read"] == [
        "150000",
        "300000",
        "450000",
        "600000",
        "700000",
        "unthrottled",
    ]
    assert config.get("perf_gradual_step_duration")["read"] == "30m"


@pytest.mark.parametrize("workload", NEW_JOBS)
def test_at_3_keyspace_is_a_global_rack_list_over_three_racks(workload):
    statements = resolve_job(workload).get("pre_create_keyspace")
    required = (
        "CREATE KEYSPACE keyspace1",
        "'class': 'NetworkTopologyStrategy'",
        "'us-east-1': ['RACK0', 'RACK1', 'RACK2']",
        "consistency = 'global'",
        "tablets = {'enabled': true}",
    )

    assert len(statements) == 1
    assert [part for part in required if part not in statements[0]] == []
    assert "replication_factor" not in statements[0]


@pytest.mark.parametrize("workload", NEW_JOBS)
def test_at_4_preferred_rack_is_the_three_node_rack(workload):
    config = resolve_job(workload)
    layout = config.get("n_db_nodes_per_rack")
    rack = f"RACK{layout.index(max(layout))}"
    dc = parse_jenkinsfile(NEW_JOBS[workload]).params["region"]

    assert f"preferred_rack = {{'{dc}': '{rack}'}}" in config.get("pre_create_keyspace")[0]


@pytest.mark.parametrize("workload", NEW_JOBS)
def test_at_5_no_tablet_count_and_no_table_precreation(workload):
    statements = resolve_job(workload).get("pre_create_keyspace")
    forbidden = ("initial", "min_tablet_count", "pow2_count", "CREATE TABLE")

    assert len(statements) == 1
    assert [part for part in forbidden if part in statements[0]] == []


def test_at_2_topology_comes_from_one_shared_file():
    shared = "configurations/strong_consistency/asymmetric_preferred_rack.yaml"

    for path in NEW_JOBS.values():
        files = parse_jenkinsfile(path).test_config
        loaded = {name: yaml.safe_load((SCT_ROOT / name).read_text(encoding="utf-8")) or {} for name in files}
        sets_keyspace = [name for name in files if "pre_create_keyspace" in loaded[name]]

        assert [name for name in files if "n_db_nodes_per_rack" in loaded[name]] == [shared]
        assert len(sets_keyspace) == 2
        assert sets_keyspace[-1] == shared
    for workload in ("read", "mixed"):
        config = resolve_job(workload)

        assert config.get("n_db_nodes") == [5]
        assert config.get("simulated_racks") == 3
        assert config.get("n_db_nodes_per_rack") == [3, 1, 1]


@pytest.mark.parametrize("workload", NEW_JOBS)
def test_at_9_rack_aware_loader_off(workload):
    assert resolve_job(workload).get("rack_aware_loader") is False
