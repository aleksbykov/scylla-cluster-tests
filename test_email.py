import os

import jinja2
import logging
import sys


from sdcm.db_stats import TestStatsMixin
from sdcm.results_analyze import PerformanceResultsAnalyzer
from sdcm.es import ES


rootLogger = logging.getLogger()
rootLogger.setLevel(logging.DEBUG)
logFormatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")

consoleHandler = logging.StreamHandler(sys.stdout)
consoleHandler.setLevel(logging.DEBUG)
consoleHandler.setFormatter(logFormatter)
rootLogger.addHandler(consoleHandler)


results = {
    "test_name": "performance_regression_test.py:PerformanceRegressionTest.test_write",
    "setup_details": {
    "instance_type_monitor": "t2.small",
    "region_name": "us-east-1",
    "n_monitor_nodes": 1,
    "n_db_nodes": 3,
    "instance_type_db": "i3.4xlarge",
    "instance_type_loader": "c4.large",
    "instance_provision": "on_demand",
    "n_loaders": 4
    },
    "dashboard_master": "https://KIBANA_URL_ID.us-east-1.aws.found.io:9243",
    "grafana_snapshot": [
        "http://ya.ru",
        "http://google.com"
    ],
    "test_version": {
        "date": "20180726",
        "commit_id": "9b4a0a287",
        "version": "2.3.rc1"
      },
    "prometheus_stats": {},
    "res_list": [
    {
        "res":{
            "op_rate": {
                "status": "Regression",
                "val": 12312,
                "best_val": 1231,
                "best_id": "19d290da",
                "percent":"3%",
            },

            "latency_mean":{
                "status": "Regression",
                "val": 0.12312,
                "best_val": 0.01231,
                "best_id": "19d290da",
                "percent":"1%",
            },
            "latency_99th_percentile": {
                "status": "Progress",
                "val": 0.12312,
                "best_val": 0.21231,
                "best_id": "19d290da",
                "percent":"10%",
            },
        },
        "version_dst": "2.1"
        
    },
    {
        "res":{
            "op_rate": {
                "status": "Regression",
                "val": 12312,
                "best_val": 1231,
                "best_id": "19d290da",
                "percent": "15%",
            },

            "latency_mean":{
                "status": "Progress",
                "val": 0.12312,
                "best_val": 0.01231,
                "best_id": "19d290da",
                "percent":"1%",
            },
            "latency_99th_percentile": {
                "status": "Progress",
                "val": 0.12312,
                "best_val": 0.21231,
                "best_id": "19d290da",
                "percent":"10%",
            },
        },
        "version_dst": "2.1.rc1"
        
    },
    {
        "res":{
            "op_rate": {
                "status": "Progress",
                "val": 12312,
                "best_val": 1231,
                "best_id": "19d290da",
                "percent": "6%",
            },

            "latency_mean":{
                "status": "Progress",
                "val": 2.12,
                "best_val": 5.01231,
                "best_id": "19d290da",
                "percent": "10%",
            },
            "latency_99th_percentile": {
                "status": "Regression",
                "val": 0.12312,
                "best_val": 0.21231,
                "best_id": "19d290da",
                "percent": "7%",
            },
        },
        "version_dst": "2.3"
        
    },        
    ],
    "cs_raw_cmd": "cassandra-stress read no-warmup cl=QUORUM duration=10m -schema 'replication(factor=3)' "
                  "-port jmx=6868 -mode cql3 native -rate threads=1 -pop 'dist=gauss(1..30000,15000,1500)'",
    "job_url": "http://jenkins.cloudius-systems.com:8080/job/scylla-master-ami-perf-regression-latency-temp-roy/label=aws-scylla-qa-builder3,sub_test=test_latency_temp/12/",
    "prometheus_stats_units": TestStatsMixin.PROMETHEUS_STATS_UNITS,
}
for stat in TestStatsMixin.PROMETHEUS_STATS:
    print stat
    results["prometheus_stats"].update({
        stat: {
            "max": 121312.423423,
            "min": 2112.234231,
            "avg": 32232.5676534,
            "stdev": 23.38964
        }
    })


def render_templ(results, html_file_path):
    loader = jinja2.FileSystemLoader("sdcm")
    env = jinja2.Environment(loader=loader, autoescape=True)
    template = env.get_template("results_performance.html")
    html = template.render(results)
    if html_file_path:
        with open(html_file_path, "w") as f:
            f.write(html)
        print "HTML report saved to '%s'." % html_file_path
    return html


def test_html_template_integrity():
    render_templ(results, os.path.expanduser("~/test.html"))


def test_perf_analyzer():
    ra = PerformanceResultsAnalyzer(es_index="performanceregressiontest", es_doc_type="test_stats",
                                    send_email=True, email_recipients=["alex.bykov@scylladb.com"], logger=rootLogger)
    ra.check_regression("20181126-203505-045525")  # mixed
    ra.check_regression("20181126-173819-465519")  # read
    ra.check_regression("20181126-191039-253842")  # write

def test_logevity_2scr(id):
    es = ES()

    doc = es.get_doc('longevitytest', id)

    print '-----------------------'
    print doc['_source']['test_details']['grafana_snapshot']

if __name__ == "__main__":
    test_html_template_integrity()
    # test_perf_analyzer()
    # test_logevity_2scr('20181211-161748-295118')