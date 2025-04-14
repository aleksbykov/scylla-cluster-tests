import random
import warnings
from typing import Tuple, List


from longevity_test import LongevityTest
from sdcm.cluster import BaseNode, NodeSetupFailed, NodeSetupTimeout
from sdcm.exceptions import ReadBarrierErrorException
from sdcm.stress_thread import CassandraStressThread
from sdcm.utils.decorators import optional_stage, skip_on_capacity_issues
from sdcm.utils.replication_strategy_utils import NetworkTopologyReplicationStrategy
from sdcm.utils.adaptive_timeouts import Operations, adaptive_timeout
from sdcm.sct_events.system import InfoEvent

warnings.filterwarnings(action="ignore", message="unclosed", category=ResourceWarning)


class TestClusterQuorum(LongevityTest):
    """Test for procedure:
     TODO: link to 'Preventing Quorum Loss in Symmetrical Multi-DC Clusters'
     """

    def assert_multidc_config(self):
        data_nodes_config = [int(n) for n in self.params.get('n_db_nodes').split()]
        zero_token_nodes_config = [int(n) for n in self.params.get('n_db_zero_token_nodes').split()]
        assert zero_token_nodes_config == [0, 0, 0], f"Zero node configuration is incorrect: {zero_token_nodes_config}"
        assert data_nodes_config[0] == data_nodes_config[1], f"Data node config is not symmetric: {data_nodes_config}"
        assert data_nodes_config[-1] == 0, f"Arbiter DC should not contain data node: {data_nodes_config[-1]}"

    def test_raft_quorum_after_add_arbitor_dc_with_zero_node(self):
        if not self.db_cluster.nodes[0].raft.is_consistent_topology_changes_enabled:
            raise Exception("Raft consistent topology changes feature have to be enabled")

        self.assert_multidc_config()
        arbiter_dcx = len(self.params.get('n_db_nodes').split()) - 1  # choose latest dc with 0 nodes as arbiter dc

        InfoEvent("Prepare keyspace1 with cassandra-stress command").publish()
        self.prewrite_db_with_data()

        region_dc_mapping = self.db_cluster.get_datacenter_name_per_region(db_nodes=self.db_cluster.data_nodes)
        data_nodes_per_region = self.db_cluster.nodes_by_region(nodes=self.db_cluster.data_nodes)

        InfoEvent("Reconfigure system and user-defined keyspaces").publish()
        self.reconfigure_keyspaces_to_use_network_topology_strategy(
            keyspaces=["system_distributed", "system_traces", "keyspace1"],
            replication_factors={region_dc_mapping[region]: len(
                data_nodes_per_region[region]) for region in data_nodes_per_region}
        )

        InfoEvent("Running repair on all data nodes").publish()
        for node in self.db_cluster.data_nodes:
            node.run_nodetool(sub_cmd="repair -pr", publish_event=True)

        regions = list(region_dc_mapping.keys())
        dead_region = regions[0]
        alive_region = regions[1]

        InfoEvent("Run backgroud workload")
        read_thread, write_thread = self.start_background_stress_commands(
            node_ips=[n.cql_address for n in data_nodes_per_region[alive_region]])

        node = data_nodes_per_region[alive_region][0]
        InfoEvent(f"Voters nodes: {node.raft.get_group0_members()}").publish()

        InfoEvent(f"Simulate dc {region_dc_mapping[dead_region]} is down").publish()
        for node in data_nodes_per_region[dead_region]:
            node.stop_scylla()

        InfoEvent(f"Voters nodes: {node.raft.get_group0_members()}").publish()

        InfoEvent("Validate raft quorum is lost").publish()
        verification_node = data_nodes_per_region[alive_region][0]
        assert not self.is_raft_quorum_exists(
            verification_node), "Quorum is preserved. Cluster DC are not symmetric, not all nodes were stopped. Check logs for further investigation"

        InfoEvent(f"Start all nodes in dc {region_dc_mapping[dead_region]}").publish()
        for node in data_nodes_per_region[dead_region]:
            node.start_scylla()
        self.db_cluster.wait_all_nodes_un()

        # Add new dc with zero node only
        InfoEvent("Add arbitter dc with single zero node").publish()
        arbitor_dc_node = self.add_zero_node_to_dc(dc_idx=arbiter_dcx)
        assert arbitor_dc_node.region == self.params.region_names[
            arbiter_dcx], f"Zero toke node {arbitor_dc_node.name} was added to region {arbitor_dc_node.dc_idx}:{arbitor_dc_node.region} but expected to {self.params.region_names[arbiter_dcx]}"

        InfoEvent(f"Simulate dc {region_dc_mapping[dead_region]} is down").publish()
        hostid_dead_node_mapping = {}
        for node in data_nodes_per_region[dead_region]:
            hostid_dead_node_mapping[node.host_id] = node
            node.stop_scylla()

        InfoEvent("Validate raft quorum is preserved").publish()
        assert self.is_raft_quorum_exists(verification_node=arbitor_dc_node), "No raft quorum, Check the logs"

        InfoEvent("Replace all data nodes in dead DC").publish()
        self.replace_nodes_by_host_id(dead_node_mapping=hostid_dead_node_mapping, verification_node=arbitor_dc_node)

        InfoEvent("Rebuild and repair data on new nodes").publish()
        data_nodes_per_region = self.db_cluster.nodes_by_region(nodes=self.db_cluster.data_nodes)

        self.log.info("Running rebuild  in restored DC")
        for node in data_nodes_per_region[dead_region]:
            node.run_nodetool(sub_cmd=f"rebuild -- {region_dc_mapping[alive_region]}", publish_event=True)

        self.log.info("Running repair on all data nodes")
        for node in self.db_cluster.data_nodes:
            node.run_nodetool(sub_cmd="repair -pr", publish_event=True)

        InfoEvent("Verify data on restored DC nodes").publish()
        self.verify_data_can_be_read_from_dc(verification_node=data_nodes_per_region[dead_region][0])
        # wait for stress to complete
        self.verify_stress_thread(thread_pool=read_thread)
        self.verify_stress_thread(thread_pool=write_thread)

        self.log.info("Test completed.")

    def test_raft_quorum_after_add_zeronode_to_single_dc(self):
        self.log.info("Start test with single dc and zero node")
        assert len(self.params.total_db_nodes) == 1, "Single DC should be configured"

        self.prewrite_db_with_data()

        # Stop half of nodes in dc and check that raft quorum is lost
        num_of_data_nodes = self.params.get("n_db_nodes")
        lost_quorum_num = num_of_data_nodes // 2 if num_of_data_nodes % 2 == 0 else (num_of_data_nodes // 2) + 1
        dead_nodes, alive_nodes = self.db_cluster.data_nodes[:
                                                             lost_quorum_num], self.db_cluster.data_nodes[lost_quorum_num:]

        self.log.info("Stop half nodes to simulate quorum lost")
        for node in dead_nodes:
            node.stop_scylla()

        self.log.info("Assert that quorum is lost")
        verification_node = random.choice(alive_nodes)
        InfoEvent("Validate raft quorom is lost").publish()
        assert not self.is_raft_quorum_exists(
            verification_node=verification_node), "Raft quorum preserved, Check the logs"

        # Start stopped nodes
        self.log.info("Start stopped nodes and restore cluster")
        for node in dead_nodes:
            node.start_scylla()
        self.db_cluster.wait_all_nodes_un()

        # Add new dc with zero node only
        self.log.info("Add new zero node")
        zero_token_node = self.add_zero_node_to_dc()

        hostid_dead_node_mapping = {}
        for node in dead_nodes:
            hostid_dead_node_mapping[node.host_id] = node
            node.stop_scylla()

        InfoEvent("Validate raft quorum is preserved").publish()
        assert self.is_raft_quorum_exists(verification_node=zero_token_node), "No raft quorum, Check the logs"

        self.replace_nodes_by_host_id(dead_node_mapping=hostid_dead_node_mapping, verification_node=verification_node)

        self.log.info("Running repair on all data nodes")
        for node in self.db_cluster.data_nodes:
            node.run_nodetool(sub_cmd="repair -pr", publish_event=True)

        stress_cmd = self.params.get('verify_data_after_entire_test')
        end_stress = self.run_stress_thread(stress_cmd=stress_cmd, stats_aggregate_cmds=False, round_robin=False)

        self.verify_stress_thread(thread_pool=end_stress)

    def reconfigure_keyspaces_to_use_network_topology_strategy(self, keyspaces: List[str], replication_factors: dict[str, int]) -> None:
        node = self.db_cluster.nodes[0]
        self.log.info("Reconfiguring keyspace Replication Strategy")
        network_topology_strategy = NetworkTopologyReplicationStrategy(
            **replication_factors)
        for keyspace in keyspaces:
            cql = f"ALTER KEYSPACE {keyspace} WITH replication = {network_topology_strategy}"
            node.run_cqlsh(cql)
        self.log.info("Replication Strategies for {} reconfigured".format(keyspaces))

    @optional_stage('prepare_write')
    def prewrite_db_with_data(self) -> None:
        self.log.info("Prewriting database...")
        stress_cmd = self.params.get('prepare_write_cmd')
        pre_thread = self.run_stress_thread(stress_cmd=stress_cmd, stats_aggregate_cmds=False, round_robin=False)
        self.verify_stress_thread(thread_pool=pre_thread)
        self.log.info("Database pre write completed")

    def add_zero_node_to_dc(self, dc_idx: int = 0) -> BaseNode:
        if not self.params.get("use_zero_nodes"):
            raise Exception("Zero node support should be enabled")
        self.log.info("Adding new node")
        new_node = self.db_cluster.add_nodes(1, dc_idx=dc_idx, enable_auto_bootstrap=True, is_zero_node=True)[0]
        self.db_cluster.wait_for_init(node_list=[new_node], timeout=900,
                                      check_node_health=True)
        self.db_cluster.wait_for_nodes_up_and_normal(nodes=[new_node])
        self.monitors.reconfigure_scylla_monitoring()

        return new_node

    @optional_stage('post_test_load')
    def verify_data_can_be_read_from_dc(self, verification_node: BaseNode) -> None:
        self.log.info("Verifying if data has been transferred successfully to the new DC")
        stress_cmd = self.params.get('verify_data_after_entire_test') + f" -node {verification_node.ip_address}"
        end_stress = self.run_stress_thread(stress_cmd=stress_cmd, stats_aggregate_cmds=False, round_robin=False)
        self.verify_stress_thread(thread_pool=end_stress)

    def replace_cluster_node(self, verification_node: BaseNode,
                             host_id: str | None = None,
                             dc_idx: int = 0,
                             ignore_dead_node_host_ids: str = "",
                             timeout: int | float = 3600 * 8) -> BaseNode:
        """When old_node_ip or host_id are not None then replacement node procedure is initiated"""
        self.log.info("Adding new node to cluster...")
        new_node: BaseNode = skip_on_capacity_issues(self.db_cluster.add_nodes)(
            count=1, dc_idx=dc_idx, enable_auto_bootstrap=True)[0]
        self.monitors.reconfigure_scylla_monitoring()
        with new_node.remote_scylla_yaml() as scylla_yaml:
            scylla_yaml.ignore_dead_nodes_for_replace = ignore_dead_node_host_ids
        # since we need this logic before starting a node, and in `use_preinstalled_scylla: false` case
        # scylla is not yet installed or target node was terminated, we should use an alive node without nemesis for version,
        # it should be up and with scylla executable available

        new_node.replacement_host_id = host_id

        try:
            with adaptive_timeout(Operations.NEW_NODE, node=verification_node, timeout=timeout):
                self.db_cluster.wait_for_init(node_list=[new_node], timeout=timeout, check_node_health=False)
            self.db_cluster.clean_replacement_node_options(new_node)
            self.db_cluster.set_seeds()
            self.db_cluster.update_seed_provider()
        except (NodeSetupFailed, NodeSetupTimeout):
            self.log.warning("TestConfig of the '%s' failed, removing it from list of nodes" % new_node)
            self.db_cluster.nodes.remove(new_node)
            self.log.warning("Node will not be terminated. Please terminate manually!!!")
            raise

        self.db_cluster.wait_for_nodes_up_and_normal(nodes=[new_node], verification_node=verification_node)
        new_node.wait_node_fully_start()
        with new_node.remote_scylla_yaml() as scylla_yaml:
            scylla_yaml.ignore_dead_nodes_for_replace = ""

        return new_node

    def replace_nodes_by_host_id(self, dead_node_mapping: dict[str, BaseNode], verification_node: BaseNode):
        host_ids = list(dead_node_mapping.keys())
        new_nodes = []
        for i, host_id in enumerate(host_ids):
            self.log.info("Replace node %s with host_id: %s", dead_node_mapping[host_id].name, host_id)
            new_nodes.append(self.replace_cluster_node(verification_node,
                                                       dc_idx=dead_node_mapping[host_id].dc_idx,
                                                       host_id=host_id,
                                                       ignore_dead_node_host_ids=",".join(host_ids[i+1:])))

        for node in dead_node_mapping.values():
            self.db_cluster.terminate_node(node)

        self.db_cluster.wait_all_nodes_un()

    def is_raft_quorum_exists(self, verification_node: BaseNode) -> bool:
        try:
            verification_node.raft.call_read_barrier()
            return True
        except ReadBarrierErrorException:
            return False

    def start_background_stress_commands(self, node_ips: list[str] | None = None) -> Tuple[CassandraStressThread, CassandraStressThread]:
        self.log.info("Running stress during adding new DC")

        stress_cmds = self.params.get('stress_cmd')
        if node_ips:
            node_param = f" -node {','.join(node_ips)}"
        else:
            node_param = ""
        read_thread = self.run_stress_thread(
            stress_cmd=stress_cmds[0] + node_param, stats_aggregate_cmds=False, round_robin=False)
        write_thread = self.run_stress_thread(
            stress_cmd=stress_cmds[1] + node_param, stats_aggregate_cmds=False, round_robin=False)
        self.log.info("Stress during adding DC started")
        return read_thread, write_thread
