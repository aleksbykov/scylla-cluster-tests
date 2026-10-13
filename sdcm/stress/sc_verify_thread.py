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

"""Runs `cql-stress-sc-verify`, the linearizability checker for strongly consistent tables (SCYLLADB-4519)."""

import json
import logging
import os
import re
from pathlib import Path

from sdcm.remote.libssh2_client.exceptions import Failure
from sdcm.sct_events import Severity
from sdcm.sct_events.loaders import ScVerifyStressEvent
from sdcm.stress.base import DockerBasedStressThread, format_stress_cmd_error
from sdcm.utils.common import find_file_under_sct_dir
from sdcm.utils.docker_remote import RemoteDocker

LOGGER = logging.getLogger(__name__)

TOOL = "cql-stress-sc-verify"
# The tool's defaults for flags an entry may leave out (cql-stress `src/bin/cql-stress-sc-verify/cli.rs`).
DEFAULT_MODE = "both"
DEFAULT_POP = "seq=0..1023"
DEFAULT_BULK_POP = "seq=1099511627776..1099522113535"


def get_flag(stress_cmd: str, flag: str) -> str | None:
    if match := re.search(rf"(?:^|\s){flag}[ =](\S+)", stress_cmd):
        return match.group(1)
    return None


def setup_guard_errors(stress_cmds: list[str], round_robin: bool, n_loaders: int) -> list[str]:
    """Check the rules that keep two copies of the tool off the same rows.

    Two copies working the same rows time them with two different clocks and report false violations.

    Args:
        stress_cmds: the test's `stress_cmd` entries; entries of other tools are ignored.
        round_robin: the test's `round_robin`.
        n_loaders: the number of loader nodes.

    Returns:
        one message per broken rule; empty when the setup is fine or has no `cql-stress-sc-verify` entry.
    """
    cmds = [cmd for cmd in stress_cmds if cmd.strip().startswith(TOOL)]
    if not cmds:
        return []
    errors = []
    if not round_robin:
        errors.append("round_robin must be true, or every entry runs on every loader")
    if len(cmds) != n_loaders:
        errors.append(f"{len(cmds)} {TOOL} entries in stress_cmd for {n_loaders} loaders; give exactly one per loader")
    if len({get_flag(cmd, "--profile") for cmd in cmds}) > 1:
        errors.append("every entry must use the same --profile")
    for flag, default, modes in (
        ("--pop", DEFAULT_POP, {"verify", "both"}),
        ("--bulk-pop", DEFAULT_BULK_POP, {"bulk", "both"}),
    ):
        ranges = []
        for cmd in cmds:
            if (get_flag(cmd, "--mode") or DEFAULT_MODE) not in modes:
                continue
            value = get_flag(cmd, flag) or default
            if match := re.fullmatch(r"seq=(\d+)\.\.(\d+)", value):
                ranges.append((int(match.group(1)), int(match.group(2))))
            else:
                errors.append(f"{flag} {value}: only seq=a..b can be checked for overlap")
        ranges.sort()
        errors += [f"{flag} ranges overlap: {a} and {b}" for a, b in zip(ranges, ranges[1:]) if b[0] <= a[1]]
    return errors


def count_illegal(output: str) -> int:
    """Sum `illegal` over the `SCV checked` lines: rows the checker found not linearizable.

    They leave the exit code at 0, so the thread has to count them itself.
    """
    prefix = 'SCV {"t":"checked"'
    return sum(json.loads(line[4:]).get("illegal", 0) for line in output.splitlines() if line.startswith(prefix))


class ScVerifyThread(DockerBasedStressThread):
    DOCKER_IMAGE_PARAM_NAME = "stress_image.cql-stress-sc-verify"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history_dirs = []

    def build_stress_cmd(self, cmd_runner) -> str:
        stress_cmd = self.stress_cmd.strip()
        if profile := get_flag(stress_cmd, "--profile"):
            # SCT finds the profile by file name anywhere under its tree, so profile names must be unique.
            cmd_runner.send_files(find_file_under_sct_dir(filename=Path(profile).name), profile, delete_dst=True)
        if get_flag(stress_cmd, "--nodes") is None:
            stress_cmd += f" --nodes {','.join(node.cql_address for node in self.node_list)}"
        if (credentials := self.loader_set.get_db_auth()) and get_flag(stress_cmd, "--user") is None:
            stress_cmd += " --user {} --password {}".format(*credentials)
        return stress_cmd

    def _run_stress(self, loader, loader_idx, cpu_idx):
        os.makedirs(loader.logdir, exist_ok=True)
        log_id = self._build_log_file_id(loader_idx, cpu_idx, "")
        log_file_name = os.path.join(loader.logdir, f"{TOOL}-{log_id}.log")
        result = None
        with ScVerifyStressEvent(node=loader, stress_cmd=self.stress_cmd, log_file_name=log_file_name) as event:
            stress_cmds = self.params.get("stress_cmd") or []
            if errors := setup_guard_errors(
                stress_cmds=[stress_cmds] if isinstance(stress_cmds, str) else stress_cmds,
                round_robin=self.round_robin,
                n_loaders=len(self.loader_set.nodes),
            ):
                event.severity = Severity.CRITICAL
                event.add_error(errors)
                return loader, result, event

            # The history lives on the loader, not in the container, so it outlives `docker rm -f` in kill().
            history_dir = loader.remoter.run(
                f"mkdir -p $HOME/sc-verify-{log_id} && realpath $HOME/sc-verify-{log_id}", verbose=False
            ).stdout.strip()
            self.history_dirs.append((loader, history_dir))
            with RemoteDocker(
                loader,
                self.docker_image_name,
                command_line="-c 'tail -f /dev/null'",
                extra_docker_opts="--ulimit nofile=65536:65536 --network=host --security-opt seccomp=unconfined "
                f"--label shell_marker={self.shell_marker} --entrypoint /bin/bash -w / "
                f"-v {history_dir}:/history:z",
            ) as cmd_runner:
                stress_cmd = self.build_stress_cmd(cmd_runner)
                LOGGER.info("Stress command:\n%s", stress_cmd)
                try:
                    result = cmd_runner.run(
                        cmd=stress_cmd,
                        timeout=self.timeout + self.shutdown_timeout,
                        log_file=log_file_name,
                        retry=0,
                        timestamp_logs=True,
                    )
                    if illegal := count_illegal(result.stdout):
                        event.severity = Severity.ERROR
                        event.add_error([f"the checker found {illegal} rows not linearizable; see {history_dir}"])
                except Exception as exc:  # noqa: BLE001
                    self.configure_event_on_failure(stress_event=event, exc=exc)
                finally:
                    # A CRITICAL event stops the test at once, so the evidence is copied before the event is published.
                    self.copy_history(loader, history_dir)
        return loader, result, event

    def configure_event_on_failure(self, stress_event: ScVerifyStressEvent, exc: Exception | Failure):
        """Any failing exit code is CRITICAL: a correctness failure must never be lowered by `stop_test_on_failure`."""
        error_msg = format_stress_cmd_error(exc)
        if getattr(exc, "result", None) is not None and exc.result.exited == 137:
            stress_event.severity = Severity.WARNING
            error_msg = f"Stress killed by test/teardown\n{error_msg}"
        else:
            stress_event.severity = Severity.CRITICAL
        stress_event.add_error(errors=[error_msg])

    @staticmethod
    def copy_history(loader, history_dir: str):
        try:
            loader.remoter.receive_files(src=history_dir, dst=loader.logdir)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to copy the %s history %s from %s", TOOL, history_dir, loader.name)

    def kill(self):
        super().kill()
        for loader, history_dir in self.history_dirs:
            self.copy_history(loader, history_dir)
