# utils.py
import os
from django.conf import settings
from django.core.files.storage import default_storage
from datetime import datetime, timedelta
import cloudinary.uploader

def save_receipt(file):
    result = cloudinary.uploader.upload(
        file,
        folder="no_due_receipts",
        resource_type="auto"   # 🔥 PDF + image both
    )
    return result["secure_url"]   # 🔥 URL save



def reset_expired_no_dues(no_due_col):
    now = datetime.now()

    # PENDING → NOT_SENT after 1 min
    no_due_col.update_many(
        {
            "status": "PENDING",
            "created_at": {"$lte": now - timedelta(minutes=1)}
        },
        {
            "$set": {
                "status": "NOT_SENT",
                "updated_at": now
            }
        }
    )

    # APPROVED → NOT_SENT after 5 mins
    no_due_col.update_many(
        {
            "status": "APPROVED",
            "updated_at": {"$lte": now - timedelta(minutes=5)}
        },
        {
            "$set": {
                "status": "NOT_SENT",
                "updated_at": now
            }
        }
    )


