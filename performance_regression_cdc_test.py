import pprint
import time

from sdcm.cluster import BaseNode, UnexpectedExit, Failure, Setup
from sdcm.results_analyze import QueryFilter, QueryFilterCS, PerformanceResultsAnalyzer
from performance_regression_test import PerformanceRegressionTest

PP = pprint.PrettyPrinter(indent=2)


class CDCQueryFilter(QueryFilter):
    def filter_test_details(self):
        test_details = 'test_details.job_name: \"{}\" '.format(
            self.test_doc['_source']['test_details']['job_name'].split('/')[0])
        test_details += self.test_cmd_details()
        test_details += ' AND test_details.time_completed: {}'.format(self.date_re)
        test_details += r' AND test_details.sub_type: cdc* '
        return test_details

    def test_cmd_details(self):
        pass


class CDCQueryFilterCS(QueryFilterCS, CDCQueryFilter):
    def cs_params(self):
        return self._PROFILE_PARAMS if 'profiles' in self.test_name else self._PARAMS


class CDCPerformanceResultsAnalyzer(PerformanceResultsAnalyzer):
    def _query_filter(self, test_doc, is_gce):
        return CDCQueryFilterCS(test_doc, is_gce)()


class PerformanceRegressionCDCTest(PerformanceRegressionTest):
    keyspace = None
    table = None

    def test_write_with_cdc(self):
        write_cmd = self.params.get("stress_cmd_w")

        self._workload_cdc(write_cmd,
                           stress_num=2,
                           test_name="test_write",
                           sub_type="cdc_disabled")

        node1: BaseNode = self.db_cluster.nodes[0]

        self.truncate_base_table(node1)
        node1.run_cqlsh(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true}}")

        self.wait_no_compactions_running()
        self.run_fstrim_on_all_db_nodes()

        self._workload_cdc(write_cmd,
                           stress_num=2,
                           test_name="test_write",
                           sub_type="cdc_enabled")

        self.wait_no_compactions_running()
        self.check_regression_with_baseline(base_line="cdc_disabled")

    def test_write_with_cdc_preimage(self):
        write_cmd = self.params.get("stress_cmd_w")

        self._workload_cdc(write_cmd,
                           stress_num=2,
                           test_name="test_write",
                           sub_type="cdc_disabled")
        node1: BaseNode = self.db_cluster.nodes[0]

        self.truncate_base_table(node1, cdclog_table=True)
        node1.run_cqlsh(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true, 'preimage': true}}")

        self._workload_cdc(write_cmd,
                           stress_num=2,
                           test_name="test_write",
                           sub_type="cdc_preimage_enabled")

        self.check_regression_with_baseline(base_line="cdc_disabled")

    def test_write_with_cdc_postimage(self):
        write_cmd = self.params.get("stress_cmd_w")

        self._workload_cdc(write_cmd,
                           stress_num=2,  # pylint: disable=unused-variable
                           test_name="test_write",
                           sub_type="cdc_disabled")
        node1: BaseNode = self.db_cluster.nodes[0]

        self.truncate_base_table(node1, cdclog_table=True)
        node1.run_cqlsh(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true, 'postimage': true}}")

        self._workload_cdc(write_cmd,
                           stress_num=2,  # pylint: disable=unused-variable
                           test_name="test_write",
                           sub_type="cdc_preimage_enabled")

        self.check_regression_with_baseline(base_line="cdc_disabled")

    def test_write(self):  # pylint: disable=unused-variable
        self.keyspace = "keyspace1"
        self.table = "standard1"
        write_cmd = self.params.get("stress_cmd_w")

        self._workload_cdc(write_cmd,
                           stress_num=2,
                           test_name="test_write",
                           sub_type="cdc_disabled")

        node1: BaseNode = self.db_cluster.nodes[0]
        self.wait_no_compactions_running()
        self.run_fstrim_on_all_db_nodes()
        self.truncate_base_table(node1)

        node1.run_cqlsh(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true}}")

        self._workload_cdc(write_cmd,
                           stress_num=2,
                           test_name="test_write",
                           sub_type="cdc_enabled")

        self.wait_no_compactions_running()
        self.run_fstrim_on_all_db_nodes()
        self.truncate_base_table(node1, cdclog_table=True)

        node1.run_cqlsh(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true, 'preimage': true}}")

        self._workload_cdc(write_cmd,
                           stress_num=2,
                           test_name="test_write",
                           sub_type="cdc_preimage_enabled")

        self.wait_no_compactions_running()
        self.run_fstrim_on_all_db_nodes()
        self.truncate_base_table(node1, cdclog_table=True)

        node1.run_cqlsh(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true, 'postimage': true}}")

        self._workload_cdc(write_cmd,
                           stress_num=2,
                           test_name="test_write",
                           sub_type="cdc_postimage_enabled")

        self.wait_no_compactions_running()

        self.check_regression_with_baseline(base_line="cdc_disabled")

    def _workload_cdc(self, stress_cmd, stress_num, test_name, sub_type=None, keyspace_num=1,  # pylint: disable=too-many-arguments
                      prefix='', debug_message='', save_stats=True):

        if debug_message:
            self.log.debug(debug_message)

        if save_stats:
            self.create_test_stats(sub_type=sub_type,
                                   doc_id_with_timestamp=True,
                                   append_sub_test_to_name=False)

        stress_queue = self.run_stress_thread(stress_cmd=stress_cmd, stress_num=stress_num, keyspace_num=keyspace_num,
                                              prefix=prefix, stats_aggregate_cmds=False)

        results = self.get_stress_results(queue=stress_queue, store_results=True)

        if save_stats:
            self.update_test_details(scylla_conf=True)
            stat_results = PP.pformat(self._stats["results"])
            self.log.debug(f'Results {test_name}: \n{stat_results}')
            self.display_results(results, test_name=test_name)

    def check_regression_with_baseline(self, base_line):
        results_analyzer = CDCPerformanceResultsAnalyzer(es_index=self._test_index,
                                                         es_doc_type=self._es_doc_type,
                                                         send_email=True,
                                                         email_recipients=self.params.get('email_recipients')
                                                         )

        is_gce = bool(self.params.get('cluster_backend') == 'gce')
        try:
            results_analyzer.check_regression_with_subtest_baseline(test_id=self._test_id,
                                                                    base_test_id=Setup.test_id(),
                                                                    subtest_baseline=base_line,
                                                                    is_gce=is_gce)
        except Exception as ex:  # pylint: disable=broad-except
            self.log.exception('Failed to check regression: %s', ex)

    def truncate_base_table(self, node, cdclog_table=False):
        try:
            node.run_cqlsh(f"TRUNCATE TABLE {self.keyspace}.{self.table}", timeout=300)
        except (UnexpectedExit, Failure) as details:
            self.log.warning("Truncate error %s. Sleep and continue", details)
            time.sleep(60)
        if cdclog_table:
            try:
                node.run_cqlsh(f"TRUNCATE TABLE {self.keyspace}.{self.table}_scylla_cdc_log", timeout=300)
            except (UnexpectedExit, Failure) as details:
                self.log.warning("Truncate error %s. Sleep and continue", details)
                time.sleep(60)
