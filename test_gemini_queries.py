import logging
import time
from pprint import PrettyPrinter
from cassandra.cluster import Cluster, Session, ResultSet, SimpleStatement, ConsistencyLevel, ExecutionProfile, EXEC_PROFILE_DEFAULT, RetryPolicy
from cassandra.policies import WhiteListRoundRobinPolicy, DowngradingConsistencyRetryPolicy, AddressTranslator
from cassandra.query import tuple_factory
from cassandra.util import Date

from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty
from threading import Thread, current_thread


class ComposeAddressTranslator(AddressTranslator):

    def set_map(self, address_map):
        # strip ports from both source and destination as the cassandra python
        # client doesn't appear to support ports translation
        self.address_map = address_map

    def contact_points(self):
        return [addr for addr in self.address_map.values()]

    def translate(self, addr):

        # print some debug output
        print('in translate(self, addr) method', type(addr), addr)

        trans_addr = self.address_map[addr]
        return trans_addr


class RetryOnRead10TimesPolicy(RetryPolicy):
    attempts = 5
    max_num_retries = 100

    def get_retry_status(self, query, consistency, required_responses, received_responses, data_retrieved, retry_num):
        print(self.attempts, retry_num, bool(retry_num % self.attempts), sep=":::")

        if retry_num == self.max_num_retries:
            return (RetryPolicy.RETHROW, None)
        time.sleep(5)
        return (RetryPolicy.RETRY, ConsistencyLevel.ONE) if retry_num % self.attempts else (RetryPolicy.RETRY_NEXT_HOST, ConsistencyLevel.QUORUM)

    def on_read_timeout(self, *args, **kwargs):
        print(f"ON_READ_TIMEOUT_ARGS: {locals()}", sep="\n")
        return self.get_retry_status(*args, **kwargs)

    def on_unavailable(self, *args, **kwargs):
        print(f"ON_UNAVAILABLE_TIMEOUT_ARGS: {locals()}", sep="\n")
        time.sleep(10)
        return (RetryPolicy.RETRY_NEXT_HOST, ConsistencyLevel.QUORUM)

    def on_request_error(self, *args, **kwargs):
        print(f"ON_REQUEST_ERROR_TIMEOUT_ARGS: {locals()}", sep="\n")
        return self.get_retry_status(*args, **kwargs)


test_cl_map = {
    "10.0.157.102": "34.243.32.43",
    "10.0.186.188": "34.244.179.140",
    "10.0.164.214": "34.244.125.253",
}

oracle_cl_map = {
    "10.0.168.48": "34.247.189.204"
}

test_addr_trans = ComposeAddressTranslator()
test_addr_trans.set_map(test_cl_map)

oracl_addr_trans = ComposeAddressTranslator()
oracl_addr_trans.set_map(oracle_cl_map)


logging.basicConfig(level="INFO")
LOGGER = logging.getLogger("gemini_query")


qu = Queue()

# SELECT * FROM ks1.table1 WHERE pk0=-125 AND pk1=3611523799366506795 AND pk2=1145034405 AND pk3=-2297 AND pk4=3044488773455204433
# keys = {"pk0": -125, "pk1": 3611523799366506795, "pk2": 1145034405,
#         "pk3": -2297, "pk4": 3044488773455204433
#         }


# stm = "SELECT * FROM ks1.table1 WHERE pk0 = %(pk0)s and pk1 = %(pk1)s and pk2 = %(pk2)s and pk3 = %(pk3)s and pk4 = %(pk4)s"

test_cl = Cluster(contact_points=test_addr_trans.contact_points(),
                  address_translator=test_addr_trans,
                  control_connection_timeout=60,
                  connect_timeout=60
                  )

oracl_cl = Cluster(contact_points=oracl_addr_trans.contact_points(),
                   address_translator=oracl_addr_trans,
                   control_connection_timeout=60,
                   connect_timeout=60
                   )


test_session: Session = test_cl.connect()
test_session.default_timeout = 60
test_session.default_consistency_level = ConsistencyLevel.ANY

oracl_session: Session = oracl_cl.connect()

# t_stm = SimpleStatement("SELECT * FROM ks1.table1")
# t_stm.consistency_level = ConsistencyLevel.ALL
# o_stm = SimpleStatement("SELECT * FROM ks1.table1")
# o_stm.consistency_level = ConsistencyLevel.ONE
# test_results: ResultSet = test_session.execute(t_stm)
# orale_result: ResultSet = oracl_session.execute(o_stm)


# test_results: ResultSet = list(test_session.execute(stm, keys))
# orale_result: ResultSet = list(oracl_session.execute(stm, keys))
stm = SimpleStatement("select pk0, pk1, pk2, pk3, pk4, ck0, ck1, ck2 from ks1.table1")
stm.consistency_level = ConsistencyLevel.QUORUM
stm.fetch_size = 100
stm.retry_policy = RetryOnRead10TimesPolicy()

test_results: ResultSet = test_session.execute(stm, timeout=300.0, trace=False)
# orale_result: ResultSet = oracl_session.execute("select * from ks1.table1"),


# print(test_results, orale_result)
# print(len(test_results), len(orale_result), sep="::::")

# # LOGGER.info(PrettyPrinter(indent=4).pformat(list(test_results)))
# # LOGGER.info(PrettyPrinter(indent=4).pformat(list(orale_result)))
error = 0
error_rows = []
# with open("/home/abykov/tmp/error_rows.log", "a") as fp:


def put_to_queue(queue: Queue, keys: ResultSet):
    num = 0
    try:
        for key in keys:
            queue.put(key)
            num += 1
    except Exception as details:
        print(details)
        # print(keys.get_query_trace())
        for i in range(30):
            queue.put(None)
        print(num)

th = Thread(target=put_to_queue, args=(qu, test_results), daemon=True)
th.start()

th.join()

# for keys in test_results:
    # t_row = list(test_session.execute(f"select * from ks1.table1 \
    #                                     where pk0={keys.pk0} and pk1={keys.pk1} and pk2={keys.pk2} and \
    #                                     ck0='{keys.ck0}' and ck1={keys.ck1} and ck2='{keys.ck2}'"))[0]
    # o_row = list(oracl_session.execute(f"select * from ks1.table1 \
    #                                   where pk0={keys.pk0} and pk1={keys.pk1} and pk2={keys.pk2} and \
    #                                   ck0='{keys.ck0}' and ck1={keys.ck1} and ck2='{keys.ck2}'"))[0]
    # print(t_row, o_row, sep="--t_row d---\n---o_row q")
    # for f in t_row._fields:
    #     # LOGGER.info("t_row_field %s: %s", f, getattr(t_row, f))
    #     # LOGGER.info("o_row_field %s: %s", f, getattr(o_row, f))
    #     try:
    #         assert getattr(t_row, f) == getattr(o_row, f), f"fields are differs:\n t_row:{t_row} \n o_row:{o_row}"
    #     except Exception:
    #         print("row error found")
    #         error += 1
    #         error_rows.append((t_row, o_row))
    #         # fp.write(PrettyPrinter(indent=2).pformat((t_row, o_row)))

def check_row(queue: Queue, test_session: Session, oracl_session: Session):

    while True:
        try:
            keys = queue.get(timeout=30)
            if not keys:
                break
            where = " and ".join([f"{field}=%({field})s" for field in keys._fields])

            data = {key: getattr(keys, key) for key in keys._fields}
            cql_query = SimpleStatement(f"select * from ks1.table1 \
                                        where {where}")
            cql_query.consistency_level = ConsistencyLevel.QUORUM
            try:
                t_row = test_session.execute(cql_query, data).one()
            except Exception as details:
                print(details)
                continue
            cql_query.consistency_level = ConsistencyLevel.ONE
            try:
                o_row = oracl_session.execute(cql_query, data).one()
            except Exception as details:
                print(details)
                continue

            for f in t_row._fields:
                # LOGGER.info("t_row_field %s: %s", f, getattr(t_row, f))
                # LOGGER.info("o_row_field %s: %s", f, getattr(o_row, f))
                try:
                    assert getattr(t_row, f) == getattr(o_row, f), f"fields {f} are differs:\n t_row:{t_row} \n o_row:{o_row}"
                except Exception:
                    print("row error found")
                    raise
            print("Rows are same", current_thread())

        except Empty:
            pass

        finally:
            queue.task_done()


futures = []

with ThreadPoolExecutor(max_workers=30, thread_name_prefix='check_gemini') as pool:
    for _ in range(30):
        futures.append(pool.submit(check_row, queue=qu, test_session=test_session, oracl_session=oracl_session))


# while not qu.empty():
#     qu.join(60)


for future in futures:
    exc = future.exception()
    if exc:
        print(exc)

qu.join()

# for t_row, o_row in zip(test_results, orale_result):
#     assert t_row._fields == o_row._fields, f"different fields {t_row._fields}\n{o_row._fields}"
#     for f in t_row._fields:
#         # LOGGER.info("t_row_field %s: %s", f, getattr(t_row, f))
#         # LOGGER.info("o_row_field %s: %s", f, getattr(o_row, f))
#         try:
#             assert getattr(t_row, f) == getattr(o_row, f), f"fields are differs:\n t_row:{t_row} \n o_row:{o_row}"
#         except Exception:
#             print("row error found")
#             error += 1
#             error_rows.append((t_row, o_row))
#             # fp.write(PrettyPrinter(indent=2).pformat((t_row, o_row)))
#         # assert getattr(t_row, f) == getattr(o_row, f), f"""col: {f} have different value: t_row:{getattr(t_row, f)} ::: o_row: {getattr(o_row, f)} \nrows are differ:\n t_row:{t_row} \n o_row:{o_row}"""
#         # LOGGER.info("field %s same in test and oracle clusters", f)

# # 
