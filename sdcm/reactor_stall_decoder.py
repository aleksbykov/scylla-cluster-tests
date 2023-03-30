import json
import logging

from pathlib import Path
from collections import defaultdict

from sdcm.utils.decorators import retrying


LOGGER = logging.getLogger(__name__)
DECODED_STALLS_DIR_NAME = "operations_reactor_stalls"


class ReactorStallDecoder:

    DISRUPTION_EVENT = "DisruptionEvent"
    INFO_EVENT = "InfoEvent"
    STEADY_STATE = "SteadyState"

    decoding_tools = ["addr2line.py",
                      "stall-analyser.py"]

    def __init__(self, cmd_runner: "CommandRunner",
                 working_dir: str | Path,
                 raw_events_file: str | Path,
                 scylla_exec_bin: str | Path = ""):
        self._cmd_runner = cmd_runner
        base_dir = self._convert_str_to_path(working_dir)
        self.raw_events_file = self._convert_str_to_path(raw_events_file)
        self.store_dir = base_dir.joinpath(DECODED_STALLS_DIR_NAME)
        self.tool_dir = base_dir.joinpath("scripts")
        self.scylla_exec_bin_path = scylla_exec_bin
        self.reactor_stalls_per_operation = defaultdict(dict)
        self.encoded_files = []

    def run_decoding(self):
        self._create_working_dirs()
        self._download_tools()
        self._extract_reactor_stalls_events_by_nemesis()
        self._save_per_operation_by_node()
        self.decode_reactor_stalls_in_batch()

    def _create_working_dirs(self):
        for creating_dir in [self.store_dir, self.tool_dir]:
            creating_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _convert_str_to_path(value: str | Path) -> Path:
        if isinstance(value, str):
            return Path(value)
        return value

    def _extract_reactor_stalls_events_by_nemesis(self):
        with open(self.raw_events_file, 'r', encoding='utf-8') as raw_events_file_content:
            current_operation = self.STEADY_STATE
            reactor_stall_per_node = defaultdict(list)
            for line in raw_events_file_content:
                event = json.loads(line)

                if event['base'] == 'InfoEvent' and 'TEST_END' in event['message']:
                    if current_operation in self.reactor_stalls_per_operation:
                        self.reactor_stalls_per_operation[current_operation].update(reactor_stall_per_node)
                    else:
                        self.reactor_stalls_per_operation[current_operation] = reactor_stall_per_node
                    break

                if event['base'] == self.DISRUPTION_EVENT:
                    self.reactor_stalls_per_operation[current_operation] = reactor_stall_per_node
                    reactor_stall_per_node = defaultdict(list)
                    if event['period_type'] == 'begin':
                        current_operation = event['nemesis_name']
                    if event['period_type'] == 'end':
                        current_operation = f"{self.STEADY_STATE}_after_{event['nemesis_name']}"
                    continue

                if event['base'] == 'DatabaseLogEvent' and event['type'].strip() == 'REACTOR_STALLED':
                    node_name = self._parse_node_name(event['node'])
                    reactor_stall_per_node[node_name].append(event['line'])

    @staticmethod
    def _parse_node_name(name: str):
        try:
            return name.split(" ")[1]
        except IndexError:
            return name

    def _save_per_operation_by_node(self):
        for operation, node_data in self.reactor_stalls_per_operation.items():
            operation_dir = self.store_dir.joinpath(operation)
            operation_dir.mkdir(exist_ok=True, parents=True)
            for node in node_data:
                node_file = operation_dir.joinpath(node)
                if not node_data[node]:
                    continue
                with open(node_file, 'w', encoding='utf-8') as encoded_stall_file:
                    encoded_stall_file.write('\n'.join(node_data[node]))
                self.encoded_files.append(node_file)

    @retrying()
    def _download_tools(self):
        for tool in self.decoding_tools:
            self._cmd_runner.run(
                f"curl -o {self.tool_dir}/{tool} https://raw.githubusercontent.com/scylladb/seastar/master/scripts/{tool};")

    def decode_reactor_stalls_in_batch(self):
        command = f"python3 {self.tool_dir}/stall-analyser.py"
        if self.scylla_exec_bin_path:
            command += f" -e {self.scylla_exec_bin_path}"

        for file in self.encoded_files:
            dest_dir = file.parent
            decoded_file = dest_dir.joinpath(f"decoded_{file.name}")
            self._cmd_runner.run(f"{command} {file} > {decoded_file}")
