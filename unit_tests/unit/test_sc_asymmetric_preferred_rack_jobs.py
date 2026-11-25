"""Gate tests for the manual SC perf jobs on the asymmetric 3/1/1 `preferred_rack` cluster.

Each job is resolved the way Jenkins and `sct.py lint-pipelines` resolve it, offline: the
jenkinsfile is parsed, turned into the SCT environment, and resolved into an `SCTConfiguration`
under the linter's cloud-API stubs.
"""

import functools

import pytest

from sdcm.sct_config import SCTConfiguration
from sdcm.utils.common import get_sct_root_path
from sdcm.utils.lint.env_builder import build_env
from sdcm.utils.lint.jenkins_parser import parse_jenkinsfile
from sdcm.utils.lint.validator import resolve_pipeline_config

PERF_DIR = get_sct_root_path() / "jenkins-pipelines/performance/branch-perf-v17/scylla-enterprise/perf-regression"
NEW_JOBS = {
    workload: PERF_DIR / f"scylla-enterprise-perf-regression-predefined-throughput-steps-cql-stress-{workload}"
    "-i8g-tablets-sc-asymmetric-preferred-rack.jenkinsfile"
    for workload in ("write", "read", "mixed")
}


@functools.cache
def resolve_job(workload: str) -> SCTConfiguration:
    """Resolve a new job's SCT configuration once per process."""
    return resolve_pipeline_config(build_env(parse_jenkinsfile(NEW_JOBS[workload])))


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the write job has no 3/1/1 rack layout yet")
def test_at_1_write_job_resolves_to_five_db_nodes_laid_out_3_1_1():
    config = resolve_job("write")

    assert config.get("n_db_nodes") == [5]
    assert config.get("simulated_racks") == 3
    assert config.get("n_db_nodes_per_rack") == [3, 1, 1]
