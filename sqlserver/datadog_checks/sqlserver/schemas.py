try:
    import datadog_agent
except ImportError:
    from ..stubs import datadog_agent
import time

from datadog_checks.sqlserver.const import (
    TABLES_IN_SCHEMA_QUERY,
    COLUMN_QUERY,
    PARTITIONS_QUERY,
    INDEX_QUERY,
    FOREIGN_KEY_QUERY,
    SCHEMA_QUERY,
    DB_QUERY
)

from datadog_checks.sqlserver.utils import (
    execute_query_output_result_as_a_dict, get_list_chunks
)
import pdb
import time
import json
import copy

from datadog_checks.base.utils.db.utils import default_json_event_encoding

class SubmitData: 
    MAX_COLUMN_COUNT  = 100_000

    # REDAPL has a 3MB limit per resource
    #TODO Report truncation to the backend
    MAX_TOTAL_COLUMN_COUNT = 250_000

    def __init__(self, submit_data_function, base_event, logger):
        self._submit_to_agent_queue = submit_data_function
        self._base_event = base_event
        self._log = logger

        self._columns_count  = 0
        self._total_columns_count = 0
        self.db_to_schemas = {} # dbname : { id : schema }
        self.db_info = {} # name to info

    def set_base_event_data(self, hostname, tags, cloud_metadata):
        self._base_event["host"] = hostname
        self._base_event["tags"] = tags
        self._base_event["cloud_metadata"] = cloud_metadata

    def reset(self):
        self._columns_count = 0
        self._total_columns_count = 0
        self.db_to_schemas = {}
        self.db_info = {}
    
    def store_db_info(self, db_name, db_info):
        self.db_info[db_name] = db_info

    def store(self, db_name, schema, tables, columns_count):
        self._columns_count += columns_count
        self._total_columns_count += columns_count
        schemas = self.db_to_schemas.setdefault(db_name, {})
        if schema["id"] in schemas:
            known_tables = schemas[schema["id"]].setdefault("tables",[])
            known_tables = known_tables + tables
        else:
            schemas[schema["id"]] = copy.deepcopy(schema) # TODO a deep copy ? kind of costs not much to be safe
            schemas[schema["id"]]["tables"] = tables
        if self._columns_count > self.MAX_COLUMN_COUNT:
            self._submit()

    #TODO P - disable for p.
    def tmp_modify_to_fit_in_postgres(self, db_info):
        if "collation" in db_info:
            del db_info["collation"]
        return db_info
    
    def exceeded_total_columns_number(self):
        return self._total_columns_count > self.MAX_TOTAL_COLUMN_COUNT

    def submit(self):
        if not bool(self.db_to_schemas):
            return
        self._columns_count  = 0
        event = {**self._base_event,
                 "metadata" : [],
                 "timestamp": time.time() * 1000
                 }
        for db, schemas_by_id in self.db_to_schemas.items():
            db_info = {}
            if db not in self.db_info:
                #TODO log error
                db_info["name"] = db
            else:
                db_info = self.db_info[db]
            #event["metadata"] =  event["metadata"] + [{**(self.tmp_modify_to_fit_in_postgres(db_info)), "schemas": list(schemas_by_id.values())}]
            event["metadata"] =  event["metadata"] + [{**(self.tmp_modify_to_fit_in_postgres(db_info)), "schemas": list(schemas_by_id.values())}]
            pdb.set_trace()
        #TODO Remove Debug Code, calculate tables and schemas sent : 
        schemas_debug = list(schemas_by_id.values())
        t_count = 0
        printed_first = False
        for schema in schemas_debug:
            t_count += len(schema['tables'])
            if not printed_first and len(schema['tables']) >0:
                printed_first = True
                self._log.warning("One of tables db {} schema {} table {}".format( list(self.db_to_schemas.keys()), schema['name'], schema['tables'][0]["name"]))

        self._log.warning("Boris Adding event to Agent queue with : {} schemas and {} tables.".format(len(schemas_debug), t_count))
        #END debug code
        json_event = json.dumps(event, default=default_json_event_encoding)
        #self._log.debug("Reporting the following payload for schema collection: {}".format(json_event))
        self._submit_to_agent_queue(json_event)
        self.db_to_schemas = {}

#TODO Introduce total max for data
class Schemas:
    def __init__(self, check):
        self._check = check 
        self._log = check.log
        self._tags = [t for t in check.tags if not t.startswith('dd.internal')]
        self._tags.append("boris:data")
        self.schemas_per_db = {} 
        """
        base_event = {
                "host": self._check.resolved_hostname,
                "agent_version": datadog_agent.get_version(),
                "dbms": "sqlserver", #TODO ?
                "kind": "", # TODO 
                #"collection_interval": self.schemas_collection_interval,
                #"dbms_version": self._payload_pg_version(),
                #"tags": self._tags_no_db,
                #"cloud_metadata": self._config.cloud_metadata,
            }
        """
        #TODO error is just so that payload passes, shoud be removed
        hostname = "error"
        if self._check.resolved_hostname is not None:
            hostname = self._check.resolved_hostname
        base_event = {
            "host": hostname,
            "agent_version": datadog_agent.get_version(),
            "dbms": "postgres", #TODO fake it until you make it - trying to pass this data as postgres for now
            "kind": "pg_databases", # TODO pg_databases - will result in KindPgDatabases and so processor would thing its postgres 
            "collection_interval": 0.5, #dummy
            "dbms_version": "v14.2", #dummy but may be format i v11 is important ?
            "tags": self._tags, #in postgres it's no DB.
            "cloud_metadata": self._check._config.cloud_metadata,
        }

        self._dataSubmitter = SubmitData(self._check.database_monitoring_metadata, base_event, self._log)

        # These are fields related to the work to do while doing the initial intake
        # for diffs there should eb a self._done_db_list which will be used to see if new dbs have appeared/disappeared.
        self._databases_to_query = []
        self._current_table_list = None
        self._current_schema_list = None
        self._number_of_collected_tables = 0 #TODO later switch to columns

    def reset_data_collection(self):
        self._current_table_list = None  
        self._current_schema_list = None
        self._number_of_collected_tables = 0
       
    def _init_schema_collection(self):
        currently_known_databases = self._check.get_databases()
        if len(self._databases_to_query) == 0:
            self._databases_to_query = self._check.get_databases()
            return  
        else:
            if self._databases_to_query[0] not in currently_known_databases:
                #TODO if db dissapeared we invalidate indexes should be done in exception treatment of use DB ?
                #if DB is not there the first use db will throw and we continue until we find an existing db or exaust the list
                # the idea is always finish the existing DB list and then run "diff" logic which will create a new list of "tasks"
                self.reset_data_collection()

   #TODO update this at the very end as it constantly changing
    """schemas data struct is a dictionnary with key being a schema name the value is
    schema
    dict:
        "name": str
        "id": str
        "principal_id": str
        "tables" : []
            id : str
            name : str
            columns: list of columns                  
                "columns": dict
                    name: str
                    data_type: str
                    default: str
                    is_nullable : str
            indexes : list of indexes - important
            foreign_keys : list of foreign keys
            partitions useful to know the number 
    """
    
    #sends all the data in one go but split in chunks (like Seth's solution)
    def collect_schemas_data(self):
        self._dataSubmitter.reset()
        start_time = time.time()
        self._log.warning("Starting schema collection {}".format(start_time))
        # for now only setting host and tags and metada
        self._dataSubmitter.set_base_event_data(self._check.resolved_hostname, self._tags, self._check._config.cloud_metadata)
        #returns Stop, Stop == True.
        def fetch_schema_data(cursor, db_name):
            db_info  = self._query_db_information(db_name, cursor)
            schemas = self._query_schema_information(cursor)
            self._dataSubmitter.store_db_info(db_name, db_info)
            chunk_size = 50
            for schema in schemas:
                if schema['name'] != 'test_schema':
                    continue
                tables = self._get_tables(schema, cursor)  
                #TODO sorting is purely for testing
                sorted_tables = sorted(tables, key=lambda x: x['name'])          
                tables_chunk = list(get_list_chunks(sorted_tables, chunk_size))
                for tables_chunk in tables_chunk:
                    if self._dataSubmitter.exceeded_total_columns_number():
                        self._log.warning("Truncated data due to the max limit, stopped on db - {} on schema {}".format(db_name, schema["name"]))
                        return True
                    self._log.warning("elapsed time {}".format(time.time() - start_time))

                    start_get_tables_time = time.time()
                    columns_count, tables_info = self._get_tables_data(tables_chunk, schema, cursor)
                    self._log.warning("_get_tables_data time {}".format(time.time() - start_get_tables_time))

                    start_store_time = time.time()
                    self._dataSubmitter.store(db_name, schema, tables_info, columns_count)  
                    self._log.warning("store time {}".format(time.time() - start_store_time))

                    start_submit_time = time.time()
                    self._dataSubmitter.submit() # we force submit after each 50 tables chunk
                    self._log.warning("submit time {}".format(time.time() - start_submit_time))
                if len(tables) == 0:
                    self._dataSubmitter.store(db_name, schema, [], 0)
            # we want to submit for each DB separetly for clarity
            self._dataSubmitter.submit()
            self._log.error("Finished collecting for DB - {} elapsed time {}".format(db_name, time.time() - start_time))
            return False
        self._check.do_for_databases(fetch_schema_data, self._check.get_databases())
        # submit the last chunk of data if any
        self._log.error("Finished collect_schemas_data")
        self._dataSubmitter.submit()


    def _query_db_information(self, db_name, cursor):
        db_info = execute_query_output_result_as_a_dict(DB_QUERY.format(db_name), cursor)
        if len(db_info) == 1:
            return db_info[0]
        else:
            return None
    # TODO how often ?

    #TODOTODO do we need this map/list format if we are not dumping in json ??? May be we need to send query results as they are ? 

    #TODO Looks fine similar to Postgres, do we need to do someting with prinicipal_id
    # or reporting principal_id is ok
    def _query_schema_information(self, cursor):

        # principal_id is kind of like an owner not sure if need it.
        self._log.debug("collecting db schemas")
        self._log.debug("Running query [%s]", SCHEMA_QUERY)
        cursor.execute(SCHEMA_QUERY)
        schemas = []
        columns = [i[0] for i in cursor.description]
        schemas = [dict(zip(columns, [str(item) for item in row])) for row in cursor.fetchall()]
        #TODO we can refactor it , doesnt have to have a tables :[] if there is nothing. 
        for schema in schemas:
            schema["tables"] = []
        self._log.debug("fetched schemas len(rows)=%s", len(schemas))
        return schemas
        
    #TODO collect diffs : we need to take care of new DB / removed DB . schemas new removed
    # will nedd a separate query for changed indexes
    def _get_tables_data(self, table_list, schema, cursor):
        if len(table_list) == 0:
            return
        name_to_id = {}
        id_to_all = {}
        #table_names = ",".join(["'{}'".format(t.get("name")) for t in table_list])
        #OBJECT_NAME is needed to make it work for special characters 
        table_ids_object = ",".join(["OBJECT_NAME({})".format(t.get("id")) for t in table_list])
        table_ids = ",".join(["{}".format(t.get("id")) for t in table_list])
        for t in table_list:
            name_to_id[t["name"]] = t["id"] 
            id_to_all[t["id"]] = t
        total_columns_number  = self._populate_with_columns_data(table_ids_object, name_to_id, id_to_all, schema, cursor)
        self._populate_with_partitions_data(table_ids, id_to_all, cursor) #TODO P DISABLED as postgrss backend accepts different data model
        self._populate_with_foreign_keys_data(table_ids, id_to_all, cursor) #TODO P DISABLED as postgrss backend accepts different data model
        self._populate_with_index_data(table_ids, id_to_all, cursor) #TODO P DISABLED as postgrss backend accepts different data model
        # unwrap id_to_all
        return total_columns_number, list(id_to_all.values())

    # TODO refactor the next 3 to have a base function when everythng is settled.
    def _populate_with_columns_data(self, table_ids, name_to_id, id_to_all, schema, cursor):
        # get columns if we dont have a dict here unlike postgres
        start_time = time.time()
        cursor.execute(COLUMN_QUERY.format(table_ids, schema["name"]))
        self._log.warning("Executed columns query for {} seconds".format(time.time() - start_time))
        start_time_fetch = time.time()
        data = cursor.fetchall()
        self._log.warning("Executed cursor.fetchall()for {} seconds".format(time.time() - start_time_fetch))
        start_time_rest = time.time()
        columns = []
        #TODO we need it cause if I put AS default its a forbidden key word and to be inline with postgres we need it
        for i in cursor.description:
            if str(i[0]).lower() == "column_default":
                columns.append("default")
            else:
                columns.append(str(i[0]).lower())
        

        rows = [dict(zip(columns, [str(item) for item in row])) for row in data]       
        for row in rows:
            table_id = name_to_id.get(str(row.get("table_name")))
            if table_id is not None:
                # exclude "table_name" from the row dict
                row.pop("table_name", None)
                if "nullable" in row:
                    if row["nullable"].lower() == "no" or row["nullable"].lower() == "false":
                        #to make compatible with postgres 
                        row["nullable"] = False
                    else:
                        row["nullable"] = True
                id_to_all.get(table_id)["columns"] = id_to_all.get(table_id).get("columns",[]) + [row]
        self._log.warning("Executed loops for {} seconds".format(time.time() - start_time_rest))
        return len(data)
    
    def _populate_with_partitions_data(self, table_ids, id_to_all, cursor):
        cursor.execute(PARTITIONS_QUERY.format(table_ids))
        columns = [str(i[0]).lower() for i in cursor.description] 
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        for row in rows:
            id  = row.pop("id", None)
            if id is not None:
                #TODO what happens if not found ? 
                id_to_all.get(str(id))["partitions"] = row
            else:
                print("todo error")
            row.pop("id", None)
        print("end")

    def _populate_with_index_data(self, table_ids, id_to_all, cursor):
        cursor.execute(INDEX_QUERY.format(table_ids))
        columns = [str(i[0]).lower() for i in cursor.description] 
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        for row in rows:
            id  = row.pop("id", None)
            if id is not None:
                id_to_all.get(str(id))["indexes"] = row
            else:
                print("todo error")
            row.pop("id", None)
        print("end")

    def _populate_with_foreign_keys_data(self, table_ids, id_to_all, cursor):
            cursor.execute(FOREIGN_KEY_QUERY.format(table_ids))
            columns = [str(i[0]).lower() for i in cursor.description] 
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            for row in rows:
                id  = row.pop("id", None)
                if id is not None:
                    id_to_all.get(str(id))["foreign_keys"] = row
                else:
                    print("todo error")  
            print("end")
        #return execute_query_output_result_as_a_dict(COLUMN_QUERY.format(table_name, schema_name), cursor)
    
        
    #TODO in SQLServer partitioned child tables should have the same object_id might be worth checking with a test.

    #TODOTODO do we need this map/list format if we are not dumping in json ??? May be we need to send query results as they are ? 
    def _get_tables(self, schema, cursor):
        cursor.execute(TABLES_IN_SCHEMA_QUERY.format(schema["id"]))
        columns = [str(i[0]).lower() for i in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()] #TODO may be more optimal to patch columns with index etc 
        # rows = [dict(zip(columns + ["columns", "indexes", "partitions", "foreign_keys"], row + [[], [], [], []])) for row in cursor.fetchall()] #TODO may be this works
        #return [ {"id" : row["object_id"], "name" : row['name'], "columns" : [], "indexes" : [], "partitions" : [], "foreign_keys" : []} for row in rows ]     # TODO P disabled because of postgres later enable             
        return [ {"id" : str(row["object_id"]), "name" : row['name'], "columns" : []} for row in rows ]  

    #TODO table 1803153469 is in  sys.indexes but not in sys.index_columns ... shell we do something about it ?


    #TODO its hard to get the partition key - for later ? 

        # TODO check out sys.partitions in postgres we deliver some data about patitions
        # "partition_key": str (if has partitions) - equiv ? 
        # may be use this  https://littlekendra.com/2016/03/15/find-the-partitioning-key-on-an-existing-table-with-partition_ordinal/
        # for more in depth search, it's not trivial to determine partition key like in Postgres
       