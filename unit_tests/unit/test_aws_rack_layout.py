# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2026 ScyllaDB

"""`AWSCluster.add_nodes` gives every node it creates a simulated rack.

DB nodes follow `n_db_nodes_per_rack` when it is set. Without it, and on every other cluster, the nodes go
round-robin over the racks.
"""

import functools
from collections import Counter
from unittest.mock import MagicMock

import pytest

from sdcm.cluster_aws import AWSCluster, LoaderSetAWS, ScyllaAWSCluster


def _cluster(cls, layout):
    """A stand-in `cls` cluster on 3 simulated racks in one zone, with the DB rack layout `layout`."""
    cluster = MagicMock(spec=cls)
    cluster.params = {
        "simulated_regions": False,
        "simulated_racks": 3,
        "ip_ssh_connections": "private",
        "n_db_nodes_per_rack": layout,
    }
    cluster.racks_count = 3
    cluster.nodes = []
    cluster._node_index = 0
    cluster.log = MagicMock()
    cluster._ec2_ami_username = MagicMock()
    cluster.node_prefix = MagicMock()
    cluster.logdir = MagicMock()
    cluster._create_or_find_instances.side_effect = lambda count, **_: [MagicMock() for _ in range(count)]
    cluster._new_node_racks = functools.partial(cls._new_node_racks, cluster)
    return cluster


def _racks_of_new_nodes(cluster, count, rack=None):
    AWSCluster.add_nodes.__wrapped__(cluster, count=count, rack=rack)
    # The nodes initialize in parallel: order the calls by node index, which follows the instance order.
    calls = sorted(cluster._create_node.call_args_list, key=lambda call: call.args[3])
    return [call.kwargs["rack"] for call in calls]


@pytest.mark.parametrize(("count", "racks"), [(3, [0, 1, 2]), (5, [0, 1, 2, 0, 1])])
def test_at_16_db_nodes_round_robin_without_layout(count, racks):
    assert _racks_of_new_nodes(_cluster(ScyllaAWSCluster, layout=[]), count) == racks


def test_at_15_db_layout_places_five_nodes_3_1_1():
    racks = _racks_of_new_nodes(_cluster(ScyllaAWSCluster, layout=[3, 1, 1]), count=5)

    assert Counter(racks) == {0: 3, 1: 1, 2: 1}
    assert racks == [0, 0, 0, 1, 2]


def test_at_17_loaders_stay_round_robin_with_db_layout():
    racks = _racks_of_new_nodes(_cluster(LoaderSetAWS, layout=[3, 1, 1]), count=6)

    assert Counter(racks) == {0: 2, 1: 2, 2: 2}


def test_db_layout_rejects_a_node_count_other_than_its_total():
    cluster = _cluster(ScyllaAWSCluster, layout=[3, 1, 1])

    with pytest.raises(ValueError, match=r"n_db_nodes_per_rack=\[3, 1, 1\] totals 5, but add_nodes is adding 1"):
        _racks_of_new_nodes(cluster, count=1)


def test_explicit_rack_bypasses_the_db_layout():
    assert _racks_of_new_nodes(_cluster(ScyllaAWSCluster, layout=[3, 1, 1]), count=1, rack=2) == [2]
