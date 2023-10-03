import logging
from typing import Iterable

from sdcm.cluster import BaseNode
from sdcm.wait import wait_for
from sdcm.sct_events.group_common_events import decorate_with_context, ignore_ycsb_connection_refused

LOGGER = logging.getLogger(__name__)


class RaftException(Exception):
    """Raise if raft feature mode differs on nodes"""


def validate_raft_on_nodes(nodes: list["BaseNode"]) -> None:
    LOGGER.debug("Check that raft is enabled on all the nodes")
    raft_enabled_on_nodes = [node.raft.is_enabled for node in nodes]
    if len(set(raft_enabled_on_nodes)) != 1:
        raise RaftException("Raft configuration is not the same on all the nodes")

    if not all(raft_enabled_on_nodes):
        LOGGER.debug("Raft feature is disabled)")
        return
    LOGGER.debug("Raft feature is enabled)")
    LOGGER.debug("Check raft feature status on nodes")
    nodes_raft_status = []
    for node in nodes:
        if raft_ready := node.raft.is_ready():
            nodes_raft_status.append(raft_ready)
            continue
        LOGGER.error("Node %s has raft status: %s", node.name, node.raft.get_status())
    if not all(nodes_raft_status):
        raise RaftException("Raft is not ready")

    LOGGER.debug("Raft is ready!")


class NodeBootStrapAbortHandler:

    def __init__(self, node: BaseNode, verification_node: BaseNode):
        self.node = node
        self.verification_node: BaseNode = verification_node

    @property
    def host_id_searcher(self) -> Iterable[str]:
        return self.node.follow_system_log(patterns=['Setting local host id to'])

    def set_wait_stop_event(self):
        if not self.node.wait_for_stop_event.is_set():
            self.node.wait_for_stop_event.set()
        LOGGER.debug("Stop event was set for node %s", self.node.name)

    def start_bootstrap(self, timeout=3600):
        try:
            LOGGER.debug("Starting bootstrap process %s", self.node.name)
            self.node.parent_cluster.node_setup(self.node, verbose=True, timeout=timeout)
        except Exception as exc:  # pylint: disable=broad-except
            LOGGER.error("Setup failed for node %s with err %s", self.node.name, exc)
        finally:
            self.set_wait_stop_event()
        LOGGER.debug("Node %s was bootstrapped", self.node.name)

    def abort_bootstrap(self, log_message: str, timeout: int = 600):
        LOGGER.debug("Stop bootsrap process after log message: '%s'", log_message)
        log_follower = self.node.follow_system_log(patterns=[log_message])
        try:
            wait_for(func=lambda: list(log_follower), step=5,
                     text="Waiting log message to stop scylla...",
                     timeout=timeout,
                     throw_exc=True,
                     stop_event=self.node.wait_for_stop_event)
            self.node.stop_scylla()
            LOGGER.info("Scylla was stopped succesfully on node %s", self.node.name)
        except Exception as exc:  # pylint: disable=broad-except
            LOGGER.warning("Abort was failed on node %s with errof %s", self.node.name, exc)
        finally:
            self.set_wait_stop_event()

    def clean_unbootstrapped_node(self):
        node_host_ids = []
        if found_stings := list(self.host_id_searcher):
            for line in found_stings:
                new_node_host_id = line.split(" ")[-1].strip()
                LOGGER.debug("Node %s has host id: %s in log", self.node.name, new_node_host_id)
                node_host_ids.append(new_node_host_id)
        self.node.log.debug("New host was not properly bootstrapped. Terminate it")
        self._terminate_node()
        self.verification_node.raft.clean_group0_garbage(raise_exception=True)
        if node_host_ids:
            for host_id in node_host_ids:
                self.verification_node.run_nodetool(
                    f"removenode {host_id}", ignore_status=True, retry=3, warning_event_on_exception=True)

        assert self.verification_node.raft.is_cluster_topology_consistent(), \
            "Group0, Token Ring and number of node in cluster are differs. Check logs"
        self.node.parent_cluster.check_nodes_up_and_normal()
        LOGGER.info("Failed bootstrapped node %s removed. Cluster is in initial state", self.node.name)

    @decorate_with_context(ignore_ycsb_connection_refused)
    def _terminate_node(self):
        self.node.parent_cluster.terminate_node(self)
        self.node.test_config.tester_obj.monitors.reconfigure_scylla_monitoring()
