# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2026 ScyllaDB

from types import SimpleNamespace

import pytest

from sdcm.sct_events import Severity
from sdcm.stress import sc_verify_thread
from sdcm.stress.sc_verify_thread import ScVerifyThread, count_illegal, setup_guard_errors

PROFILE = "/tmp/sc-verify-profile-default.yaml"
LOADER_1 = f"cql-stress-sc-verify --profile {PROFILE} --mode both --pop seq=0..2047 --bulk-pop seq=1099511627776..1099522113535 --duration 240m"
LOADER_2 = f"cql-stress-sc-verify --profile {PROFILE} --mode both --pop seq=2048..4095 --bulk-pop seq=1099522113536..1099532599295 --duration 240m"


@pytest.mark.parametrize(
    "stress_cmds,round_robin,n_loaders,expected",
    [
        pytest.param([LOADER_1, LOADER_2], True, 2, [], id="clean"),
        pytest.param(["cassandra-stress write n=10"], False, 3, [], id="no-sc-verify-entry"),
        pytest.param([LOADER_1], False, 1, ["round_robin must be true"], id="no-round-robin"),
        pytest.param([LOADER_1], True, 2, ["1 cql-stress-sc-verify entries in stress_cmd for 2 loaders"], id="count"),
        pytest.param(
            [LOADER_1, LOADER_2.replace(PROFILE, "/tmp/sc-verify-profile-100gb.yaml")],
            True,
            2,
            ["every entry must use the same --profile"],
            id="profiles",
        ),
        pytest.param(
            [LOADER_1, LOADER_2.replace("seq=2048..4095", "seq=2047..4095")],
            True,
            2,
            ["--pop ranges overlap: (0, 2047) and (2047, 4095)"],
            id="pop-overlap",
        ),
        pytest.param(
            [LOADER_1, LOADER_2.replace("seq=1099522113536..", "seq=1099522113535..")],
            True,
            2,
            ["--bulk-pop ranges overlap"],
            id="bulk-pop-overlap",
        ),
        pytest.param(
            [
                LOADER_1,
                f"cql-stress-sc-verify --profile {PROFILE} --mode bulk --pop seq=0..2047 --bulk-pop seq=1099522113536..1099532599295",
            ],
            True,
            2,
            [],
            id="pop-ignored-on-bulk",
        ),
        pytest.param(
            [f"cql-stress-sc-verify --profile {PROFILE}", f"cql-stress-sc-verify --profile {PROFILE}"],
            True,
            2,
            ["--pop ranges overlap", "--bulk-pop ranges overlap"],
            id="defaults-overlap",
        ),
        pytest.param([LOADER_1.replace("seq=0..2047", "dist=uniform(1..9)")], True, 1, ["only seq=a..b"], id="not-seq"),
    ],
)
def test_setup_guard(stress_cmds, round_robin, n_loaders, expected):
    errors = setup_guard_errors(stress_cmds, round_robin, n_loaders)

    assert len(errors) == len(expected), errors
    for error, start in zip(errors, expected):
        assert start in error, errors


def test_count_illegal():
    output = """SCV {"t":"start","mode":"both"}
SCV {"t":"checked","file":"1","ok":50,"illegal":0,"unknown":0,"archive":null}
SCV {"t":"checked","file":"2","ok":48,"illegal":2,"unknown":0,"archive":"archive/2"}
Sealed 98 rows
SCV {"t":"end","exit":0}
"""

    assert count_illegal(output) == 2


class FakeEvent:
    def __init__(self, calls, **_):
        self.calls = calls
        self.severity = Severity.NORMAL
        self.errors = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.calls.append("publish")

    def add_error(self, errors):
        self.errors.extend(errors)


class FailedRun(Exception):
    def __init__(self, result):
        super().__init__(f"exit {result.exited}")
        self.result = result


class FakeDocker:
    def __init__(self, calls, exited, stdout):
        self.calls, self.exited, self.stdout = calls, exited, stdout

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.calls.append("docker rm")

    def send_files(self, src, dst, **_):
        self.calls.append(f"send {dst}")

    def run(self, cmd, **_):
        self.calls.append(f"run {cmd}")
        result = SimpleNamespace(exited=self.exited, stdout=self.stdout, stderr="error: boom", failed=bool(self.exited))
        if self.exited:
            raise FailedRun(result)
        return result


def make_thread(monkeypatch, tmp_path, calls, exited=0, stdout="", stress_cmds=(LOADER_1,), round_robin=True):
    monkeypatch.setattr(sc_verify_thread, "ScVerifyStressEvent", lambda **kwargs: FakeEvent(calls, **kwargs))
    monkeypatch.setattr(sc_verify_thread, "RemoteDocker", lambda *_, **__: FakeDocker(calls, exited, stdout))
    remoter = SimpleNamespace(
        run=lambda cmd, **_: SimpleNamespace(stdout="/home/scyllaadm/sc-verify-l1\n"),
        receive_files=lambda src, dst: calls.append(f"copy {src}"),
    )
    loader = SimpleNamespace(name="loader-1", logdir=str(tmp_path), remoter=remoter)
    loader_set = SimpleNamespace(nodes=[loader], get_db_auth=lambda: ("cassandra", "secret"))
    thread = ScVerifyThread(
        loader_set=loader_set,
        stress_cmd=stress_cmds[0],
        timeout=60,
        node_list=[SimpleNamespace(cql_address="10.0.0.1"), SimpleNamespace(cql_address="10.0.0.2")],
        round_robin=round_robin,
        params={"cluster_backend": "aws", "stress_cmd": list(stress_cmds)},
        stop_test_on_failure=False,
    )
    return thread, loader


@pytest.mark.parametrize(
    "exited,stdout,severity",
    [
        pytest.param(0, 'SCV {"t":"end","exit":0}', Severity.NORMAL, id="clean"),
        pytest.param(0, 'SCV {"t":"checked","file":"2","ok":1,"illegal":1,"unknown":0}', Severity.ERROR, id="illegal"),
        pytest.param(1, "", Severity.CRITICAL, id="violation"),
        pytest.param(2, "", Severity.CRITICAL, id="setup"),
        pytest.param(3, "", Severity.CRITICAL, id="tool"),
        pytest.param(137, "", Severity.WARNING, id="killed"),
    ],
)
def test_exit_code_to_severity(monkeypatch, tmp_path, exited, stdout, severity):
    calls = []
    thread, loader = make_thread(monkeypatch, tmp_path, calls, exited=exited, stdout=stdout)

    _, _, event = thread._run_stress(loader, loader_idx=1, cpu_idx=0)

    assert event.severity == severity
    # The evidence is on the runner before the event is published: a CRITICAL event stops the test at once.
    assert calls[-3:] == ["copy /home/scyllaadm/sc-verify-l1", "docker rm", "publish"]


def test_stress_cmd_gets_profile_nodes_and_credentials(monkeypatch, tmp_path):
    calls = []
    thread, loader = make_thread(monkeypatch, tmp_path, calls)

    thread._run_stress(loader, loader_idx=1, cpu_idx=0)

    assert f"send {PROFILE}" in calls
    assert f"run {LOADER_1} --nodes 10.0.0.1,10.0.0.2 --user cassandra --password secret" in calls


def test_failed_guard_does_not_start_the_tool(monkeypatch, tmp_path):
    calls = []
    thread, loader = make_thread(monkeypatch, tmp_path, calls, round_robin=False)

    _, result, event = thread._run_stress(loader, loader_idx=1, cpu_idx=0)

    assert result is None
    assert event.severity == Severity.CRITICAL
    assert calls == ["publish"]
