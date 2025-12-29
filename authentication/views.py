from django.shortcuts import render, redirect
from django.contrib import messages
from datetime import datetime , timedelta
from .decorators import institution_login_required 
from .institution_users import INSTITUTION_USERS
from .mongo import institution_logs , students_col , no_due_col
from bson.errors import InvalidId
from bson import ObjectId
from django.conf import settings
from .utils import save_receipt , reset_expired_no_dues
import re


# ================= INDEX = INSTITUTION LOGIN =================
def index(request):
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

        # 🔎 CHECK STUDENT
        student = students_col.find_one({
            "reg_no": reg_no,
            "dob": dob
        })

        if not student:
            request.session["student_error"] = "Invalid credentials"
            return redirect("index")

        # ================= LOGIN SUCCESS =================
        request.session["role"] = "STUDENT"          # ✅ REQUIRED
        request.session["student_id"] = str(student["_id"])

        return redirect("student_dashboard")

    return redirect("index")


 

@institution_login_required
def student_dashboard(request):
    if request.session.get("role") != "STUDENT":
        return redirect("index")

    reset_expired_no_dues(no_due_col)

    student_id = ObjectId(request.session["student_id"])
    student = students_col.find_one({"_id": student_id})

    existing = {
        d["office"]: d
        for d in no_due_col.find({"student_id": student_id})
    }

    offices = ["LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"]
    dues = []

    all_approved = True   # 🔥 FLAG

    for office in offices:
        d = existing.get(office, {
            "office": office,
            "status": "NOT_SENT"
        })

        dues.append(d)

        # 🔴 if ANY office not approved → false
        if d.get("status") != "APPROVED":
            all_approved = False

    return render(request, "student_dashboard.html", {
        "student": student,
        "dues": dues,
        "all_approved": all_approved   # ✅ PASS TO TEMPLATE
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
    if len(dues) < 4:
        return redirect("student_dashboard")

    # Convert to simple dict for template
    no_dues_status = {
        "LIBRARY": "Completed",
        "HOSTEL": "Completed",
        "COLLEGE": "Completed",
        "DEPARTMENT": "Completed"
    }

    return render(request, "no_due_certificate.html", {
        "student": student,
        "no_dues": no_dues_status
    })



    
    
@institution_login_required
def send_hostel_request(request):
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
            "receipt_url": receipt_url,              # ✅ cloud URL
            "cloudinary_public_id": cloudinary_public_id,  # ✅ for delete
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
        "count": count,
        "branches": ["CSE","ECE","EEE","CIVIL","MECH","MCT"],
        "branch_summary": branch_summary,
        "year_summary": year_summary
    })

@institution_login_required
def bulk_approve(request):
    ids = request.POST.getlist("request_ids")

    if ids:
        no_due_col.update_many(
            {"_id": {"$in": [ObjectId(i) for i in ids]}},
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
            cloudinary.uploader.destroy(
                public_id,
                resource_type="raw"
            )

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
        "count": count,
        "year_summary": year_summary
    })


@institution_login_required
def send_no_due_request(request):
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
                "no_dues": no_dues   # ✅ IMPORTANT
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
        students_col.insert_one({
            "roll_no": roll_no,
            "reg_no": reg_no,
            "name": name,
            "dob": dob,
            "phone": phone,
            "branch": branch,
            "year": int(year),
            "semester": int(semester)
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

        try:
            students_col.update_one(
                {"_id": ObjectId(student_id)},
                {"$set": {
                    "roll_no": request.POST["roll_no"],
                    "reg_no": request.POST["reg_no"],
                    "name": request.POST["name"],
                    "dob": request.POST["dob"],
                    "phone": request.POST["phone"],
                    "semester": int(request.POST["semester"])
                }}
            )
        except InvalidId:
            pass

    branch = request.POST.get("branch")
    year = request.POST.get("year")
    return redirect(f"/faculty/?branch={branch}&year={year}")




# ================= LOGOUT =================
def logout_view(request):
    request.session.flush()
    return redirect("index")

