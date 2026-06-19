import os
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI")

client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=10000,   # 10 s — fail fast instead of 21 s hang
    connectTimeoutMS=10000,
    socketTimeoutMS=10000,
    tls=True,
    tlsAllowInvalidCertificates=True,  # handles lab/corporate DNS interception
)

db = client["no_dues_portal"]

# Collections
institution_logs = db["institution_logs"]
no_due_col       = db["no_due_requests"]
students_col     = db["students"]
promotion_logs   = db["promotion_logs"]
portal_settings  = db["portal_settings"]
