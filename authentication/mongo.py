import os
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI")

client = MongoClient(MONGO_URI)
db = client["no_dues_portal"]
# Collections (future use)
institution_logs = db["institution_logs"]
no_due_col = db["no_due_requests"]
students_col = db["students"]

