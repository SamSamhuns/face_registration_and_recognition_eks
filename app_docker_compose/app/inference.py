"""
Inference functions for registering and recognizing face with trtserver models and save/search with milvus database server
"""
import os

import redis
import pymysql
from pymilvus import connections, MilvusException
from pymilvus import Collection, CollectionSchema, FieldSchema, DataType, utility

from triton_server.inference_trtserver import run_inference


_REDIS_HOST = os.getenv("REDIS_HOST", default="127.0.0.1")
_REDIS_PORT = os.getenv("REDIS_PORT", default=6379)

_MYSQL_HOST = os.getenv("MYSQL_HOST", default="127.0.0.1")
_MYSQL_PORT = os.getenv("MYSQL_PORT", default=3306)
_MYSQL_USER = os.getenv("MYSQL_USER", default="user")
_MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", default="pass")
_MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", default="default")
_MYSQL_PERSON_TABLE = os.getenv("MYSQL_PERSON_TABLE", default="person")

_MILVUS_HOST = os.getenv("MILVUS_HOST", default="127.0.0.1")
_MILVUS_PORT = os.getenv("MILVUS_PORT", default=19530)
_VECTOR_DIM = 128
_METRIC_TYPE = "L2"
_INDEX_TYPE = "IVF_FLAT"
COLLECTION_NAME = 'faces'

# connect to Redis
redis_conn = redis.Redis(host=_REDIS_HOST, port=_REDIS_PORT)

# Connect to MySQL
mysql_conn = pymysql.connect(
    host=_MYSQL_HOST,
    port=_MYSQL_PORT,
    user=_MYSQL_USER,
    password=_MYSQL_PASSWORD,
    db=_MYSQL_DATABASE
)

# connect to milvus server
connections.connect(alias="default", host=_MILVUS_HOST, port=_MILVUS_PORT)

# load collection
# if collection is not present create one
if not utility.has_collection(COLLECTION_NAME):
    fields = [
        FieldSchema(name="id", dtype=DataType.INT64,
                    descrition="ids", is_primary=True, auto_id=True),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR,
                    descrition="embedding vectors", dim=_VECTOR_DIM),
        FieldSchema(name="person_id", dtype=DataType.INT64,
                    descrition="persons unique id")
    ]
    schema = CollectionSchema(
        fields=fields, description='face recognition system')
    collection = Collection(name=COLLECTION_NAME,
                            consistency_level="Strong",
                            schema=schema, using='default')
    print(f"Collection {COLLECTION_NAME} created.✅️")

    # Indexing the collection
    print("Indexing the Collection...🔢️")
    # create IVF_FLAT index for collection.
    index_params = {
        'metric_type': _METRIC_TYPE,
        'index_type': _INDEX_TYPE,
        'params': {"nlist": 4096}
    }
    collection.create_index(field_name="embedding", index_params=index_params)
    print(f"Collection {COLLECTION_NAME} indexed.✅️")
else:
    print(f"Collection {COLLECTION_NAME} present already.✅️")
    collection = Collection(COLLECTION_NAME)


# load collection into memory
collection.load()


def get_registered_face(person_id: int) -> dict:
    """
    Get registered face by person_id.
    """
    expr = f'person_id == {person_id}'
    try:
        results = collection.query(
            expr=expr,
            offset=0,
            limit=10,
            output_fields=["person_id"],
            consistency_level="Strong")
        if not results:
            return {"status": "failed", 
                    "message": f"Person with id {person_id} not found in database"}

        found_person_name = results[0]["person_id"]
        print(f"Person {found_person_name} found in database.✅️")
        return {"status": "success", 
                "message": f"Person {found_person_name} found"}
    except MilvusException as excep:
        print(excep)
        return {"status": "failed", 
                "message": excep}


def unregister_face(person_id: int) -> dict:
    """
    Deletes a registered face based on the unique person_id.
    Must use expr with the term expression `in` for delete operations
    """
    expr = f'person_id in [{person_id}]'

    try:
        collection.delete(expr)
        print(f"Person with id {person_id} unregistered from database.✅️")
        return {"status": "success", "message": f"Person with id {person_id} unregistered"}
    except MilvusException as excep:
        print(excep)
        return {"status": "failed", "message": f"Person with id {person_id} couldn't be unregistered"}


def register_face(model_name: str,
                  file_path: str,
                  threshold: float,
                  person_data: dict) -> dict:
    """
    Detects faces in image from the file_path and 
    saves the face feature vector & the related person_data dict.
    person_data dict should be based on the init.sql table schema
    """
    pred_dict = run_inference(
        file_path,
        face_feat_model=model_name,
        face_det_thres=threshold,
        face_bbox_area_thres=0.10,
        face_count_thres=1,
        return_mode="json")

    if pred_dict["status"] == 0 and not pred_dict["face_detections"]:
        return {"status": "failed", "message": "No faces were detected in the image"}
    if pred_dict["status"] < 0:
        pred_dict["status"] = "failed"
        return pred_dict

    person_id = person_data["id"]  # uniq person id from user input
    # check if face already exists in milvus db
    if get_registered_face(person_id)["status"] == "success":
        return {"status": "failed", 
                "message": f"person with id {person_id} already exists in milvus db"}
    face_vector = pred_dict["face_feats"][0].tolist()
    data = [[face_vector], [person_id]]

    # insert face_vector into milvus collection
    try:
        collection.insert(data)
        print(f"Vector for {person_data['name']} inserted into milvus db.✅️")
    except MilvusException as e:
        print(f"milvus collection insert failed ❌. {e}")
        return {"status": "failed", "message": "milvus insertion error"}
    # After final entity is inserted, it is best to call flush to have no growing segments left in memory
    collection.flush()

    # insert data into mysql table with param binding
    query = (f"INSERT INTO {_MYSQL_PERSON_TABLE}" +
             " (id, name, birthdate, country, city, title, org)" +
             " VALUES (%s, %s, %s, %s, %s, %s, %s)")
    values = (person_id, person_data["name"],  person_data["birthdate"],  person_data["country"],
              person_data["city"],  person_data["title"],  person_data["org"])
    cursor = mysql_conn.cursor()
    try:
        cursor.execute(query, values)
        mysql_conn.commit()
        print(f"{person_data['name']} info inserted into mysql db.✅️")
    except pymysql.Error as e:
        print(f"mysql insert failed ❌. {e}")
        unregister_face(person_id)  # del person with person_id from milvus as well
        return {"status": "failed", "message": "mysql insertion error"}
    finally:
        cursor.close()

    # cache data in redis
    redis_key = f"{_MYSQL_PERSON_TABLE}_{person_id}"
    person_data["birthdate"] = str(person_data["birthdate"])
    redis_conn.hset(redis_key, mapping=person_data)  # hash set data
    redis_conn.expire(redis_key, 3600)  # cache for 1 hour

    return {"status": "success", 
            "message": f"Person {person_data['person_name']} successfully registered"}


def recognize_face(model_name: str,
                   file_path: str,
                   threshold: float,
                   face_dist_threshold: float = 0.1) -> dict:
    """
    Detects faces in image from the file_path and finds the most similar face vector
    from a set of saved face vectors
    """
    pred_dict = run_inference(
        file_path,
        face_feat_model=model_name,
        face_det_thres=threshold,
        face_bbox_area_thres=0.10,
        face_count_thres=1,
        return_mode="json")

    if pred_dict["status"] == 0 and not pred_dict["face_detections"]:
        return {"status": "failed", "message": "No faces were detected in the image"}
    if pred_dict["status"] < 0:
        pred_dict["status"] = "failed"
        return pred_dict

    face_vector = pred_dict["face_feats"]
    # run a vector search and return the closest face with the L2 metric
    search_params = {"metric_type": "L2",  "params": {"nprobe": 2056}}
    results = collection.search(
        data=face_vector,
        anns_field="embedding",
        param=search_params,
        limit=3,
        output_fields=["person_id"])
    if not results:
        return {"status": "failed", "message": "No saved face entries found in database"}

    results = sorted(results, key=lambda k: k.distances)

    face_dist = results[0].distances[0]
    person_id = results[0][0].entity.get("person_id")
    if face_dist > face_dist_threshold:
        return {"status": "success", "message": "No similar faces were found in the database"}

    return {"status": "success", "message": f"Detected face matches id {person_id}", "match_id": person_id}
