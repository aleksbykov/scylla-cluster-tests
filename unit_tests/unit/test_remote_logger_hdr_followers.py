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

"""HDR loggers that start together on one node must each copy their own remote file.

A loader that runs two stress commands at once follows two HDR files. When every logger of the SCT process wrote
its `tail -f` into the same remote script, a logger could start the other one's tail, and its local HDR file held
the other command's histograms.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdcm.remote import LocalCmdRunner
from sdcm.utils.remote_logger import HDRHistogramFileLogger

TAGS = ("read_row-rt", "write_row-rt")


def wait_until(condition, timeout: float = 10) -> bool:
    """Poll `condition` until it holds or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def local_copy(logger: HDRHistogramFileLogger) -> str:
    """What the logger has copied to its local file so far."""
    target = Path(logger.target_log_file)
    return target.read_text(encoding="utf-8") if target.exists() else ""


@pytest.fixture
def setsid_gate(tmp_path, monkeypatch):
    """Hold each follower between writing its script and running it, until the test opens the gate.

    The remote command writes the script, then runs `setsid bash <script>`. A `setsid` earlier on PATH that waits
    here lets both loggers write their scripts before either runs one, the order in which the run crossed them.
    """
    gate = SimpleNamespace(bin=tmp_path / "bin", arrivals=tmp_path / "arrivals", file=tmp_path / "gate-open")
    gate.bin.mkdir()
    gate.arrivals.mkdir()
    setsid = gate.bin / "setsid"
    setsid.write_text(
        "#!/bin/bash\n"
        f'touch "{gate.arrivals}/$$"\n'
        f'while [ ! -e "{gate.file}" ]; do sleep 0.05; done\n'
        f'exec {shutil.which("setsid")} "$@"\n'
    )
    setsid.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gate.bin}:{os.environ['PATH']}")
    return gate


@pytest.fixture
def hdr_loggers(tmp_path, setsid_gate):
    """One HDR logger per stress command of one loader, both on the same node, each with its own remote file."""
    node = SimpleNamespace(remoter=LocalCmdRunner(), name="loader-1")
    loggers = {}
    for tag in TAGS:
        remote_file = tmp_path / f"remote-{tag}.hdr"
        remote_file.write_text(f"Tag={tag},0.0,10.0,0.5,HISTFAkAAAA\n")
        loggers[tag] = HDRHistogramFileLogger(
            node=node, remote_log_file=str(remote_file), target_log_file=str(tmp_path / f"local-{tag}.hdr")
        )
    yield loggers
    for logger in loggers.values():
        logger.stop()
    # A follower still held at the gate has no remote file in its command line, so stop() does not reach it
    subprocess.run(["pkill", "-f", str(tmp_path)], check=False)


def test_hdr_loggers_started_together_on_one_node_copy_only_their_own_file(hdr_loggers, setsid_gate):
    """Each local HDR file must hold only its own command's tag, or the latency summary counts the wrong commands."""
    for logger in hdr_loggers.values():
        logger.start()
    assert wait_until(lambda: len(list(setsid_gate.arrivals.iterdir())) == len(TAGS)), "the followers never started"

    setsid_gate.file.touch()

    assert wait_until(lambda: all("Tag=" in local_copy(logger) for logger in hdr_loggers.values())), (
        "no HDR data reached the local files"
    )
    for tag, logger in hdr_loggers.items():
        assert [held for held in TAGS if f"Tag={held}," in local_copy(logger)] == [tag], local_copy(logger)
