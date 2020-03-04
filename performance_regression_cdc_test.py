import pprint
import time

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from sortedcontainers import SortedDict

from sdcm.cluster import BaseNode, UnexpectedExit, Failure, Setup
from sdcm.results_analyze import QueryFilter, QueryFilterCS, PerformanceResultsAnalyzer
from sdcm.db_stats import TestStatsMixin
from sdcm.logcollector import GrafanaScreenShot, GrafanaSnapshot
from performance_regression_test import PerformanceRegressionTest

PP = pprint.PrettyPrinter(indent=2)


class CDCQueryFilter(QueryFilter):
    def filter_test_details(self):
        test_details = 'test_details.job_name: \"{}\" '.format(
            self.test_doc['_source']['test_details']['job_name'].split('/')[0])
        test_details += self.test_cmd_details()
        test_details += ' AND test_details.time_completed: {}'.format(self.date_re)
        test_details += r' AND test_details.test_name: *cdc* '
        return test_details

    def test_cmd_details(self):
        pass


class CDCQueryFilterCS(QueryFilterCS, CDCQueryFilter):
    def cs_params(self):
        return self._PROFILE_PARAMS if 'profiles' in self.test_name else self._PARAMS


class CDCPerformanceResultsAnalyzer(PerformanceResultsAnalyzer):
    def __init__(self, es_index, es_doc_type, send_email, email_recipients, logger=None, performance_email_template=None):  # pylint: disable=too-many-arguments
        super(PerformanceResultsAnalyzer, self).__init__(
            es_index=es_index,
            es_doc_type=es_doc_type,
            send_email=send_email,
            email_recipients=email_recipients,
            email_template_fp=performance_email_template,
            logger=logger
        )

    def _query_filter(self, test_doc, is_gce):
        return CDCQueryFilterCS(test_doc, is_gce)()

    def check_regression_cdc(self, test_id, base_test_id, is_gce=False):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements

        doc = self.get_test_by_id(test_id)
        if not doc:
            self.log.error('Cannot find test by id: {}!'.format(test_id))
            return False
        self.log.debug(PP.pformat(doc))

        test_stats = self._test_stats(doc)
        if not test_stats:
            return False

        # filter tests
        query = self._query_filter(doc, is_gce)
        self.log.debug(query)
        if not query:
            return False
        self.log.debug("Query to ES: %s", query)
        filter_path = ['hits.hits._id',
                       'hits.hits._source.results.stats_average',
                       'hits.hits._source.results.stats_total',
                       'hits.hits._source.results.throughput',
                       'hits.hits._source.results',
                       'hits.hits._source.versions',
                       'hits.hits._source.test_details']
        tests_filtered = self._es.search(index=self._es_index, q=query, filter_path=filter_path,  # pylint: disable=unexpected-keyword-arg
                                         size=self._limit)

        if not tests_filtered:
            self.log.info('Cannot find tests with the same parameters as {}'.format(test_id))
            return False
        # get the best res for all versions of this job
        group_by_version_cdc_type = dict()
        # Example:
        # group_by_cdc = {
        #     "version": {
        #           "cdc_disabled|cdc_enabled|cdc_pre_image": {
        #               "tests": {  # SortedDict(),
        #                   "20180726": {
        #                       "latency 99th percentile": 10.3,
        #                       "op rate": 15034.3
        #                       #...
        #                   }
        #               },
        #
        #               "stats_best": {
        #                   "op rate": 0,
        #                   "latency mean": 0,
        #               },
        #               "best_test_id": {
        #                   "op rate": "9b4a0a287",
        #                   "latency mean": "9b4a0a287",
        #
        #               }
        #           }
        #      }
        # }
        # Find best results for each version

        current_tests = SortedDict()
        for row in tests_filtered['hits']['hits']:
            if '_source' not in row:  # non-valid record?
                self.log.error('Skip non-valid test: %s', row['_id'])
                continue
            version_info = self._test_version(row)
            version = version_info['version']
            if not version:
                self.log.error('Skip with wrong version %s', row['_id'])
                continue
            if "results" not in row["_source"]:
                self.log.error('Skip with no results %s', row['_id'])
                continue
            cdc_type = row["_source"]['test_details']['test_name'].split("_")[-2:]
            cdc_type = "-".join(cdc_type)
            curr_test_stats = self._test_stats(row)
            if not curr_test_stats:
                self.log.error('Skip with no test stats %s', row['_id'])
                continue
            if base_test_id in row["_id"] and cdc_type not in current_tests:
                current_tests[cdc_type] = dict()
                current_tests[cdc_type]['stats'] = curr_test_stats
                current_tests[cdc_type]['version'] = version_info
                current_tests[cdc_type]['best_test_id'] = {
                    k: f"Commit: {version_info['commit_id']}, Date: {version_info['date']}" for k in self.PARAMS}
                current_tests[cdc_type]['results'] = row['_source']['results']
                self.log.info('Added current test results %s. Check next', row['_id'])
                continue
            if version not in group_by_version_cdc_type:
                group_by_version_cdc_type[version] = dict()

            if cdc_type not in group_by_version_cdc_type[version]:
                group_by_version_cdc_type[version][cdc_type] = dict(
                    tests=SortedDict(), stats_best=dict(), best_test_id=dict())
                group_by_version_cdc_type[version][cdc_type]['stats_best'] = {k: 0 for k in self.PARAMS}
                group_by_version_cdc_type[version][cdc_type]['best_test_id'] = {
                    k: f"Commit: {version_info['commit_id']}, Date: {version_info['date']}" for k in self.PARAMS}

            group_by_version_cdc_type[version][cdc_type]['tests'][version_info['date']] = curr_test_stats
            old_best = group_by_version_cdc_type[version][cdc_type]['stats_best']
            group_by_version_cdc_type[version][cdc_type]['stats_best'] =\
                {k: self._get_best_value(k, curr_test_stats[k], old_best[k])
                 for k in self.PARAMS if k in curr_test_stats and k in old_best}
            # replace best test id if best value changed
            for k in self.PARAMS:
                if k in curr_test_stats and k in old_best and\
                        group_by_version_cdc_type[version][cdc_type]['stats_best'][k] == curr_test_stats[k]:
                    group_by_version_cdc_type[version][cdc_type]['best_test_id'][
                        k] = f"Commit: {version_info['commit_id']}, Date: {version_info['date']}"

        current_res_list = list()
        versions_res_list = list()

        test_version_info = self._test_version(doc)
        test_version = test_version_info['version']
        cdc_type = doc["_source"]['test_details']['test_name'].split("_")[-2:]
        cdc_type = "-".join(cdc_type)
        base_line = current_tests.get('cdc-disabled')

        for cdc_type in current_tests:
            if not current_tests[cdc_type] or cdc_type == 'cdc-disabled':
                self.log.info('No tests with cdc in the current run {} to compare'.format(test_version))
                continue
            cmp_res = self.cmp(current_tests[cdc_type]['stats'],
                               base_line['stats'],
                               cdc_type,
                               current_tests[cdc_type]['best_test_id'])
            current_res_list.append(cmp_res)

        if not current_res_list:
            self.log.info('No test results to compare with')
            return False
        current_res_list = sorted(current_res_list, key=lambda x: x['version_dst'])
        current_prometheus_stats = SortedDict()
        for cdc_type in current_tests:
            current_prometheus_stats[cdc_type] = {stat: current_tests[cdc_type]["results"].get(
                                                  stat, {}) for stat in TestStatsMixin.PROMETHEUS_STATS}

        for version in group_by_version_cdc_type:
            version_res = SortedDict()

            for cdc_type in current_tests:
                print(version, cdc_type)
                if cdc_type not in group_by_version_cdc_type[version]:
                    self.log.info('No %s cdc tests in version %s', cdc_type, version)
                    continue
                if not group_by_version_cdc_type[version][cdc_type]['tests']:
                    self.log.info('No previous tests in the current version {} to compare'.format(test_version))
                    # cmp_res.update({'cdc_type': cdc_type})
                    continue
                version_res[cdc_type] = self.cmp(group_by_version_cdc_type[version][cdc_type]['stats_best'],
                                                 current_tests[cdc_type]['stats'],
                                                 version,
                                                 group_by_version_cdc_type[version][cdc_type]['best_test_id'])
                version_res[cdc_type].update({'cdc_type': cdc_type})

            versions_res_list.append({version: version_res})

        full_test_name = doc["_source"]["test_details"]["test_name"].rsplit("_cdc_", 1)[0]
        test_start_time = datetime.utcfromtimestamp(float(doc["_source"]["test_details"]["start_time"]))
        cassandra_stress = doc['_source']['test_details'].get('cassandra-stress')
        dashboard_path = "app/kibana#/dashboard/03414b70-0e89-11e9-a976-2fe0f5890cd0?_g=()"
        results = dict(test_name=full_test_name,
                       test_start_time=str(test_start_time),
                       test_version=test_version_info,
                       base_line=base_line,
                       res_list=current_res_list,
                       ver_res_list=versions_res_list,
                       setup_details=self._get_setup_details(doc, is_gce),
                       prometheus_stats=current_prometheus_stats,
                       prometheus_stats_units=TestStatsMixin.PROMETHEUS_STATS_UNITS,
                       grafana_snapshots=self._get_grafana_snapshot(doc),
                       grafana_screenshots=self._get_grafana_screenshot(doc),
                       cs_raw_cmd=cassandra_stress.get('raw_cmd', "") if cassandra_stress else "",
                       job_url=doc['_source']['test_details'].get('job_url', ""),
                       dashboard_master=self.gen_kibana_dashboard_url(dashboard_path),
                       )
        self.log.debug('Regression analysis:')
        self.log.debug(PP.pformat(results))
        # test_name = full_test_name.split('.', 1)[1]  # Example: longevity_test.py:LongevityTest.test_custom_time
        subject = 'Performance Regression Compare Results - {} - {}'.format(full_test_name, test_version)
        html = self.render_to_html(results)
        self.send_email(subject, html)

        return True


class PerformanceRegressionCDCTest(PerformanceRegressionTest):
    keyspace = None
    table = None

    def prepare_base_line(self, write_cmd, test_name):
        total_ops, write_latency_99_avg, write_latency_99_stdev, _ = self._workload_cdc(
            write_cmd, 2, test_name, sub_type="cdc_disabled", check_regression=False)
        return total_ops, write_latency_99_avg, write_latency_99_stdev

    def _get_latency_write_99_avg(self):
        return self._stats['results']['latency_write_99']['avg']

    def _get_latency_write_99_stdev(self):
        return self._stats['results']['latency_write_99']['stdev']

    def test_write_with_cdc(self):
        write_cmd = self.params.get("stress_cmd_w")

        total_ops_base_line, base_latency_write_99_avg, base_latency_write_99_stddev = self.prepare_base_line(
            write_cmd, "test_write_with_cdc_disabled")
        node1: BaseNode = self.db_cluster.nodes[0]

        with self.cql_connection_patient(node1) as session:
            session.execute(f"TRUNCATE TABLE {self.keyspace}.{self.table}")
            session.execute(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true}}")

        total_ops_with_cdc, latency_write_99_avg_with_cdc, latency_write_99_stdev_with_cdc = self._workload_cdc(
            write_cmd, 1, "test_write_with_cdc_enabled", sub_type="cdc_enabled")

        self.log.info('Base line total_ops - cdc disabled \n%s', total_ops_base_line)
        self.log.info('Base line latency_write_99.avg - cdc disabled \n%s', base_latency_write_99_avg)
        self.log.info('Base line latency_write_99.stdev - cdc disabled \n%s', base_latency_write_99_stddev)
        self.log.info("CDC enabled total ops - \n%s", total_ops_with_cdc)
        self.log.info("CDC enabled latency_write_99_avg - \n%s", latency_write_99_avg_with_cdc)
        self.log.info("CDC enabled latency_write_99_stdev - \n%s", latency_write_99_stdev_with_cdc)

    def test_write_with_cdc_pre_image(self):
        write_cmd = self.params.get("stress_cmd_w")

        total_ops_base_line, base_latency_write_99_avg, base_latency_write_99_stddev = self.prepare_base_line(
            write_cmd, "test_write_with_cdc_disabled")
        node1: BaseNode = self.db_cluster.nodes[0]

        with self.cql_connection_patient(node1) as session:
            session.execute(f"TRUNCATE TABLE {self.keyspace}.{self.table}")
            session.execute(f"TRUNCATE TABLE {self.keyspace}.{self.table}_scylla_cdc_log")
            session.execute(
                f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true, 'preimage': true}}")

        total_ops_with_cdc, latency_write_99_avg_with_cdc, latency_write_99_stdev_with_cdc = self._workload_cdc(
            write_cmd, 1, "test_name_with_cdc_enabled", sub_type="cdc_preimage_enabled")

        self.log.info('Base line total_ops - cdc disabled \n%s', total_ops_base_line)
        self.log.info('Base line latency_write_99.avg - cdc disabled \n%s', base_latency_write_99_avg)
        self.log.info('Base line latency_write_99.stdev - cdc disabled \n%s', base_latency_write_99_stddev)
        self.log.info("CDC enabled total ops - \n%s", total_ops_with_cdc)
        self.log.info("CDC enabled latency_write_99_avg - \n%s", latency_write_99_avg_with_cdc)
        self.log.info("CDC enabled latency_write_99_stdev - \n%s", latency_write_99_stdev_with_cdc)

    def test_write_profile(self):
        self.keyspace = "cdc_keyspace"
        self.table = "test_cdc"
        write_cmd = self.params.get("stress_cmd_w")

        total_ops_base_line, base_latency_write_99_avg, base_latency_write_99_stddev = self.prepare_base_line(
            write_cmd, "test_write_with_cdc_disabled")
        node1: BaseNode = self.db_cluster.nodes[0]

        self.wait_no_compactions_running()
        try:
            node1.run_cqlsh(f"TRUNCATE TABLE {self.keyspace}.{self.table}", timeout=300)
        except (UnexpectedExit, Failure) as details:
            self.log.warning("Trancate error %s. Sleep and continue", details)
            time.sleep(30)
        self.wait_no_compactions_running()
        self.run_fstrim_on_all_db_nodes()

        node1.run_cqlsh(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true}}")

        total_ops_with_cdc, latency_write_99_avg_with_cdc, latency_write_99_stdev_with_cdc = self._workload_cdc(
            write_cmd, 1, "test_write_with_cdc_enabled", sub_type="cdc_enabled", check_regression=False)
        self.wait_no_compactions_running()
        try:
            node1.run_cqlsh(f"TRUNCATE TABLE {self.keyspace}.{self.table}", timeout=300)
        except (UnexpectedExit, Failure) as details:
            self.log.warning("Trancate error %s. Sleep and continue", details)
            time.sleep(30)
        try:
            node1.run_cqlsh(f"TRUNCATE TABLE {self.keyspace}.{self.table}_scylla_cdc_log", timeout=300)
        except (UnexpectedExit, Failure) as details:
            self.log.warning("Trancate error %s. Sleep and continue", details)
            time.sleep(30)
        self.wait_no_compactions_running()
        self.run_fstrim_on_all_db_nodes()

        node1.run_cqlsh(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true, 'preimage': true}}")

        total_ops_with_cdc_preim, latency_write_99_avg_with_cdc_preim, latency_write_99_stdev_with_cdc_preim = self._workload_cdc(
            write_cmd, 1, "test_name_with_cdc_enabled", sub_type="cdc_preimage_enabled", check_regression=False)

        self.log.info('Base line total_ops - cdc disabled \n%s', total_ops_base_line)
        self.log.info('Base line latency_write_99.avg - cdc disabled \n%s', base_latency_write_99_avg)
        self.log.info('Base line latency_write_99.stdev - cdc disabled \n%s', base_latency_write_99_stddev)
        self.log.info("CDC enabled total ops - \n%s", total_ops_with_cdc)
        self.log.info("CDC enabled latency_write_99_avg - \n%s", latency_write_99_avg_with_cdc)
        self.log.info("CDC enabled latency_write_99_stdev - \n%s", latency_write_99_stdev_with_cdc)
        self.log.info("CDC enabled total ops - \n%s", total_ops_with_cdc_preim)
        self.log.info("CDC enabled latency_write_99_avg - \n%s", latency_write_99_avg_with_cdc_preim)
        self.log.info("CDC enabled latency_write_99_stdev - \n%s", latency_write_99_stdev_with_cdc_preim)

        self.check_regression()

    def test_write(self):
        self.keyspace = "keyspace1"
        self.table = "standard1"
        main_test_id = Setup.test_id()
        write_cmd = self.params.get("stress_cmd_w")

        baseline_doc_id = self._workload_cdc(write_cmd, 2,
                                             test_name="test_write",
                                             sub_type="cdc_disabled",
                                             check_regression=False)

        node1: BaseNode = self.db_cluster.nodes[0]
        self.wait_no_compactions_running()
        self.run_fstrim_on_all_db_nodes()
        try:
            node1.run_cqlsh(f"TRUNCATE TABLE {self.keyspace}.{self.table}", timeout=300)
        except (UnexpectedExit, Failure) as details:
            self.log.warning("Trancate error %s. Sleep and continue", details)
            time.sleep(60)

        node1.run_cqlsh(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true}}")

        cdc_enabled_doc_id = self._workload_cdc(write_cmd, 2,
                                                test_name="test_write",
                                                sub_type="cdc_enabled",
                                                check_regression=False)

        self.wait_no_compactions_running()
        self.run_fstrim_on_all_db_nodes()

        try:
            node1.run_cqlsh(f"TRUNCATE TABLE {self.keyspace}.{self.table}", timeout=300)
        except (UnexpectedExit, Failure) as details:
            self.log.warning("Trancate error %s. Sleep and continue", details)
            time.sleep(60)
        try:
            node1.run_cqlsh(f"TRUNCATE TABLE {self.keyspace}.{self.table}_scylla_cdc_log", timeout=300)
        except (UnexpectedExit, Failure) as details:
            self.log.warning("Trancate error %s. Sleep and continue", details)
            time.sleep(60)

        node1.run_cqlsh(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true, 'preimage': true}}")

        cdc_preimage_doc_id = self._workload_cdc(write_cmd, 2,
                                                 test_name="test_write",
                                                 sub_type="cdc_preimage_enabled",
                                                 check_regression=False)

        self.wait_no_compactions_running()

        # self.check_regression()
        results_analyzer = PerformanceResultsAnalyzer(es_index=self._test_index,
                                                      es_doc_type=self._es_doc_type,
                                                      send_email=True,
                                                      email_recipients=self.params.get('email_recipients', default=None),
                                                      performance_email_template="results_performance_cdc.html")
        is_gce = bool(self.params.get('cluster_backend') == 'gce')
        try:
            # results_analyzer.check_regression_cdc(test_id=self._test_id,
            #                                       base_test_id=self._test_id.split("_", 1)[0],
            #                                       is_gce=is_gce)
            results_analyzer.check_regression_with_subtest_baseline(test_id=cdc_preimage_doc_id,
                                                                    base_test_id=main_test_id,
                                                                    subtest_baseline="cdc_disabled",
                                                                    is_gce=is_gce)
        except Exception as ex:  # pylint: disable=broad-except
            self.log.exception('Failed to check regression: %s', ex)

    def test_mixed(self):  # pylint: disable=too-many-locals
        self.keyspace = "keyspace1"
        self.table = "standard1"
        write_cmd = self.params.get("stress_cmd_w")
        read_cdc_log_cmd = self.params.get("stress_cmd_r")

        baseline_doc_id = self._workload_cdc(write_cmd, 2,
                                             test_name="test_write_with_cdc_disabled",
                                             sub_type="cdc_disabled",
                                             check_regression=False)

        node1: BaseNode = self.db_cluster.nodes[0]
        self.wait_no_compactions_running()
        self.run_fstrim_on_all_db_nodes()

        with self.cql_connection_patient(node1) as session:
            session.execute(f"TRUNCATE TABLE {self.keyspace}.{self.table}")
            session.execute(f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true}}")

        cdc_enabled_doc_id = self._workload_cdc(write_cmd, 2,
                                                test_name="test_mixed_with_cdc_enabled",
                                                sub_type="cdc_enabled",
                                                check_regression=False,
                                                cdc_read_cmd=read_cdc_log_cmd)

        self.wait_no_compactions_running()
        self.run_fstrim_on_all_db_nodes()

        with self.cql_connection_patient(node1) as session:
            session.execute(f"TRUNCATE TABLE {self.keyspace}.{self.table}")
            session.execute(f"TRUNCATE TABLE {self.keyspace}.{self.table}_scylla_cdc_log")
            session.execute(
                f"ALTER TABLE {self.keyspace}.{self.table} WITH cdc = {{'enabled': true, 'preimage': true}}")

        cdc_preimage_doc_id = self._workload_cdc(write_cmd, 2,
                                                 test_name="test_mixed_with_cdc_preimage",
                                                 sub_type="cdc_preimage_enabled",
                                                 check_regression=False,
                                                 cdc_read_cmd=read_cdc_log_cmd)

        self.check_regression()

    def _workload_cdc(self, stress_cmd, stress_num, test_name, sub_type=None, keyspace_num=1, prefix='', debug_message='',  # pylint: disable=too-many-arguments,too-many-locals
                      save_stats=True, check_regression=True, cdc_read_cmd=None):
        read_statistics = None
        future = None
        if debug_message:
            self.log.debug(debug_message)

        if save_stats:
            self.create_test_stats(sub_type=sub_type, doc_id_with_timestamp=True, test_name=test_name)

        stress_queue = self.run_stress_thread(stress_cmd=stress_cmd, stress_num=stress_num, keyspace_num=keyspace_num,
                                              prefix=prefix, stats_aggregate_cmds=False)
        if cdc_read_cmd:
            loader_node: BaseNode = self.loaders.nodes[0]
            loader_node.remoter.run('go get -u github.com/piodul/cdc-stressor')
            db_node: BaseNode = self.db_cluster.nodes[0]

            def read_cdc_log(loader: BaseNode):
                result = loader.remoter.run(
                    f'{cdc_read_cmd} -nodes {db_node.ip_address}')
                return result
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(read_cdc_log, loader_node)

        results = self.get_stress_results(queue=stress_queue, store_results=True)
        if future:
            read_statistics = future.result()
            self.log.info(read_statistics.stdout)

        if save_stats:
            self.update_test_details(scylla_conf=True)
            stat_results = PP.pformat(self._stats["results"])
            self.log.debug(f'Results {test_name}: \n{stat_results}')
            self.display_results(results, test_name=test_name)
            if check_regression:
                self.check_regression()

            return self._test_id
        return None

    def parse_cdc_stresser_result(self, output):
        keys = ["rows read/s",
                "latency min",
                "latency avg",
                "latency median",
                "latency 90%",
                "latency 99%",
                "latency 99.9%",
                "latency max"]
        result = {}
        for line in output.split("\n"):
            r = line.split(":")
            if len(r) < 2:
                continue
            name = r[0].strip()
            value = r[1].strip()
            if name in keys:
                if name == "rows read/s":
                    result[f"{name}"] = value.split("/")[0]
                else:
                    result[f"read {name}"] = value.split(" ")[0]
        return result

    def check_regression(self):
        results_analyzer = CDCPerformanceResultAnalyzer(es_index=self._test_index, es_doc_type=self._es_doc_type,
                                                        send_email=True,
                                                        email_recipients=self.params.get('email_recipients', default=None))
        is_gce = bool(self.params.get('cluster_backend') == 'gce')
        try:
            results_analyzer.check_regression_cdc(test_id=self._test_id,
                                                  base_test_id=self._test_id.split("_", 1)[0],
                                                  is_gce=is_gce)
        except Exception as ex:  # pylint: disable=broad-except
            self.log.exception('Failed to check regression: %s', ex)


if __name__ == "__main__":
    import logging

    logging.basicConfig(level="DEBUG")
    LOGGER = logging.getLogger("CDCPerfTest")

    perf_analyzer = CDCPerformanceResultsAnalyzer("performanceregressioncdctest",
                                                  "test_stats",
                                                  True,
                                                  # ["alex.bykov@scylladb.com", "roy@scylladb.com"],
                                                  ['alex.bykov@scylladb.com'],
                                                  # ["alex.bykov@scylladb.com", 'piotr@scylladb.com',
                                                  #  'piodul@scylladb.com', 'amos@scylladb.com', "roy@scylladb.com"],
                                                  performance_email_template="results_performance_cdc.html",
                                                  logger=LOGGER)

    perf_analyzer.check_regression_with_subtest_baseline(
        test_id="63c59791-8f44-4071-9bc3-460bea31a8fc_20200304_064956_421667",
        base_test_id="63c59791-8f44-4071-9bc3-460bea31a8fc",
        subtest_baseline="cdc_disabled",
        is_gce=False)
    perf_analyzer.check_regression_cdc(
        "63c59791-8f44-4071-9bc3-460bea31a8fc_20200304_064956_421667",
        "63c59791-8f44-4071-9bc3-460bea31a8fc",
        False)
