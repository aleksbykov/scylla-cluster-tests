#!groovy

// trick from https://github.com/jenkinsci/workflow-cps-global-lib-plugin/pull/43
def lib = library identifier: 'sct@snapshot', retriever: legacySCM(scm)

longevityPipeline(
    backend: 'aws',
    region: 'eu-west-1',
    test_name: 'longevity_test.LongevityTest.test_custom_time',
    test_config: 'raft/raft-topology-changes-3h.yaml',

    timeout: [time: 360, unit: 'MINUTES']
)
