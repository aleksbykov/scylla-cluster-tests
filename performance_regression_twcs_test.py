from performance_regression_test import PerformanceRegressionTest


class PerformanceRegressionTWCSTest(PerformanceRegressionTest):

    def prepare_keyspace(self, keyspace: str = "scylla_bench", table: str = "test", ttl: int = 60, window_size: int = 1):
        """Create keyspace and table for timeseries data
        """
        with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
            session.execute(f"""
                CREATE KEYSPACE IF NOT EXISTS {keyspace} WITH replication = {{'class': 'SimpleStrategy', 'replication_factor': '3'}}
                AND durable_writes = true;""")
            session.execute(f"""
                CREATE TABLE IF NOT EXISTS {keyspace}.{table} (
                    pk bigint,
                    ck bigint,
                    v  blob,
                    PRIMARY KEY (pk, ck)
                ) WITH CLUSTERING ORDER BY (ck ASC)
                    AND default_time_to_live = {ttl}
                    AND compaction = {{'class': 'TimeWindowCompactionStrategy', 'compaction_window_size': '{window_size}',
                    'compaction_window_unit': 'MINUTES'}}"""
                            )

    def test_write(self):
        num = self.params["n_loaders"]
        sb_write_cmd = self.params["stress_cmd_w"]
        sb_write_cmds = []
        for i in range(1, num + 1):
            self.prepare_keyspace(keyspace="scylla_bench", table=f"test{i}")
            sb_write_cmds.append(sb_write_cmd + " -keyspace scylla_bench " + f" -table test{i} ")

        sb_threads_queue = []
        for cmd in sb_write_cmds:
            sb_threads_queue.append(self.run_stress_thread(stress_cmd=cmd, round_robin=True))

        for sb_thread in sb_threads_queue:
            self.get_stress_results_bench(queue=sb_thread)

        self.update_test_details(scylla_conf=True)
        self.check_regression()

    def test_read(self):
        ...

    def test_mixed(self):
        ...
