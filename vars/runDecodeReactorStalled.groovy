#! groovy

def call(){
    sh '''#!/bin/bash

        set -xe
        env

        echo "Starting decoding reactor stalled ..."
        RUNNER_IP=$(cat sct_runner_ip||echo "")
        if [[ -n "${RUNNER_IP}" ]] ; then
            ./docker/env/hydra.sh --execute-on-runner ${RUNNER_IP} decode-reactor-stalls
        else
            ./docker/env/hydra.sh generate-pt-report --logdir "`pwd`"
        fi
        echo "Reactor stalled decoded!"
    '''
}
