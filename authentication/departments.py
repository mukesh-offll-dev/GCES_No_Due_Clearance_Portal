"""
Centralized Dynamic Department / Branch Management Service.

Provides a single source of truth for all departments/branches across the portal.
Replaces all hardcoded branch lists and guarantees safety for existing students.
"""
import re
import logging
from datetime import datetime, timezone
from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import PyMongoError, DuplicateKeyError

from .mongo import departments_col, students_col

logger = logging.getLogger("nodue")

# Standard default departments seeded automatically if collection is empty
DEFAULT_DEPARTMENTS = [
    {"code": "CSE", "name": "Computer Science and Engineering", "is_active": True},
    {"code": "ECE", "name": "Electronics and Communication Engineering", "is_active": True},
    {"code": "EEE", "name": "Electrical and Electronics Engineering", "is_active": True},
    {"code": "MECH", "name": "Mechanical Engineering", "is_active": True},
    {"code": "CIVIL", "name": "Civil Engineering", "is_active": True},
    {"code": "MCT", "name": "Mechatronics Engineering", "is_active": True},
]


def ensure_default_departments():
    """
    Idempotently seeds default departments if none exist, and ensures any
    legacy branch codes already attached to students in students_col exist in departments_col.
    """
    try:
        total_depts = departments_col.count_documents({})
        now = datetime.now(timezone.utc)

        if total_depts == 0:
            seed_docs = []
            for d in DEFAULT_DEPARTMENTS:
                seed_docs.append({
                    "code": d["code"].strip().upper(),
                    "name": d["name"].strip(),
                    "is_active": d["is_active"],
                    "created_at": now,
                    "updated_at": now,
                })
            try:
                departments_col.insert_many(seed_docs, ordered=False)
                logger.info("Default departments seeded successfully.")
            except Exception as e:
                logger.warning("Default department seeding notice: %s", e)

        # Ensure any legacy branches present in students_col are registered
        try:
            student_branches = students_col.distinct("branch")
            existing_codes = set(departments_col.distinct("code"))
            for b in student_branches:
                if b and isinstance(b, str) and b.strip():
                    clean_b = b.strip().upper()
                    if clean_b not in existing_codes:
                        departments_col.update_one(
                            {"code": clean_b},
                            {"$setOnInsert": {
                                "code": clean_b,
                                "name": clean_b,
                                "is_active": True,
                                "created_at": now,
                                "updated_at": now,
                            }},
                            upsert=True
                        )
                        existing_codes.add(clean_b)
        except Exception as e:
            logger.warning("Could not sync legacy student branches: %s", e)

    except PyMongoError as exc:
        logger.error("ensure_default_departments failed: %s", exc)


def get_all_departments(active_only=False):
    """
    Returns a list of department dictionaries sorted by code.
    Includes sanitized string `id`.
    """
    try:
        ensure_default_departments()
        query = {"is_active": True} if active_only else {}
        cursor = departments_col.find(query).sort("code", 1)
        depts = []
        for doc in cursor:
            depts.append({
                "id": str(doc["_id"]),
                "_id": doc["_id"],
                "code": doc.get("code", ""),
                "name": doc.get("name", ""),
                "is_active": doc.get("is_active", True),
                "created_at": doc.get("created_at"),
                "updated_at": doc.get("updated_at"),
            })
        return depts
    except Exception as exc:
        logger.error("get_all_departments error: %s", exc)
        # Fallback to defaults in case of temporary DB failure
        return [{"id": d["code"], "code": d["code"], "name": d["name"], "is_active": d["is_active"]} for d in DEFAULT_DEPARTMENTS]


def get_active_departments():
    """Returns all active departments."""
    return get_all_departments(active_only=True)


def get_active_department_codes():
    """Returns list of active department codes (e.g. ['CSE', 'ECE', 'EEE', ...])."""
    return [d["code"] for d in get_active_departments()]


def get_all_department_codes():
    """Returns list of all department codes regardless of active status."""
    return [d["code"] for d in get_all_departments(active_only=False)]


def get_department_by_code(code):
    """Fetch single department by code."""
    if not code:
        return None
    try:
        clean_code = str(code).strip().upper()
        doc = departments_col.find_one({"code": clean_code})
        if doc:
            doc["id"] = str(doc["_id"])
        return doc
    except Exception as exc:
        logger.error("get_department_by_code error: %s", exc)
        return None


def get_department_by_id(dept_id):
    """Fetch single department by ObjectId string."""
    if not dept_id:
        return None
    try:
        doc = departments_col.find_one({"_id": ObjectId(dept_id)})
        if doc:
            doc["id"] = str(doc["_id"])
        return doc
    except (InvalidId, Exception) as exc:
        logger.error("get_department_by_id error: %s", exc)
        return None


def get_department_student_counts():
    """
    Returns a dictionary mapping department codes to the total count of enrolled students:
    { "CSE": 120, "ECE": 80, ... }
    """
    try:
        pipeline = [
            {"$group": {"_id": "$branch", "count": {"$sum": 1}}}
        ]
        results = list(students_col.aggregate(pipeline))
        counts = {}
        for r in results:
            branch = r.get("_id")
            if branch:
                counts[str(branch).strip().upper()] = r.get("count", 0)
        return counts
    except Exception as exc:
        logger.error("get_department_student_counts error: %s", exc)
        return {}


def validate_department_inputs(name, code, exclude_id=None):
    """
    Validates name and code inputs. Returns (clean_name, clean_code, error_message).
    """
    if not name or not str(name).strip():
        return None, None, "Department Name is required."

    if not code or not str(code).strip():
        return None, None, "Department Code is required."

    clean_name = re.sub(r"\s+", " ", str(name).strip())
    clean_code = re.sub(r"\s+", "", str(code).strip().upper())

    if len(clean_name) < 2:
        return None, None, "Department Name must be at least 2 characters."
    if len(clean_name) > 100:
        return None, None, "Department Name cannot exceed 100 characters."

    if len(clean_code) < 2:
        return None, None, "Department Code must be at least 2 characters."
    if len(clean_code) > 20:
        return None, None, "Department Code cannot exceed 20 characters."

    # Validate characters in code (alphanumeric, hyphen, underscore, ampersand)
    if not re.match(r"^[A-Z0-9\-_&]+$", clean_code):
        return None, None, "Department Code must contain only letters, numbers, hyphens, and underscores."

    # Uniqueness check for code
    code_query = {"code": clean_code}
    if exclude_id:
        try:
            code_query["_id"] = {"$ne": ObjectId(exclude_id)}
        except (InvalidId, TypeError):
            pass

    existing_code = departments_col.find_one(code_query)
    if existing_code:
        return None, None, f"Department Code '{clean_code}' already exists."

    # Uniqueness check for name (case-insensitive)
    name_query = {"name": {"$regex": f"^{re.escape(clean_name)}$", "$options": "i"}}
    if exclude_id:
        try:
            name_query["_id"] = {"$ne": ObjectId(exclude_id)}
        except (InvalidId, TypeError):
            pass

    existing_name = departments_col.find_one(name_query)
    if existing_name:
        return None, None, f"Department with name '{clean_name}' already exists."

    return clean_name, clean_code, None


def serialize_department(doc):
    """Returns a clean JSON-serializable dictionary without MongoDB ObjectIds or raw datetimes."""
    if not isinstance(doc, dict):
        return {}
    return {
        "id": str(doc.get("_id") or doc.get("id") or ""),
        "name": str(doc.get("name", "")),
        "code": str(doc.get("code", "")),
        "is_active": bool(doc.get("is_active", True)),
        "student_count": int(doc.get("student_count", 0)),
    }


def add_department(name, code, is_active=True):
    """
    Adds a new dynamic department after validating unique name and code.
    Returns (success, result_dict_or_error_str).
    """
    clean_name, clean_code, error = validate_department_inputs(name, code)
    if error:
        return False, error

    now = datetime.now(timezone.utc)
    doc = {
        "name": clean_name,
        "code": clean_code,
        "is_active": bool(is_active),
        "created_at": now,
        "updated_at": now,
    }

    try:
        res = departments_col.insert_one(doc)
        doc["id"] = str(res.inserted_id)
        doc["_id"] = res.inserted_id

        # Real-time WebSocket broadcast
        try:
            from .events import notify_department_changed
            notify_department_changed("department_added", {
                "id": doc["id"],
                "code": doc["code"],
                "name": doc["name"],
                "is_active": doc["is_active"],
            })
        except Exception as e:
            logger.warning("[WS] Failed to broadcast department addition: %s", e)

        return True, serialize_department(doc)
    except DuplicateKeyError:
        return False, f"Department Code '{clean_code}' already exists."
    except Exception as exc:
        logger.error("Failed to add department: %s", exc)
        return False, f"Database error: {str(exc)}"


def update_department(dept_id, name, code, is_active=None):
    """
    Updates department name and code.
    Returns (success, result_dict_or_error_str).
    """
    if not dept_id:
        return False, "Department ID is required."

    try:
        obj_id = ObjectId(dept_id)
    except (InvalidId, TypeError):
        return False, "Invalid Department ID."

    existing = departments_col.find_one({"_id": obj_id})
    if not existing:
        return False, "Department not found."

    clean_name, clean_code, error = validate_department_inputs(name, code, exclude_id=dept_id)
    if error:
        return False, error

    now = datetime.now(timezone.utc)
    update_fields = {
        "name": clean_name,
        "code": clean_code,
        "updated_at": now,
    }
    if is_active is not None:
        update_fields["is_active"] = bool(is_active)

    try:
        departments_col.update_one({"_id": obj_id}, {"$set": update_fields})
        updated = departments_col.find_one({"_id": obj_id})
        updated["id"] = str(updated["_id"])

        # Real-time WebSocket broadcast
        try:
            from .events import notify_department_changed
            notify_department_changed("department_updated", {
                "id": updated["id"],
                "code": updated["code"],
                "name": updated["name"],
                "is_active": updated["is_active"],
                "old_code": existing.get("code"),
            })
        except Exception as e:
            logger.warning("[WS] Failed to broadcast department update: %s", e)

        return True, serialize_department(updated)
    except DuplicateKeyError:
        return False, f"Department Code '{clean_code}' already exists."
    except Exception as exc:
        logger.error("Failed to update department: %s", exc)
        return False, f"Database error: {str(exc)}"


def toggle_department_status(dept_id, is_active=None):
    """
    Enables or disables a department. If is_active is None, flips current state.
    Returns (success, updated_doc_or_error_str).
    """
    if not dept_id:
        return False, "Department ID is required."

    try:
        obj_id = ObjectId(dept_id)
    except (InvalidId, TypeError):
        return False, "Invalid Department ID."

    existing = departments_col.find_one({"_id": obj_id})
    if not existing:
        return False, "Department not found."

    new_status = not existing.get("is_active", True) if is_active is None else bool(is_active)
    now = datetime.now(timezone.utc)

    try:
        departments_col.update_one(
            {"_id": obj_id},
            {"$set": {"is_active": new_status, "updated_at": now}}
        )
        updated = departments_col.find_one({"_id": obj_id})
        updated["id"] = str(updated["_id"])

        # Real-time WebSocket broadcast
        try:
            from .events import notify_department_changed
            notify_department_changed("department_toggled", {
                "id": updated["id"],
                "code": updated["code"],
                "name": updated["name"],
                "is_active": updated["is_active"],
            })
        except Exception as e:
            logger.warning("[WS] Failed to broadcast department toggle: %s", e)

        return True, serialize_department(updated)
    except Exception as exc:
        logger.error("Failed to toggle department status: %s", exc)
        return False, f"Database error: {str(exc)}"


def delete_department(dept_id):
    """
    Safely deletes a department ONLY if no students or records are associated with it.
    If records exist, deletion is blocked and an informative message is returned.
    Returns (success, message).
    """
    if not dept_id:
        return False, "Department ID is required."

    try:
        obj_id = ObjectId(dept_id)
    except (InvalidId, TypeError):
        return False, "Invalid Department ID."

    dept = departments_col.find_one({"_id": obj_id})
    if not dept:
        return False, "Department not found."

    dept_code = dept.get("code", "").strip().upper()

    # Safeguard check: Count students associated with this department
    try:
        associated_count = students_col.count_documents({
            "branch": {"$regex": f"^{re.escape(dept_code)}$", "$options": "i"}
        })
    except Exception as exc:
        logger.error("Error checking associated students for delete: %s", exc)
        return False, "Could not verify associated student records."

    if associated_count > 0:
        return (
            False,
            f"This department is currently associated with {associated_count} student(s) and historical records and cannot be permanently deleted. You can deactivate it instead."
        )

    # Safe to delete
    try:
        departments_col.delete_one({"_id": obj_id})

        # Real-time WebSocket broadcast
        try:
            from .events import notify_department_changed
            notify_department_changed("department_deleted", {
                "id": str(obj_id),
                "code": dept_code,
            })
        except Exception as e:
            logger.warning("[WS] Failed to broadcast department deletion: %s", e)

        return True, f"Department '{dept_code}' deleted successfully."
    except Exception as exc:
        logger.error("Failed to delete department: %s", exc)
        return False, f"Database error: {str(exc)}"
