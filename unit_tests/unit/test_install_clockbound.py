import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from sdcm.cluster import BaseScyllaCluster
from sdcm.utils.common import get_sct_root_path
from sdcm.utils.curl import RETRY_ALL_ERRORS_PROBE


def test_install_clockbound_script_parses():
    node = MagicMock()
    node.distro.is_rhel_like = False
    node.remoter.run.return_value.stdout = "aarch64\n"

    BaseScyllaCluster._install_clockbound(node, "3.0.0-alpha.1")

    node.install_package.assert_called_once_with("rpm2cpio cpio")
    cmd = node.remoter.sudo.call_args.args[0]
    assert "releases/download/3.0.0-alpha.1/clockbound-3.0.0-alpha.1-1.aarch64.rpm" in cmd
    # The outer shell unquotes the script as on the node; the inner bash only parses it.
    result = subprocess.run(
        ["sh", "-c", cmd.replace("bash -cxe", "bash -n -c", 1)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


SHARED_TAIL_LINES = (
    "systemctl disable --now chrony || true",
    "systemctl disable --now chronyd || true",
    "systemctl disable --now ntp || true",
    "systemctl disable --now systemd-timesyncd || true",
    "ExecStart=/usr/bin/clockbound",
    "systemctl daemon-reload",
    "systemctl enable --now clockbound",
    "timeout 60 sh -c 'until ls /var/run/clockbound/* >/dev/null 2>&1; do sleep 1; done'",
)


def _ubuntu_aarch64_node():
    node = MagicMock()
    node.distro.is_rhel_like = False
    node.remoter.run.return_value.stdout = "aarch64\n"
    return node


def _parse_with_bash(cmd):
    """The outer shell unquotes the script as on the node; the inner bash only parses it."""
    return subprocess.run(
        ["sh", "-c", cmd.replace("bash -cxe", "bash -n -c", 1)], capture_output=True, text=True, check=False
    )


def test_at_21_source_build_commands():
    node = _ubuntu_aarch64_node()

    BaseScyllaCluster._install_clockbound(node, "3.0.0-rc.0", from_source=True)

    node.install_package.assert_called_once_with("gcc libc6-dev")
    cmd = node.remoter.sudo.call_args.args[0]
    assert "--git https://github.com/aws/clock-bound --tag 3.0.0-rc.0" in cmd
    assert "cargo install --locked" in cmd
    assert "--features daemon --bin clockbound" in cmd
    assert "--target" not in cmd  # built for the node's own architecture
    assert "install -m 0755 /tmp/clockbound-build/out/bin/clockbound /usr/bin/clockbound" in cmd
    assert "releases/download" not in cmd
    for line in SHARED_TAIL_LINES:
        assert line in cmd
    assert node.remoter.sudo.call_args.kwargs == {"verbose": True, "timeout": 1800}
    result = _parse_with_bash(cmd)
    assert result.returncode == 0, result.stderr


def test_at_21_source_build_pins_rust_1_99_1():
    node = _ubuntu_aarch64_node()

    BaseScyllaCluster._install_clockbound(node, "3.0.0-rc.0", from_source=True)

    assert "--default-toolchain 1.99.1" in node.remoter.sudo.call_args.args[0]


def test_at_21_source_build_script_survives_the_outer_shell():
    """The node's login shell expands the double-quoted script before bash runs it."""
    node = _ubuntu_aarch64_node()

    BaseScyllaCluster._install_clockbound(node, "3.0.0-rc.0", from_source=True)

    cmd = node.remoter.sudo.call_args.args[0]
    assert cmd.startswith('bash -cxe "')
    assert cmd.endswith('"')
    script = cmd.removeprefix('bash -cxe "').removesuffix('"')
    assert '"' not in script
    assert "$" not in script.replace(RETRY_ALL_ERRORS_PROBE, "")


def test_at_21_cluster_config_selects_the_source_build():
    cluster = MagicMock()
    cluster.params = {"clockbound_version": "3.0.0-rc.0", "clockbound_from_source": True}
    node = _ubuntu_aarch64_node()

    BaseScyllaCluster._setup_clockbound(cluster, node)

    cluster._install_clockbound.assert_called_once_with(node, "3.0.0-rc.0", from_source=True)


def test_at_22_failed_build_fails_setup_without_fallback():
    node = _ubuntu_aarch64_node()
    build_failure = RuntimeError("cargo install exited with status 101")
    node.remoter.sudo.side_effect = build_failure

    with pytest.raises(RuntimeError) as raised:
        BaseScyllaCluster._install_clockbound(node, "3.0.0-rc.0", from_source=True)

    assert raised.value is build_failure
    node.remoter.sudo.assert_called_once()
    sent_to_node = repr(node.mock_calls)
    assert "releases/download" not in sent_to_node
    assert "3.0.0-alpha" not in sent_to_node


def test_at_23_rpm_install_builds_nothing_from_source():
    node = _ubuntu_aarch64_node()

    BaseScyllaCluster._install_clockbound(node, "3.0.0-alpha.1")

    node.install_package.assert_called_once_with("rpm2cpio cpio")
    cmd = node.remoter.sudo.call_args.args[0]
    assert (
        "https://github.com/aws/clock-bound/releases/download/3.0.0-alpha.1/clockbound-3.0.0-alpha.1-1.aarch64.rpm"
        in cmd
    )
    result = _parse_with_bash(cmd)
    assert result.returncode == 0, result.stderr
    sent_to_node = repr(node.mock_calls)
    for source_build_word in ("rustup", "cargo", "gcc"):
        assert source_build_word not in sent_to_node
    assert "timeout" not in node.remoter.sudo.call_args.kwargs


def test_at_23_cluster_config_defaults_to_the_rpm():
    cluster = MagicMock()
    cluster.params = {"clockbound_version": "3.0.0-alpha.1"}
    node = _ubuntu_aarch64_node()

    BaseScyllaCluster._setup_clockbound(cluster, node)

    cluster._install_clockbound.assert_called_once_with(node, "3.0.0-alpha.1", from_source=False)
    defaults = yaml.safe_load((Path(get_sct_root_path()) / "defaults" / "test_default.yaml").read_text())
    assert defaults["clockbound_from_source"] is False
