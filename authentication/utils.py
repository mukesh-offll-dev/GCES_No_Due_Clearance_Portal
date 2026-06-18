 
from datetime import datetime, timedelta
import cloudinary.uploader
from cloudinary.exceptions import NotFound, BadRequest


def save_receipt(file):
    result = cloudinary.uploader.upload(
        file,
        folder="no_due_receipts",
        resource_type="auto"   # 🔥 PDF + image both
    )
    return result["secure_url"]   # 🔥 URL save


         

def reset_expired_no_dues(no_due_col):
    now = datetime.now()

    def delete_cloudinary_file(public_id):
        try:
            # Try RAW first (PDF)
            cloudinary.uploader.destroy(
                public_id,
                resource_type="raw"
            )
        except (NotFound, BadRequest):
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

        no_due_col.update_one(
            {"_id": req["_id"]},
            {"$set": {
                "status": "NOT_SENT",
                "receipt_url": None,
                "cloudinary_public_id": None,
                "updated_at": now
            }}
        )

    # ================= APPROVED → NOT_SENT (5 mins) =================
    expired_approved = no_due_col.find({
        "status": "APPROVED",
        "updated_at": {"$lte": now - timedelta(minutes=5)}
    })

    for req in expired_approved:
        if req.get("office") == "HOSTEL":
            public_id = req.get("cloudinary_public_id")
            if public_id:
                delete_cloudinary_file(public_id)

        no_due_col.update_one(
            {"_id": req["_id"]},
            {"$set": {
                "status": "NOT_SENT",
                "receipt_url": None,
                "cloudinary_public_id": None,
                "updated_at": now
            }}
        )

    # Run the promotion check synchronously to ensure real-time updates on reload
    from .scheduler import check_and_promote_students
    try:
        check_and_promote_students()
    except Exception as e:
        print(f"[Synchronous Promotion Check Error]: {e}")




