#!groovy

def call(Map pipelineParams) {

    def builder = getJenkinsLabels('gce', null)

    pipeline {
        agent {
            label {
                label builder.label
            }
        }
        environment {
            AWS_ACCESS_KEY_ID     = credentials('qa-aws-secret-key-id')
            AWS_SECRET_ACCESS_KEY = credentials('qa-aws-secret-access-key')
            SCT_CLUSTER_BACKEND   = 'k8s-gce-minikube'
		}
        parameters {
            string(defaultValue: 'scylladb/scylla-operator:v0.2.3',
                   description: '',
                   name: 'k8s_scylla_operator_docker_image')
            string(defaultValue: '4.0.0',
                   description: '',
                   name: 'scylla_version')
            string(defaultValue: '2.0.2',
                   description: '',
                   name: 'scylla_mgmt_agent_version')
            string(defaultValue: "longevity_test.LongevityTest.test_custom_time",
                   description: '',
                   name: 'test_name')
            string(defaultValue: "internal_test_data/simple_test_case.yaml",
                   description: '',
                   name: 'test_config')
            string(defaultValue: "${pipelineParams.get('post_behavior_db_nodes', 'keep-on-failure')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_db_nodes')
            string(defaultValue: "${pipelineParams.get('post_behavior_loader_nodes', 'destroy')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_loader_nodes')
            string(defaultValue: "${pipelineParams.get('post_behavior_monitor_nodes', 'keep-on-failure')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_monitor_nodes')
            string(defaultValue: "qa@scylladb.com",
                   description: 'email recipients of email report',
                   name: 'email_recipients')
        }
        options {
            timestamps()
            disableConcurrentBuilds()
            timeout(pipelineParams.timeout)
            buildDiscarder(logRotator(numToKeepStr: "${pipelineParams.get('builds_to_keep', '20')}",))
        }
        stages {
            stage('Checkout') {
                steps {
                    dir('scylla-cluster-tests') {
                        checkout scm
                    }
                }
            }
            stage('Run SCT Test') {
                steps {
                    script {
                        def test_config = groovy.json.JsonOutput.toJson(params.test_config)

                        sctScript """
                            rm -fv ./latest

                            export SCT_CONFIG_FILES=${test_config}
                            export SCT_K8S_SCYLLA_OPERATOR_DOCKER_IMAGE=${params.k8s_scylla_operator_docker_image}
                            export SCT_SCYLLA_VERSION=${params.scylla_version}
                            export SCT_SCYLLA_MGMT_AGENT_VERSION=${params.scylla_mgmt_agent_version}

                            export SCT_POST_BEHAVIOR_DB_NODES="${params.post_behavior_db_nodes}"
                            export SCT_POST_BEHAVIOR_LOADER_NODES="${params.post_behavior_loader_nodes}"
                            export SCT_POST_BEHAVIOR_MONITOR_NODES="${params.post_behavior_monitor_nodes}"

                            echo "start test ......."
                            ./docker/env/hydra.sh run-test ${params.test_name} --logdir "`pwd`"
                            echo "end test ....."
                        """
                    }
                }
            }
            stage('Collect log data') {
                steps {
                    script {
                        def test_config = groovy.json.JsonOutput.toJson(params.test_config)

                        sctScript """
                            export SCT_CONFIG_FILES=${test_config}

                            echo "start collect logs ..."
                            ./docker/env/hydra.sh collect-logs --logdir "`pwd`"
                            echo "end collect logs"
                        """
                    }
                }
            }
            stage('Clean resources') {
                steps {
                    script {
                        def test_config = groovy.json.JsonOutput.toJson(params.test_config)

                        sctScript """
                            export SCT_POST_BEHAVIOR_DB_NODES="${params.post_behavior_db_nodes}"
                            export SCT_POST_BEHAVIOR_LOADER_NODES="${params.post_behavior_loader_nodes}"
                            export SCT_POST_BEHAVIOR_MONITOR_NODES="${params.post_behavior_monitor_nodes}"

                            echo "start clean resources ..."
                            ./docker/env/hydra.sh clean-resources --post-behavior --logdir "`pwd`"
                            echo "end clean resources"
                        """
                    }
                }
            }
            stage('Send email with result') {
                steps {
                    script {
                        def email_recipients = groovy.json.JsonOutput.toJson(params.email_recipients)

                        sctScript """
                            echo "Start send email ..."
                            ./docker/env/hydra.sh send-email --logdir "`pwd`" --email-recipients "${email_recipients}"
                            echo "Email sent"
                        """
                    }
                }
            }
        }
    }
}
