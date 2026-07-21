 
from datetime import datetime, timedelta, timezone
import cloudinary.uploader
from cloudinary.exceptions import NotFound, BadRequest


def save_receipt(file):
    result = cloudinary.uploader.upload(
        file,
        folder="no_due_receipts",
        resource_type="auto"   # 🔥 PDF + image both
    )
    return result["secure_url"]   # 🔥 URL save


def reset_expired_cooldowns(no_due_col):
    now_utc = datetime.now(timezone.utc)
    now_naive = datetime.now()
    
    # Reset cooldowns where cooldown_expiry has passed
    query = {
        "cooldown_expiry": {"$exists": True, "$ne": None},
        "$or": [
            {"cooldown_expiry": {"$lte": now_utc}},
            {"cooldown_expiry": {"$lte": now_naive}}
        ]
    }
    
    no_due_col.update_many(
        query,
        {
            "$set": {
                "attempts_used": 0,
                "status": "NOT_SENT",
                "reject_reason": None,
                "updated_at": now_naive
            },
            "$unset": {
                "cooldown_expiry": "",
                "second_rejection_at": ""
            }
        }
    )


def reset_expired_no_dues(no_due_col):
    now = datetime.now()

    # Reset any expired 24-hour cooldowns
    reset_expired_cooldowns(no_due_col)

    def delete_cloudinary_file(public_id):
        try:
            # Try RAW first (PDF)
            cloudinary.uploader.destroy(
                public_id,
                resource_type="raw"
            )
        except Exception:
            # Try IMAGE (jpg/png)
            try:
                cloudinary.uploader.destroy(
                    public_id,
                    resource_type="image"
                )
            except Exception:
                pass  # final ignore (already deleted / invalid)

    # ================= PENDING → NOT_SENT (3 mins) =================
    expired_pending = no_due_col.find({
        "status": "PENDING",
        "created_at": {"$lte": now - timedelta(minutes=3)}
    })

    for req in expired_pending:
        if req.get("office") == "HOSTEL":
            public_id = req.get("cloudinary_public_id")
            if public_id:
                delete_cloudinary_file(public_id)

        current_attempts = req.get("attempts_used", 1)
        new_attempts = max(0, current_attempts - 1)

        no_due_col.update_one(
            {"_id": req["_id"]},
            {"$set": {
                "status": "NOT_SENT",
                "attempts_used": new_attempts,
                "receipt_url": None,
                "cloudinary_public_id": None,
                "updated_at": now
            }}
        )







