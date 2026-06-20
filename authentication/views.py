from django.shortcuts import render, redirect
from django.contrib import messages
from datetime import datetime , date, timedelta
from .decorators import institution_login_required 
from .institution_users import INSTITUTION_USERS
from .mongo import institution_logs , students_col , no_due_col, portal_settings
from bson.errors import InvalidId
from bson import ObjectId
from django.conf import settings
from .utils import save_receipt , reset_expired_no_dues
import re
import cloudinary.uploader
from django.http import HttpResponse
from openpyxl import Workbook ,load_workbook 

# ReportLab Imports
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.pdfgen import canvas 


# =================== HELPER: ROLE → REDIRECT NAME ====================
_ROLE_REDIRECT = {
    "LIBRARY":    "library_dashboard",
    "HOSTEL":     "hostel_dashboard",
    "COLLEGE":    "college_dashboard",
    "FACULTY":    "faculty_dashboard",
    "DEPARTMENT": "department_dashboard",
    "STUDENT":    "student_dashboard",
}


# ================= INDEX = INSTITUTION LOGIN =================
def index(request):
    # ── If a valid session already exists, skip the login page ──
    role = request.session.get("role")
    if role:
        dest = _ROLE_REDIRECT.get(role)
        if dest:
            return redirect(dest)

    if request.method == "POST":
        office = request.POST.get("office")
        username = request.POST.get("username")
        password = request.POST.get("password")
        department = request.POST.get("department")

        # ================= DEPARTMENT LOGIN =================
        if office == "department":
            dept = INSTITUTION_USERS["department"].get(department)

            if dept and dept["username"] == username and dept["password"] == password:
                request.session["role"] = "DEPARTMENT"
                request.session["department"] = department

                institution_logs.insert_one({
                    "office": "DEPARTMENT",
                    "department": department,
                    "username": username,
                    "login_time": datetime.now()
                })

                return redirect("department_dashboard")

        # ================= OTHER OFFICES =================
        else:
            office_data = INSTITUTION_USERS.get(office)

            if office_data and office_data["username"] == username and office_data["password"] == password:
                role = office_data["role"]
                request.session["role"] = role

                institution_logs.insert_one({
                    "office": role,
                    "username": username,
                    "login_time": datetime.now()
                })

                # 🔀 REDIRECT BASED ON ROLE
                if role == "LIBRARY":
                    return redirect("library_dashboard")

                elif role == "HOSTEL":
                    return redirect("hostel_dashboard")

                elif role == "COLLEGE":
                    return redirect("college_dashboard")

                elif role == "FACULTY":
                    return redirect("faculty_dashboard")

        # ❌ INVALID LOGIN
        messages.error(request, "Invalid credentials")

    return render(request, "index.html")


def student_login(request):
    # ── Already logged in as STUDENT? Skip login ──
    if request.session.get("role") == "STUDENT" and request.session.get("student_id"):
        return redirect("student_dashboard")

    if request.method == "POST":
        reg_no = request.POST.get("reg_no", "").strip()
        dob = request.POST.get("dob", "").strip()

        # ✅ VALIDATIONS
        if not reg_no.isdigit() or len(reg_no) != 12:
            request.session["student_error"] = "Register Number must be 12 digits"
            return redirect("index")

        try:
            datetime.strptime(dob, "%Y-%m-%d")
        except ValueError:
            request.session["student_error"] = "Invalid Date of Birth"
            return redirect("index")

        # 🔎 CHECK STUDENT IN DB
        student = students_col.find_one({
            "reg_no": reg_no,
            "dob": dob
        })

        if not student:
            request.session["student_error"] = "Invalid credentials"
            return redirect("index")

        # ================= LOGIN SUCCESS =================
        # 🔒 Cycle session ID (prevent session-fixation attacks).
        # Falls back silently if the session was never persisted yet.
        try:
            request.session.cycle_key()
        except Exception:
            request.session.flush()

        request.session["role"]       = "STUDENT"
        request.session["student_id"] = str(student["_id"])

        return redirect("student_dashboard")

    return redirect("index")


 

def check_no_due_access_status():
    settings_doc = portal_settings.find_one({"_id": "global_config"})
    if not settings_doc:
        return False
    enabled = settings_doc.get("no_due_access_enabled", False)
    if enabled:
        auto_disable_at = settings_doc.get("auto_disable_at")
        if auto_disable_at:
            if datetime.now() >= auto_disable_at:
                portal_settings.update_one(
                    {"_id": "global_config"},
                    {"$set": {"no_due_access_enabled": False}}
                )
                return False
        else:
            portal_settings.update_one(
                {"_id": "global_config"},
                {"$set": {"no_due_access_enabled": False}}
            )
            return False
    return enabled


@institution_login_required
def student_dashboard(request):
    if request.session.get("role") != "STUDENT":
        return redirect("index")

    reset_expired_no_dues(no_due_col)

    student_id = ObjectId(request.session["student_id"])
    student = students_col.find_one({"_id": student_id})

    # ── Determine offices based on student type ──
    student_type = student.get("student_type", "Hosteller")
    if student_type == "Day Scholar":
        offices = ["LIBRARY", "COLLEGE", "DEPARTMENT"]
        required_count = 3
    else:
        offices = ["LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"]
        required_count = 4

    existing = {
        d["office"]: d
        for d in no_due_col.find({"student_id": student_id})
    }

    dues = []
    all_approved = True   # 🔥 FLAG

    for office in offices:
        d = existing.get(office, {
            "office": office,
            "status": "NOT_SENT"
        })
        dues.append(d)

        # 🔴 if ANY required office not approved → false
        if d.get("status") != "APPROVED":
            all_approved = False

    # Query promotion logs for this student
    from .mongo import promotion_logs
    logs = list(promotion_logs.find({"student_id": student_id}).sort("promotion_time", -1))

    no_due_access_enabled = check_no_due_access_status()

    return render(request, "student_dashboard.html", {
        "student": student,
        "dues": dues,
        "all_approved": all_approved,
        "promotion_logs": logs,
        "student_type": student_type,
        "no_due_access_enabled": no_due_access_enabled,
    })

    
    
@institution_login_required
def update_student_profile(request):
    if request.method == "POST" and request.session.get("role") == "STUDENT":

        year = request.POST.get("year")
        semester = request.POST.get("semester")

        # 🔐 STRICT VALIDATION
        if not year.isdigit() or not (1 <= int(year) <= 4):
            return redirect("student_dashboard")

        if not semester.isdigit() or not (1 <= int(semester) <= 8):
            return redirect("student_dashboard")

        students_col.update_one(
            {"_id": ObjectId(request.session["student_id"])},
            {"$set": {
                "year": int(year),
                "semester": int(semester)
            }}
        )

    return redirect("student_dashboard")


@institution_login_required
def no_due_certificate(request):
    if request.session.get("role") != "STUDENT":
        return redirect("index")

    student_id = ObjectId(request.session["student_id"])

    student = students_col.find_one({"_id": student_id})

    # Fetch all approved no-dues
    dues = list(no_due_col.find({
        "student_id": student_id,
        "status": "APPROVED"
    }))

    # 🔐 Safety check
    student_type = student.get("student_type", "Hosteller")
    required_count = 3 if student_type == "Day Scholar" else 4
    if len(dues) < required_count:
        return redirect("student_dashboard")

    # Convert to simple dict for template
    no_dues_status = {
        "LIBRARY": "Completed",
        "COLLEGE": "Completed",
        "DEPARTMENT": "Completed"
    }
    if student_type == "Hosteller":
        no_dues_status["HOSTEL"] = "Completed"

    return render(request, "no_due_certificate.html", {
        "student": student,
        "no_dues": no_dues_status
    })



    
@institution_login_required
def send_hostel_request(request):
    if not check_no_due_access_status():
        messages.error(request, "No Due process is currently locked. Please contact your Faculty.")
        return redirect("student_dashboard")

    if request.method == "POST":

        receipt_url = None
        cloudinary_public_id = None

        if "receipt" in request.FILES:
            upload = cloudinary.uploader.upload(
                request.FILES["receipt"],
                folder="no_dues/hostel",
                resource_type="raw"
            )
            receipt_url = upload["secure_url"]
            cloudinary_public_id = upload["public_id"]

        no_due_col.insert_one({
            "student_id": ObjectId(request.session["student_id"]),
            "office": "HOSTEL",
            "last_payment_id": request.POST.get("payment_id"),
            "receipt_url": receipt_url,
            "cloudinary_public_id": cloudinary_public_id,
            "status": "PENDING",
            "created_at": datetime.now()
        })

    return redirect("student_dashboard")



@institution_login_required
def hostel_dashboard(request):
    if request.session.get("role") != "HOSTEL":
        return redirect("index")

    reset_expired_no_dues(no_due_col)

    branch = request.GET.get("branch")
    year = request.GET.get("year")
    semester = request.GET.get("semester")

    requests = []
    count = 0
    year_summary = {}
    branch_summary = {}

    # ================= 3️⃣ Branch + Year =================
    if branch and year:
        requests = list(no_due_col.aggregate([
            {"$match": {"office": "HOSTEL", "status": "PENDING"}},
            {"$lookup": {
                "from": "students",
                "localField": "student_id",
                "foreignField": "_id",
                "as": "student"
            }},
            {"$unwind": "$student"},
            {"$match": {
                "student.branch": branch,
                "student.year": int(year)
            }}
        ]))

        for r in requests:
            r["id"] = str(r["_id"])

        count = len(requests)

    # ================= 2️⃣ Only Branch =================
    elif branch:
        for y in [1, 2, 3, 4]:
            year_summary[y] = len(list(no_due_col.aggregate([
                {"$match": {"office": "HOSTEL", "status": "PENDING"}},
                {"$lookup": {
                    "from": "students",
                    "localField": "student_id",
                    "foreignField": "_id",
                    "as": "student"
                }},
                {"$unwind": "$student"},
                {"$match": {
                    "student.branch": branch,
                    "student.year": y
                }}
            ])))

    # ================= 1️⃣ Nothing Selected =================
    else:
        for b in ["CSE", "ECE", "EEE", "CIVIL", "MECH", "MCT"]:
            branch_summary[b] = len(list(no_due_col.aggregate([
                {"$match": {"office": "HOSTEL", "status": "PENDING"}},
                {"$lookup": {
                    "from": "students",
                    "localField": "student_id",
                    "foreignField": "_id",
                    "as": "student"
                }},
                {"$unwind": "$student"},
                {"$match": {"student.branch": b}}
            ])))

    return render(request, "hostel_dashboard.html", {
        "requests": requests,
        "count": count,
        "branch": branch,
        "year": year,
        "semester": semester,
        "branches": ["CSE","ECE","EEE","CIVIL","MECH","MCT"],
        "branch_summary": branch_summary,
        "year_summary": year_summary
    })


 

# ================= LIBRARY PAGE =================
@institution_login_required
def library_dashboard(request):
    if request.session.get("role") != "LIBRARY":
        return redirect("index")

    reset_expired_no_dues(no_due_col)

    branch = request.GET.get("branch")
    year = request.GET.get("year")
    semester = request.GET.get("semester")

    requests = []
    count = 0
    branch_summary = {}
    year_summary = {}

    # ================= 3️⃣ Branch + Year =================
    if branch and year:
        requests = list(no_due_col.aggregate([
            {"$match": {"office": "LIBRARY", "status": "PENDING"}},
            {"$lookup": {
                "from": "students",
                "localField": "student_id",
                "foreignField": "_id",
                "as": "student"
            }},
            {"$unwind": "$student"},
            {"$match": {
                "student.branch": branch,
                "student.year": int(year)
            }}
        ]))

        for r in requests:
            r["id"] = str(r["_id"])

        count = len(requests)

    # ================= 2️⃣ Branch only =================
    elif branch:
        for y in [1, 2, 3, 4]:
            year_summary[y] = len(list(no_due_col.aggregate([
                {"$match": {"office": "LIBRARY", "status": "PENDING"}},
                {"$lookup": {
                    "from": "students",
                    "localField": "student_id",
                    "foreignField": "_id",
                    "as": "student"
                }},
                {"$unwind": "$student"},
                {"$match": {
                    "student.branch": branch,
                    "student.year": y
                }}
            ])))

    # ================= 1️⃣ Nothing selected =================
    else:
        for b in ["CSE", "ECE", "EEE", "CIVIL", "MECH", "MCT"]:
            branch_summary[b] = len(list(no_due_col.aggregate([
                {"$match": {"office": "LIBRARY", "status": "PENDING"}},
                {"$lookup": {
                    "from": "students",
                    "localField": "student_id",
                    "foreignField": "_id",
                    "as": "student"
                }},
                {"$unwind": "$student"},
                {"$match": {"student.branch": b}}
            ])))

    return render(request, "institution_dashboard.html", {
        "office_name": "Library Office",
        "requests": requests,
        "branch": branch,
        "year": year,
        "semester": semester,
        "count": count,
        "branches": ["CSE","ECE","EEE","CIVIL","MECH","MCT"],
        "branch_summary": branch_summary,
        "year_summary": year_summary
    })




@institution_login_required
def bulk_approve(request):
    ids = request.POST.getlist("request_ids")
    object_ids = [ObjectId(i) for i in ids]

    if object_ids:
        no_due_col.update_many(
            {"_id": {"$in": object_ids}},
            {"$set": {"status": "APPROVED", "updated_at": datetime.now()}}
        )

    return redirect(request.META.get("HTTP_REFERER"))



@institution_login_required
def reject_request(request):
    req_id = request.POST.get("req_id")
    reason = request.POST.get("reason")

    # 🔹 Get existing request
    req = no_due_col.find_one({"_id": ObjectId(req_id)})

    # 🔥 If hostel + file exists → delete from Cloudinary
    if req and req.get("office") == "HOSTEL":
        public_id = req.get("cloudinary_public_id")
        if public_id:
            try:
                cloudinary.uploader.destroy(
                    public_id,
                    resource_type="raw"
                )
            except Exception:
                pass  # Ignore cloudinary deletion failure, proceed with DB status update


    # 🔁 Update DB
    no_due_col.update_one(
        {"_id": ObjectId(req_id)},
        {"$set": {
            "status": "REJECTED",
            "reject_reason": reason,
            "receipt_url": None,
            "cloudinary_public_id": None,
            "updated_at": datetime.now()
        }}
    )

    return redirect(request.META.get("HTTP_REFERER"))


@institution_login_required
def college_dashboard(request):
    if request.session.get("role") != "COLLEGE":
        return redirect("index")

    reset_expired_no_dues(no_due_col)

    branch = request.GET.get("branch")
    year = request.GET.get("year")
    semester = request.GET.get("semester")

    requests = []
    count = 0
    branch_summary = {}
    year_summary = {}

    # ================= 3️⃣ Branch + Year =================
    if branch and year:
        requests = list(no_due_col.aggregate([
            {"$match": {"office": "COLLEGE", "status": "PENDING"}},
            {"$lookup": {
                "from": "students",
                "localField": "student_id",
                "foreignField": "_id",
                "as": "student"
            }},
            {"$unwind": "$student"},
            {"$match": {
                "student.branch": branch,
                "student.year": int(year)
            }}
        ]))

        for r in requests:
            r["id"] = str(r["_id"])

        count = len(requests)

    # ================= 2️⃣ Branch only =================
    elif branch:
        for y in [1, 2, 3, 4]:
            year_summary[y] = len(list(no_due_col.aggregate([
                {"$match": {"office": "COLLEGE", "status": "PENDING"}},
                {"$lookup": {
                    "from": "students",
                    "localField": "student_id",
                    "foreignField": "_id",
                    "as": "student"
                }},
                {"$unwind": "$student"},
                {"$match": {
                    "student.branch": branch,
                    "student.year": y
                }}
            ])))

    # ================= 1️⃣ Nothing selected =================
    else:
        for b in ["CSE","ECE","EEE","CIVIL","MECH","MCT"]:
            branch_summary[b] = len(list(no_due_col.aggregate([
                {"$match": {"office": "COLLEGE", "status": "PENDING"}},
                {"$lookup": {
                    "from": "students",
                    "localField": "student_id",
                    "foreignField": "_id",
                    "as": "student"
                }},
                {"$unwind": "$student"},
                {"$match": {"student.branch": b}}
            ])))

    return render(request, "institution_dashboard.html", {
        "office_name": "College Office",
        "requests": requests,
        "branch": branch,
        "year": year,
        "semester": semester,
        "count": count,
        "branches": ["CSE","ECE","EEE","CIVIL","MECH","MCT"],
        "branch_summary": branch_summary,
        "year_summary": year_summary
    })


@institution_login_required
def department_dashboard(request):
    if request.session.get("role") != "DEPARTMENT":
        return redirect("index")

    reset_expired_no_dues(no_due_col)

    dept = request.session.get("department")
    year = request.GET.get("year")
    semester = request.GET.get("semester")

    requests = []
    count = 0
    year_summary = {}

    # ================= 2️⃣ Year selected =================
    if year:
        requests = list(no_due_col.aggregate([
            {"$match": {"office": "DEPARTMENT", "status": "PENDING"}},
            {"$lookup": {
                "from": "students",
                "localField": "student_id",
                "foreignField": "_id",
                "as": "student"
            }},
            {"$unwind": "$student"},
            {"$match": {
                "student.branch": dept,
                "student.year": int(year)
            }}
        ]))

        for r in requests:
            r["id"] = str(r["_id"])

        count = len(requests)

    # ================= 1️⃣ No year selected =================
    else:
        for y in [1, 2, 3, 4]:
            year_summary[y] = len(list(no_due_col.aggregate([
                {"$match": {"office": "DEPARTMENT", "status": "PENDING"}},
                {"$lookup": {
                    "from": "students",
                    "localField": "student_id",
                    "foreignField": "_id",
                    "as": "student"
                }},
                {"$unwind": "$student"},
                {"$match": {
                    "student.branch": dept,
                    "student.year": y
                }}
            ])))

    return render(request, "institution_dashboard.html", {
        "office_name": "Department",
        "requests": requests,
        "branch": dept,
        "year": year,
        "semester": semester,
        "count": count,
        "year_summary": year_summary
    })


@institution_login_required
def send_no_due_request(request):
    if not check_no_due_access_status():
        messages.error(request, "No Due process is currently locked. Please contact your Faculty.")
        return redirect("student_dashboard")

    if request.method == "POST":
        office = request.POST.get("office")

        data = {
            "student_id": ObjectId(request.session["student_id"]),
            "office": office,
            "status": "PENDING",
            "created_at": datetime.now()
        }

        # HOSTEL extra fields
        if office == "HOSTEL":
            if "receipt" in request.FILES:
                data["receipt"] = save_receipt(request.FILES["receipt"])
            data["last_payment_id"] = request.POST.get("payment_id")

        # 🔁 UPDATE if exists, else INSERT
        no_due_col.update_one(
            {
                "student_id": data["student_id"],
                "office": office
            },
            {"$set": data},
            upsert=True
        )

    return redirect("student_dashboard")


@institution_login_required
def retry_request(request):
    if not check_no_due_access_status():
        messages.error(request, "No Due process is currently locked. Please contact your Faculty.")
        return redirect("student_dashboard")

    if request.method == "POST" and request.session.get("role") == "STUDENT":
        office = request.POST.get("office")
        student_id = ObjectId(request.session["student_id"])

        result = no_due_col.update_one(
            {
                "student_id": student_id,
                "office": office,
                "status": "REJECTED"   # 🔥 IMPORTANT FILTER
            },
            {
                "$set": {
                    "status": "NOT_SENT",
                    "created_at": datetime.now(),
                    "updated_at": datetime.now()
                },
                "$unset": {
                    "reject_reason": ""
                }
            }
        )

        print("Retry matched:", result.matched_count)

    return redirect("student_dashboard")






@institution_login_required
def faculty_dashboard(request):
    if request.session.get("role") != "FACULTY":
        return redirect("index")
    reset_expired_no_dues(no_due_col)
    
    add_error = request.session.pop("add_error", None)
    add_success = request.session.pop("add_success", None)

    branch = request.GET.get("branch")
    year = request.GET.get("year")

    students = []
    count = 0

    if branch and year:
        cursor = students_col.find({
            "branch": branch,
            "year": int(year)
        })

        for s in cursor:
            student_id = s["_id"]

            # 🔥 FETCH NO DUES FOR THIS STUDENT
            dues_cursor = no_due_col.find({"student_id": student_id})

            no_dues = {
                "LIBRARY": "NOT_SENT",
                "HOSTEL": "NOT_SENT",
                "COLLEGE": "NOT_SENT",
                "DEPARTMENT": "NOT_SENT"
            }

            for d in dues_cursor:
                no_dues[d["office"]] = d["status"]

            s_type = s.get("student_type", "Hosteller")

            students.append({
                "id": str(student_id),
                "roll_no": s["roll_no"],
                "reg_no": s["reg_no"],
                "name": s["name"],
                "semester": s["semester"],
                "dob": s["dob"],
                "phone": s["phone"],
                "branch": s["branch"],
                "year": s["year"],
                "student_type": s_type,
                "no_dues": no_dues
            })

        count = len(students)

    return render(request, "faculty_dashboard.html", {
        "students": students,
        "count": count,
        "branch": branch,
        "year": year,
        "add_error": add_error,
        "add_success": add_success
    })


@institution_login_required
def add_student(request):
    if request.session.get("role") != "FACULTY":
        return redirect("index")

    if request.method == "POST":
        roll_no = request.POST["roll_no"].strip()
        reg_no = request.POST["reg_no"].strip()
        name = request.POST["name"].strip()
        dob = request.POST["dob"]
        semester = request.POST["semester"]
        phone = request.POST["phone"].strip()
        branch = request.POST["branch"]
        year = request.POST["year"]

        # ================= FORMAT FIXES =================

        # 🔥 NAME → FULL CAPS
        name = name.upper()

        # 🔥 ROLL NO → department code CAPS (23cs533 → 23CS533)
        roll_no = re.sub(r'([a-zA-Z]+)', lambda m: m.group(1).upper(), roll_no)

        # ================= VALIDATIONS =================

        # Register Number → exactly 12 digits
        if not reg_no.isdigit() or len(reg_no) != 12:
            request.session["add_error"] = "Register Number must be exactly 12 digits"
            return redirect(f"/faculty/?branch={branch}&year={year}")

        # Phone Number → exactly 10 digits
        if not phone.isdigit() or len(phone) != 10:
            request.session["add_error"] = "Phone Number must be exactly 10 digits"
            return redirect(f"/faculty/?branch={branch}&year={year}")

        # DOB → valid date
        try:
            datetime.strptime(dob, "%Y-%m-%d")
        except ValueError:
            request.session["add_error"] = "Invalid Date of Birth format"
            return redirect(f"/faculty/?branch={branch}&year={year}")

        # Semester → 1 to 8
        if not semester.isdigit() or not (1 <= int(semester) <= 8):
            request.session["add_error"] = "Semester must be between 1 and 8"
            return redirect(f"/faculty/?branch={branch}&year={year}")

        # Duplicate check (Roll No / Reg No)
        existing_student = students_col.find_one({
            "$or": [
                {"roll_no": roll_no},
                {"reg_no": reg_no}
            ]
        })

        if existing_student:
            request.session["add_error"] = (
                "Student with this Roll No or Register No already exists"
            )
            return redirect(f"/faculty/?branch={branch}&year={year}")

        # ================= INSERT =================
        student_type = request.POST.get("student_type", "Hosteller")
        if student_type not in ("Hosteller", "Day Scholar"):
            student_type = "Hosteller"

        students_col.insert_one({
            "roll_no": roll_no,
            "reg_no": reg_no,
            "name": name,
            "dob": dob,
            "phone": phone,
            "branch": branch,
            "year": int(year),
            "semester": int(semester),
            "student_type": student_type
        })

        request.session["add_success"] = "Student added successfully"

    return redirect(f"/faculty/?branch={branch}&year={year}")




@institution_login_required
def delete_students(request):
    if request.session.get("role") != "FACULTY":
        return redirect("index")

    if request.method == "POST":
        ids = request.POST.getlist("student_ids")

        valid_object_ids = []
        for i in ids:
            try:
                valid_object_ids.append(ObjectId(i))
            except (InvalidId, TypeError):
                pass   # ignore empty / invalid ids

        if valid_object_ids:
            students_col.delete_many({
                "_id": {"$in": valid_object_ids}
            })

    branch = request.POST.get("branch")
    year = request.POST.get("year")

    return redirect(f"/faculty/?branch={branch}&year={year}")


@institution_login_required
def edit_student(request):
    if request.session.get("role") != "FACULTY":
        return redirect("index")

    if request.method == "POST":
        student_id = request.POST.get("student_id")

        student_type = request.POST.get("student_type", "Hosteller")
        if student_type not in ("Hosteller", "Day Scholar"):
            student_type = "Hosteller"

        try:
            students_col.update_one(
                {"_id": ObjectId(student_id)},
                {"$set": {
                    "roll_no": request.POST["roll_no"],
                    "reg_no": request.POST["reg_no"],
                    "name": request.POST["name"],
                    "dob": request.POST["dob"],
                    "year": int(request.POST["year"]),
                    "phone": request.POST["phone"],
                    "semester": int(request.POST["semester"]),
                    "student_type": student_type
                }}
            )
        except InvalidId:
            pass

    branch = request.POST.get("branch")
    year = request.POST.get("year")
    return redirect(f"/faculty/?branch={branch}&year={year}")


@institution_login_required
def faculty_promotion_page(request):
    if request.session.get("role") != "FACULTY":
        return redirect("index")

    # Handle password verification
    if request.method == "POST":
        password = request.POST.get("promotion_password", "").strip()
        if password == "gces8301":
            request.session["promotion_unlocked"] = True
            return redirect("faculty_promotion")
        else:
            return render(request, "faculty_promotion_login.html", {"error": True})

    # Render login page if session is locked
    if not request.session.get("promotion_unlocked"):
        return render(request, "faculty_promotion_login.html")

    # Sync toggle expiration on load
    check_no_due_access_status()

    sem8_count = students_col.count_documents({"semester": 8})

    sem_counts = {}
    for sem in range(1, 9):
        sem_counts[sem] = students_col.count_documents({"semester": sem})

    global_sem8_count = sem8_count

    settings_doc = portal_settings.find_one({"_id": "global_config"})
    no_due_access_enabled = False
    enabled_at_str = ""
    auto_disable_at_str = ""
    auto_disable_at_iso = ""
    duration_days = 75

    if settings_doc:
        no_due_access_enabled = settings_doc.get("no_due_access_enabled", False)
        enabled_at = settings_doc.get("enabled_at")
        auto_disable_at = settings_doc.get("auto_disable_at")
        duration_days = settings_doc.get("duration_days", 75)
        
        if enabled_at:
            enabled_at_str = enabled_at.strftime("%d-%m-%Y %I:%M %p")
        if auto_disable_at:
            auto_disable_at_str = auto_disable_at.strftime("%d-%m-%Y %I:%M %p")
            auto_disable_at_iso = auto_disable_at.isoformat()

    return render(request, "faculty_promotion.html", {
        "sem8_count": sem8_count,
        "global_sem8_count": global_sem8_count,
        "sem_counts": sem_counts,
        "no_due_access_enabled": no_due_access_enabled,
        "enabled_at_str": enabled_at_str,
        "auto_disable_at_str": auto_disable_at_str,
        "auto_disable_at_iso": auto_disable_at_iso,
        "duration_days": duration_days,
    })


@institution_login_required
def toggle_no_due_access(request):
    if request.session.get("role") != "FACULTY":
        return redirect("index")

    if not request.session.get("promotion_unlocked"):
        messages.error(request, "Access denied. Please verify password first.")
        return redirect("faculty_promotion")

    if request.method == "POST":
        current_status = request.POST.get("current_status", "true") == "true"
        new_status = not current_status

        if new_status:
            duration_type = request.POST.get("duration_type", "").strip()
            custom_datetime_str = request.POST.get("custom_datetime", "").strip()

            now = datetime.now()
            days = 0
            if duration_type == "recommended":
                auto_disable_at = now + timedelta(days=75)
                days = 75
            elif duration_type == "custom":
                if not custom_datetime_str:
                    messages.error(request, "Auto Disable Date and Time is required to enable No Due Access.")
                    return redirect("faculty_promotion")
                try:
                    # Parse local datetime-local format: YYYY-MM-DDTHH:MM
                    auto_disable_at = datetime.fromisoformat(custom_datetime_str)
                except ValueError:
                    messages.error(request, "Invalid Date and Time format.")
                    return redirect("faculty_promotion")

                if auto_disable_at <= now:
                    messages.error(request, "Auto Disable Date and Time must be in the future.")
                    return redirect("faculty_promotion")

                delta = auto_disable_at - now
                days = delta.days if delta.days > 0 else 1
            else:
                messages.error(request, "Auto Disable Duration is required to enable No Due Access.")
                return redirect("faculty_promotion")

            portal_settings.update_one(
                {"_id": "global_config"},
                {"$set": {
                    "no_due_access_enabled": True,
                    "enabled_at": now,
                    "auto_disable_at": auto_disable_at,
                    "duration_days": days
                }},
                upsert=True
            )
            
            # Format display string for success message
            exp_str = auto_disable_at.strftime("%d-%m-%Y %I:%M %p")
            messages.success(request, f"No Due Access has been successfully Enabled globally until {exp_str}.")
        else:
            portal_settings.update_one(
                {"_id": "global_config"},
                {"$set": {"no_due_access_enabled": False}},
                upsert=True
            )
            messages.success(request, "No Due Access has been successfully Disabled globally.")

    return redirect("faculty_promotion")


@institution_login_required
def promote_students(request):
    if request.session.get("role") != "FACULTY":
        return redirect("index")

    if not request.session.get("promotion_unlocked"):
        messages.error(request, "Access denied. Please verify password first.")
        return redirect("faculty_promotion")

    if request.method == "POST":
        # Ensure config document exists
        portal_settings.update_one(
            {"_id": "global_config"},
            {"$setOnInsert": {"promotion_in_progress": False}},
            upsert=True
        )

        # Acquire lock to prevent duplicate concurrent promotions
        lock_acquired = portal_settings.find_one_and_update(
            {"_id": "global_config", "promotion_in_progress": {"$ne": True}},
            {"$set": {"promotion_in_progress": True}}
        )
        if not lock_acquired:
            messages.error(request, "A promotion is already in progress. Please wait.")
            return redirect("faculty_promotion")

        try:
            # Get counts of all semesters in the system
            counts = {}
            for sem in range(1, 9):
                counts[sem] = students_col.count_documents({"semester": sem})

            from_sem_raw = request.POST.get("from_sem")
            from_sem = None
            if from_sem_raw and from_sem_raw.strip():
                try:
                    from_sem = int(from_sem_raw)
                except (ValueError, TypeError):
                    from_sem = None

            # Determine if Semester-Wise or Full Promotion
            if from_sem is not None:
                # ================= SEMESTER-WISE PROMOTION =================
                if from_sem < 1 or from_sem > 7:
                    messages.error(request, "Invalid semester selected.")
                    return redirect("faculty_promotion")

                # If even semester promoting to odd (Year transition), check Sem 8 students
                if from_sem in (2, 4, 6):
                    if counts[8] > 0:
                        messages.error(request, "Please remove Semester 8 students before promoting students to the next academic year.")
                        return redirect("faculty_promotion")

                # Target semester validation
                target_sem = from_sem + 1
                if counts[target_sem] > 0:
                    messages.error(request, f"Promotion blocked. Semester {target_sem} already contains students. Promoting Semester {from_sem} students would merge two different batches.")
                    return redirect("faculty_promotion")

                students = list(students_col.find({"semester": from_sem}))
                if not students:
                    messages.warning(request, f"No students in Semester {from_sem} found for promotion.")
                    return redirect("faculty_promotion")
            else:
                # ================= FULL PROMOTION =================
                # Check if Semester 8 students exist
                if counts[8] > 0:
                    messages.error(request, "Please remove Semester 8 students before promoting students to the next academic year.")
                    return redirect("faculty_promotion")

                # Target semester validation via descending order simulation
                sim_counts = dict(counts)
                for s in range(7, 0, -1):
                    student_count = sim_counts[s]
                    if student_count > 0:
                        t_sem = s + 1
                        t_count = sim_counts[t_sem]
                        if t_count > 0:
                            messages.error(request, f"Promotion blocked. Semester {t_sem} already contains students. Promoting Semester {s} students would merge two different batches.")
                            return redirect("faculty_promotion")
                        else:
                            # Move in simulation
                            sim_counts[t_sem] = student_count
                            sim_counts[s] = 0

                # Query all students in Semesters 1 to 7
                students = list(students_col.find({"semester": {"$in": [1, 2, 3, 4, 5, 6, 7]}}))
                if not students:
                    messages.warning(request, "No students in Semesters 1 to 7 found for promotion.")
                    return redirect("faculty_promotion")

            progression = {
                1: (2, 0),
                2: (3, 1),
                3: (4, 0),
                4: (5, 1),
                5: (6, 0),
                6: (7, 1),
                7: (8, 0),
            }

            promoted_count = 0
            from .mongo import promotion_logs
            now = datetime.now()

            # Process updates student-by-student to maintain exact logs and individual status updates.
            for student in students:
                student_id = student["_id"]
                current_sem = student.get("semester")
                current_year = student.get("year", 1)

                if current_sem not in progression:
                    continue

                next_sem, year_change = progression[current_sem]
                new_year = current_year + year_change

                # Update student document
                students_col.update_one(
                    {"_id": student_id},
                    {"$set": {
                        "semester": next_sem,
                        "year": new_year
                    }}
                )

                student_type = student.get("student_type", "Hosteller")
                offices = ["LIBRARY", "COLLEGE", "DEPARTMENT"] if student_type == "Day Scholar" else ["LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"]
                approved_count = no_due_col.count_documents({
                    "student_id": student_id,
                    "office": {"$in": offices},
                    "status": "APPROVED"
                })
                no_due_cleared = (approved_count == len(offices))

                # Reset dues
                no_due_col.update_many(
                    {"student_id": student_id},
                    {"$set": {
                        "status": "NOT_SENT",
                        "receipt_url": None,
                        "cloudinary_public_id": None,
                        "reject_reason": None,
                        "last_payment_id": None,
                        "updated_at": now,
                    }}
                )

                # Insert log
                promotion_logs.insert_one({
                    "student_id": student_id,
                    "previous_semester": current_sem,
                    "previous_year": current_year,
                    "new_semester": next_sem,
                    "new_year": new_year,
                    "student_type": student_type,
                    "completion_time": now,
                    "promotion_time": now,
                    "no_due_cleared": no_due_cleared,
                })
                promoted_count += 1

            messages.success(request, f"Successfully promoted {promoted_count} students to the next semester!")
        finally:
            portal_settings.update_one(
                {"_id": "global_config"},
                {"$set": {"promotion_in_progress": False}}
            )

    return redirect("faculty_promotion")


@institution_login_required
def remove_sem8_students(request):
    if request.session.get("role") != "FACULTY":
        return redirect("index")

    if not request.session.get("promotion_unlocked"):
        messages.error(request, "Access denied. Please verify password first.")
        return redirect("faculty_promotion")

    if request.method == "POST":
        query = {"semester": 8}
        students = list(students_col.find(query))
        if not students:
            messages.warning(request, "No Semester 8 students found to remove.")
            return redirect("faculty_promotion")

        student_ids = [s["_id"] for s in students]

        students_col.delete_many({"_id": {"$in": student_ids}})
        no_due_col.delete_many({"student_id": {"$in": student_ids}})

        from .mongo import promotion_logs
        promotion_logs.delete_many({"student_id": {"$in": student_ids}})

        messages.success(request, f"Successfully removed {len(students)} Semester 8 students from the system.")

    return redirect("faculty_promotion")



@institution_login_required
def download_student_template(request):
    if request.session.get("role") != "FACULTY":
        return redirect("index")

    from openpyxl.styles import numbers as xl_numbers
    from openpyxl.cell.cell import TYPE_STRING

    wb = Workbook()
    ws = wb.active
    ws.title = "Student Template"

    # ── Column widths ─────────────────────────────────────
    ws.column_dimensions["A"].width = 15   # roll_no
    ws.column_dimensions["B"].width = 20   # reg_no
    ws.column_dimensions["C"].width = 25   # name
    ws.column_dimensions["D"].width = 15   # dob
    ws.column_dimensions["E"].width = 15   # phone
    ws.column_dimensions["F"].width = 12   # semester

    # ── Apply cell formats for rows 1-500 BEFORE writing any data ────────────
    # "@" = Text  →  prevents 830123104032 becoming 8.31E+11
    #              →  prevents 9876543210  becoming a float
    for row in range(1, 501):
        ws[f"A{row}"].number_format = "@"           # roll_no  → Text
        ws[f"B{row}"].number_format = "@"           # reg_no   → Text
        ws[f"E{row}"].number_format = "@"           # phone    → Text
        ws[f"D{row}"].number_format = "yyyy-mm-dd"  # dob      → Date

    # ── Header row (row 1) ────────────────────────────────
    for col, header in enumerate(["roll_no", "reg_no", "name", "dob", "phone", "semester"], start=1):
        ws.cell(row=1, column=col).value = header

    # ── Sample row (row 2) — all text columns stored as strings ──────────────
    def write_text(row, col, value):
        """Write value as explicit Text string so Excel stores it as-is."""
        cell = ws.cell(row=row, column=col)
        cell.value = str(value)
        cell.data_type = TYPE_STRING   # force Excel to treat as Text

    write_text(2, 1, "21CS001")        # roll_no
    write_text(2, 2, "202110000001")   # reg_no  (12 digits, no scientific notation)
    write_text(2, 3, "SAMPLE NAME")    # name
    ws.cell(row=2, column=4).value = "2003-01-01"   # dob  (date string)
    write_text(2, 5, "9876543210")     # phone   (10 digits, no conversion)
    ws.cell(row=2, column=6).value = 5              # semester (number is fine)

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="student_template.xlsx"'

    wb.save(response)
    return response


@institution_login_required
def import_students_excel(request):
    print("Import Students Excel called")

    # 🔐 ROLE CHECK
    if request.session.get("role") != "FACULTY":
        return redirect("index")

    if request.method != "POST":
        return redirect("faculty_dashboard")

    excel = request.FILES.get("excel")
    branch = request.POST.get("branch")
    year = request.POST.get("year")

    if not excel:
        messages.error(request, "No Excel file uploaded ❌")
        return redirect(f"/faculty/?branch={branch}&year={year}")

    inserted = 0
    skipped = 0
    skipped_students = []   # name + reason

    try:
        wb = load_workbook(excel)
        ws = wb.active

        for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):

            # Expected columns
            if len(row) < 6:
                skipped += 1
                skipped_students.append(f"Row {idx} (Invalid column format)")
                continue

            roll_no, reg_no, name, dob, phone, semester = row

            # 🛑 Completely empty row → IGNORE
            if not any([roll_no, reg_no, name, dob, phone, semester]):
                continue

            # 🛑 Mandatory field missing
            if not roll_no or not reg_no or not name:
                skipped += 1
                skipped_students.append(
                    f"{name or 'Unknown'} (Row {idx} – Missing RegNo / RollNo)"
                )
                continue

            # 🔧 CLEAN VALUES
            roll_no = str(roll_no).strip().upper()
            reg_no = str(reg_no).strip()
            name = str(name).strip().upper()
            phone = str(phone).strip()

            # 🔥 DOB FIX → remove 00:00:00
            if isinstance(dob, (datetime, date)):
                dob = dob.strftime("%Y-%m-%d")
            else:
                dob = str(dob).strip()

            # 🛑 Duplicate check
            exists = students_col.find_one({
                "$or": [
                    {"roll_no": roll_no},
                    {"reg_no": reg_no}
                ]
            })

            if exists:
                skipped += 1
                skipped_students.append(
                    f"{name} (Duplicate RegNo / RollNo)"
                )
                continue

            # ✅ INSERT STUDENT
            students_col.insert_one({
                "roll_no": roll_no,
                "reg_no": reg_no,
                "name": name,
                "dob": dob,                 # ✅ yyyy-mm-dd only
                "phone": phone,
                "branch": branch,
                "year": int(year),
                "semester": int(semester),
                "student_type": "Hosteller"  # ✅ default for bulk imports
            })

            inserted += 1

        # ✅ SUCCESS MESSAGE
        messages.success(
            request,
            f"Excel Import Completed ✅ Added: {inserted}, Skipped: {skipped}"
        )

        # ⚠️ SKIPPED DETAILS
        if skipped_students:
            messages.warning(
                request,
                "Skipped Students:\n" + "\n".join(skipped_students)
            )

    except Exception as e:
        messages.error(request, f"Excel import failed ❌ {str(e)}")

    return redirect(f"/faculty/?branch={branch}&year={year}")

# ================= LOGOUT =================
def logout_view(request):
    request.session.flush()
    return redirect("index")


# ================= OFFICE STUDENT STATUS API =================
from django.http import JsonResponse
import math

@institution_login_required
def office_student_status_api(request):
    role = request.session.get("role")
    if role not in ("LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"):
        return JsonResponse({"error": "Unauthorized"}, status=403)

    try:
        year = int(request.GET.get("year", 0))
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid Year"}, status=400)

    if not year:
        return JsonResponse({"error": "Year is required"}, status=400)

    search = request.GET.get("search", "").strip()
    status_filter = request.GET.get("status", "All").strip()
    branch_filter = request.GET.get("branch", "").strip()
    
    try:
        page = int(request.GET.get("page", 1))
        if page < 1:
            page = 1
    except (ValueError, TypeError):
        page = 1

    limit = 10  # number of students per page

    # Build match query for students
    match_query = {
        "year": year
    }

    if role == "DEPARTMENT":
        match_query["branch"] = request.session.get("department")
    elif branch_filter:
        match_query["branch"] = branch_filter

    if search:
        match_query["$or"] = [
            {"reg_no": {"$regex": search, "$options": "i"}},
            {"name": {"$regex": search, "$options": "i"}}
        ]

    # Build aggregation pipeline
    pipeline = [
        {"$match": match_query},
        {
            "$lookup": {
                "from": "no_due_requests",
                "let": {"student_id": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$student_id", "$$student_id"]},
                                    {"$eq": ["$office", role]}
                                ]
                            }
                        }
                    }
                ],
                "as": "no_due_record"
            }
        },
        {
            "$addFields": {
                "no_due": {"$arrayElemAt": ["$no_due_record", 0]}
            }
        },
        {
            "$addFields": {
                "office_status": {
                    "$cond": {
                        "if": {"$and": [{"$eq": [role, "HOSTEL"]}, {"$eq": ["$student_type", "Day Scholar"]}]},
                        "then": "Completed",
                        "else": {
                            "$cond": {
                                "if": {"$eq": ["$no_due.status", "APPROVED"]},
                                "then": "Completed",
                                "else": {
                                    "$cond": {
                                        "if": {"$eq": ["$no_due.status", "PENDING"]},
                                        "then": "Pending",
                                        "else": "Incomplete"
                                    }
                                }
                            }
                        }
                    }
                },
                "completed_time": {
                    "$cond": {
                        "if": {"$and": [{"$eq": [role, "HOSTEL"]}, {"$eq": ["$student_type", "Day Scholar"]}]},
                        "then": "-",
                        "else": {
                            "$cond": {
                                "if": {"$eq": ["$no_due.status", "APPROVED"]},
                                "then": {"$ifNull": ["$no_due.updated_at", "$no_due.created_at"]},
                                "else": "-"
                            }
                        }
                    }
                }
            }
        }
    ]

    # Apply status filter
    if status_filter and status_filter != "All":
        pipeline.append({"$match": {"office_status": status_filter}})

    # Pagination facet
    pipeline.append({
        "$facet": {
            "metadata": [{"$count": "total"}],
            "data": [
                {"$skip": (page - 1) * limit},
                {"$limit": limit}
            ]
        }
    })

    try:
        results = list(students_col.aggregate(pipeline))
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

    total_count = 0
    students_list = []

    if results:
        metadata = results[0].get("metadata", [])
        if metadata:
            total_count = metadata[0]["total"]
        data = results[0].get("data", [])
        
        for s in data:
            ct = s.get("completed_time")
            # Format time if it is a datetime object
            if isinstance(ct, datetime):
                completed_time_str = ct.strftime("%d-%m-%Y %I:%M %p")
            else:
                completed_time_str = "-"
                
            students_list.append({
                "reg_no": s.get("reg_no"),
                "name": s.get("name"),
                "branch": s.get("branch"),
                "semester": s.get("semester"),
                "status": s.get("office_status"),
                "completed_time": completed_time_str
            })

    total_pages = math.ceil(total_count / limit)

    return JsonResponse({
        "students": students_list,
        "current_page": page,
        "total_pages": total_pages,
        "total_count": total_count
    })


# ═══════════════════════════════════════════
#  REPORT GENERATION & EXPORT VIEWS
# ═══════════════════════════════════════════

def _get_report_students_pipeline(role, report_type, year_str, branch_val, dept):
    match_query = {}

    if role == "DEPARTMENT":
        match_query["branch"] = dept
        branch_val = dept

    if report_type == "year":
        if not year_str:
            return None, "Year is required for Year Wise Report"
        try:
            match_query["year"] = int(year_str)
        except (ValueError, TypeError):
            return None, "Invalid Year"

    elif report_type == "branch":
        if role != "DEPARTMENT":
            if not branch_val:
                return None, "Branch is required for Branch Wise Report"
            match_query["branch"] = branch_val

    elif report_type == "year_branch":
        if not year_str:
            return None, "Year is required for Year + Branch Wise Report"
        try:
            match_query["year"] = int(year_str)
        except (ValueError, TypeError):
            return None, "Invalid Year"
        if role != "DEPARTMENT":
            if not branch_val:
                return None, "Branch is required for Year + Branch Wise Report"
            match_query["branch"] = branch_val

    pipeline = [
        {"$match": match_query},
        {
            "$lookup": {
                "from": "no_due_requests",
                "let": {"student_id": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$student_id", "$$student_id"]},
                                    {"$eq": ["$office", role]}
                                ]
                            }
                        }
                    }
                ],
                "as": "no_due_record"
            }
        },
        {
            "$addFields": {
                "no_due": {"$arrayElemAt": ["$no_due_record", 0]}
            }
        },
        {
            "$addFields": {
                "office_status": {
                    "$cond": {
                        "if": {"$and": [{"$eq": [role, "HOSTEL"]}, {"$eq": ["$student_type", "Day Scholar"]}]},
                        "then": "Completed",
                        "else": {
                            "$cond": {
                                "if": {"$eq": ["$no_due.status", "APPROVED"]},
                                "then": "Completed",
                                "else": {
                                    "$cond": {
                                        "if": {"$eq": ["$no_due.status", "PENDING"]},
                                        "then": "Pending",
                                        "else": "Incomplete"
                                    }
                                }
                            }
                        }
                    }
                },
                "completed_time": {
                    "$cond": {
                        "if": {"$and": [{"$eq": [role, "HOSTEL"]}, {"$eq": ["$student_type", "Day Scholar"]}]},
                        "then": "-",
                        "else": {
                            "$cond": {
                                "if": {"$eq": ["$no_due.status", "APPROVED"]},
                                "then": {"$ifNull": ["$no_due.updated_at", "$no_due.created_at"]},
                                "else": "-"
                            }
                        }
                    }
                }
            }
        },
        {"$sort": {"reg_no": 1}}
    ]
    return pipeline, None


@institution_login_required
def office_report_preview_api(request):
    role = request.session.get("role")
    if role not in ("LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"):
        return JsonResponse({"error": "Unauthorized"}, status=403)

    report_type = request.GET.get("report_type", "").strip()
    if report_type not in ("year", "branch", "year_branch"):
        return JsonResponse({"error": "Invalid Report Type"}, status=400)

    year_str = request.GET.get("year", "").strip()
    branch_val = request.GET.get("branch", "").strip()
    dept = request.session.get("department")

    pipeline, err = _get_report_students_pipeline(role, report_type, year_str, branch_val, dept)
    if err:
        return JsonResponse({"error": err}, status=400)

    try:
        page = int(request.GET.get("page", 1))
        if page < 1:
            page = 1
    except (ValueError, TypeError):
        page = 1

    limit = 10
    pagination_pipeline = list(pipeline)
    pagination_pipeline.append({
        "$facet": {
            "metadata": [{"$count": "total"}],
            "data": [
                {"$skip": (page - 1) * limit},
                {"$limit": limit}
            ]
        }
    })

    try:
        results = list(students_col.aggregate(pagination_pipeline))
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

    total_count = 0
    students_list = []

    if results:
        metadata = results[0].get("metadata", [])
        if metadata:
            total_count = metadata[0]["total"]
        data = results[0].get("data", [])

        for s in data:
            ct = s.get("completed_time")
            if isinstance(ct, datetime):
                completed_time_str = ct.strftime("%d-%m-%Y %I:%M %p")
            else:
                completed_time_str = "-"

            students_list.append({
                "reg_no": s.get("reg_no"),
                "roll_no": s.get("roll_no", ""),
                "name": s.get("name"),
                "branch": s.get("branch"),
                "year": s.get("year"),
                "semester": s.get("semester"),
                "student_type": s.get("student_type", "Hosteller"),
                "status": s.get("office_status"),
                "completed_time": completed_time_str
            })

    total_pages = math.ceil(total_count / limit)

    return JsonResponse({
        "students": students_list,
        "current_page": page,
        "total_pages": total_pages,
        "total_count": total_count
    })


class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            super().showPage()
        super().save()

    def draw_page_number(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#4B5563"))
        
        # Line above footer
        width, height = A4
        self.setStrokeColor(colors.HexColor("#D1D5DB"))
        self.setLineWidth(0.5)
        self.line(36, 45, width - 36, 45)
        
        # Footer content
        self.drawString(36, 32, "Generated by GCES No Due Clearance Portal")
        page_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(width - 36, 32, page_text)
        self.restoreState()


@institution_login_required
def office_report_pdf_view(request):
    role = request.session.get("role")
    if role not in ("LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"):
        return HttpResponse("Unauthorized", status=403)

    report_type = request.GET.get("report_type", "").strip()
    if report_type not in ("year", "branch", "year_branch"):
        return HttpResponse("Invalid Report Type", status=400)

    year_str = request.GET.get("year", "").strip()
    branch_val = request.GET.get("branch", "").strip()
    dept = request.session.get("department")

    pipeline, err = _get_report_students_pipeline(role, report_type, year_str, branch_val, dept)
    if err:
        return HttpResponse(err, status=400)

    try:
        students = list(students_col.aggregate(pipeline))
    except Exception as e:
        return HttpResponse(f"Database error: {str(e)}", status=500)

    # Prepare response as PDF attachment
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="Clearance_Report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'

    # Set download token cookie if provided
    download_token = request.GET.get("download_token")
    if download_token:
        response.set_cookie("fileDownloadToken", download_token, max_age=60)

    # Document template setup (A4 standard: 595.27 x 841.89 points)
    doc = SimpleDocTemplate(
        response,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=55
    )

    story = []
    styles = getSampleStyleSheet()

    primary_color = colors.HexColor("#d52b1e")
    dark_gray = colors.HexColor("#1F2937")

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=primary_color,
        alignment=1
    )

    subtitle_style = ParagraphStyle(
        'DocSubTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=13,
        leading=16,
        textColor=dark_gray,
        alignment=1
    )

    info_label_style = ParagraphStyle(
        'InfoLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#374151")
    )

    info_val_style = ParagraphStyle(
        'InfoVal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#4B5563")
    )

    # Title Block
    story.append(Paragraph("GOVERNMENT COLLEGE OF ENGINEERING – SRIRANGAM", title_style))
    story.append(Spacer(1, 4))

    office_display = role.title() + " Office" if role != "DEPARTMENT" else f"{dept} Department Office"
    story.append(Paragraph(f"{office_display} – Clearance Status Report", subtitle_style))
    story.append(Spacer(1, 10))

    # Red divider line
    divider = Table([[""]], colWidths=[523], rowHeights=[2])
    divider.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), primary_color),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(divider)
    story.append(Spacer(1, 12))

    # Meta-info block
    now_str = datetime.now().strftime("%d-%m-%Y %I:%M %p")

    selected_year = f"Year {year_str}" if year_str else "All Years"
    selected_branch = branch_val if branch_val else "All Branches"

    report_title_display = "Clearance Report"
    if report_type == "year":
        report_title_display = "Year-Wise Clearance Report"
    elif report_type == "branch":
        report_title_display = "Branch-Wise Clearance Report"
    elif report_type == "year_branch":
        report_title_display = "Year & Branch Clearance Report"

    info_data = [
        [
            Paragraph("Report Type:", info_label_style), Paragraph(report_title_display, info_val_style),
            Paragraph("Generated on:", info_label_style), Paragraph(now_str, info_val_style)
        ],
        [
            Paragraph("Selected Year:", info_label_style), Paragraph(selected_year, info_val_style),
            Paragraph("Selected Branch:", info_label_style), Paragraph(selected_branch, info_val_style)
        ],
        [
            Paragraph("Total Students:", info_label_style), Paragraph(str(len(students)), info_val_style),
            "", ""
        ]
    ]

    info_table = Table(info_data, colWidths=[100, 160, 100, 163])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 0),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 15))

    # Student Data Table
    th_style = ParagraphStyle(
        'TableHead',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.white
    )

    td_style = ParagraphStyle(
        'TableBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=dark_gray
    )

    td_completed_style = ParagraphStyle('TableBodyCompleted', parent=td_style, textColor=colors.HexColor("#16A34A"))
    td_pending_style = ParagraphStyle('TableBodyPending', parent=td_style, textColor=colors.HexColor("#D97706"))
    td_incomplete_style = ParagraphStyle('TableBodyIncomplete', parent=td_style, textColor=colors.HexColor("#DC2626"))

    table_data = [[
        Paragraph("Register No", th_style),
        Paragraph("Roll No", th_style),
        Paragraph("Student Name", th_style),
        Paragraph("Branch", th_style),
        Paragraph("Year", th_style),
        Paragraph("Sem", th_style),
        Paragraph("Type", th_style),
        Paragraph("Status", th_style),
        Paragraph("Approval Date & Time", th_style)
    ]]

    for s in students:
        ct = s.get("completed_time")
        if isinstance(ct, datetime):
            completed_time_str = ct.strftime("%d-%m-%Y %I:%M %p")
        else:
            completed_time_str = "-"

        status = s.get("office_status")
        if status == "Completed":
            status_p = Paragraph("Completed", td_completed_style)
        elif status == "Pending":
            status_p = Paragraph("Pending", td_pending_style)
        else:
            status_p = Paragraph("Incomplete", td_incomplete_style)

        table_data.append([
            Paragraph(str(s.get("reg_no", "")), td_style),
            Paragraph(str(s.get("roll_no", "")), td_style),
            Paragraph(str(s.get("name", "")), td_style),
            Paragraph(str(s.get("branch", "")), td_style),
            Paragraph(str(s.get("year", "")), td_style),
            Paragraph(str(s.get("semester", "")), td_style),
            Paragraph(str(s.get("student_type", "Hosteller")), td_style),
            status_p,
            Paragraph(completed_time_str, td_style)
        ])

    student_table = Table(table_data, colWidths=[75, 55, 95, 50, 25, 25, 60, 60, 78], repeatRows=1)
    
    t_style = TableStyle([
        ('BACKGROUND', (0,0), (-1,0), primary_color),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#E5E7EB")),
    ])

    for i in range(1, len(table_data)):
        bg_color = colors.HexColor("#F9FAFB") if i % 2 == 0 else colors.white
        t_style.add('BACKGROUND', (0, i), (-1, i), bg_color)

    student_table.setStyle(t_style)
    story.append(student_table)

    doc.build(story, canvasmaker=NumberedCanvas)
    return response

