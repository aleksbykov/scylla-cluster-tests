# CDC - Change Data Capture

## Description

Scylla 3.2 came with the new experimental feature called Change Data Capture (CDC for short). It makes it possible to monitor changes of data stored in the database.  
Such functionality is useful in many use cases like fraud detection and e-commerce where advanced replication of data across various systems, and monitoring of data are needed.  
Changes are logged in so-called CDC Log that reflects all the modifications and keeps them in the order they have happened

### Main parts

* cdc feature enabled as table option upon creating base table
* several modes:
    - Delta - logs only changes for modified column(s)
    - Preimage - logs state of column(s) before changing and Delta
    - Postimage - logs Delta and state of full row after changes applied
    - Preimage, Postimage - Preimage + Delta + Postimage. Allow to get full history of row
* cdc log tables is regular scylla table with Replication strategy as base table
* changes in base table are stored in cdc log table as streams, where stream_id is a partition in cdc log table
* stream ids generated on node start and stored in system table
* partition of base table and log table located on same node and shard


### Usefull links

* official documentation:
    * https://docs.scylladb.com/using-scylla/cdc/
    * https://docs.scylladb.com/using-scylla/cdc/cdc-stream-generations/
* Main dev document  
https://docs.google.com/document/d/1WI5QaDjTfL-aDKqHywi-ET0ueZhIb5sZxFDdrMafLqU/edit#
* Addintional dev documents
    * https://docs.google.com/document/d/1FG1xRlwc5wfLTbtptgOd1jmn8rMYYZ9T6hH06oj2vp4/edit
    * https://docs.google.com/document/d/1riJlkormkzZL0SYqQixxpsDXiURCjG0n_9KJS21zCto/edit

## SCT tests
SCT support testing the CDC feature. Used next tools for testing:
* cassandra-stress
* gemini
These tools used for running operations over base table. 

### Longevity tests
For that moment we have only next basic longevity test: **longevity-cdc-100gb-4h.yaml**. (__Thanks to Amos__). It supports all cdc modes and most of data manipulation

#### Highlights

* Don't require any specific tools to run basic operations over base table
* CDC_log table is reqular table, so the all infrastructure of sct is used to detect any errors which could occur for CDC log table

#### Lowlights

* For long time we don't have the tool to read from cdc log table. Now at least we have CDC stressor tool (written by Piotr D). Not yet implemented in longevity because doesn't support disruptive nemesis
* For that moment we still don't have ability to verify correctness of data cdc log table during tests
* Using c-s profiles to enable cdc on base table from the beginning, without altering base table with cdc options during test is running
* Need long time longevity test (48h)
* multi-region longevity

### Gemini tests
Gemini tool supports different table options from command line(Thanks to Pekka and Henrik), which allow to configure different table options including cdc options.  
Gemini tool run over base table more operations than c-s, including range deletions, altering table with columns and options. In addition gemini tool generate different table schemas

For that moment we have:
* gemini-3h-cdc-write.yaml
* gemini-3h-cdc-preimage-write.yaml
* gemini-3h-cdc-postimage-write.yaml

#### Highlights

* Allow to cover different table schema with different scylla data types

#### Lowlights

* Here we have same problems as with longevity test. No posibility to check correctness of data in cdc_log table and no supports for cdc stressor with disruptive nemesis


### Performance tests
Performance was one of main test which was requested by dev team. One of purpose was to have ability to compare results with some baseline, which was defined. 
For CDC performance test the baseline is base table with disabled cdc, and each cdc mode performance result is compared with it.
This functionality was implemented on current sct performance infrastructure and big thanks to Israel who help to polish this code.
Same cassandra-stress command and parameters is used for running CDC performance tests.  

At the end of 4.0 we got the cdc stressor tool which allow to read cdc log table in parallel with operations on base table.  



#### Highlights

* Added to sct new comparing functionlity for performance tests with baseline
* Added cdc-stressor tool using new dockers infrastructure on loader.
* Allow to find the performance degradation with cdc enabled. For that moment already implemented improvement for scylla which allot to increase performance in 3 times

#### Lowlights

* For long time test was running only for write operations for basetable. Because we don't have client which could read cdc log table.
* Need refactor our performance and result comparing code, because adding new feature become complex


### Future plan

* Next large step in CDC testing for Dev and QA team is implement one of CDC use case - Replicate data via cdc streams from scylla to another database( on first stage it will be another scylla)
* Add long time longevity to validate that large partitions for cdc log tables


## CDC Dtests

Base Dtest was added by dev team (mostly Piotr J and Piotr D) which cover basic functionality with simplest table schema and check correctness of cdc streams generation, data in cdc log table, grow and shrink cluster, etc
For that moment we also have test which check correctness data in cdc log table for all the scylla data types (native, collections, frozen collections)(this test was merged) also PRs mostly ready for UDT, CDC TTL, Batches, snapshots.

#### Highlights

* Have coverage for main functionality
* python driver version in Dtest was updated to support cdc functionality
* Dev team is very responsible and help a lot in investigating issues

#### Lowlights

* Different data types have different log records in cdc  for different operations with different cdc modes. For example if for base table cdc enabled with preimage and post image
