from django.urls import path
from .views import index, library_dashboard, logout_view, faculty_dashboard, add_student , delete_students , edit_student, student_login, student_dashboard, send_hostel_request, college_dashboard, department_dashboard, hostel_dashboard, bulk_approve, reject_request, send_no_due_request, retry_request, update_student_profile , no_due_certificate , download_student_template, import_students_excel, office_student_status_api, office_report_preview_api, office_report_pdf_view, faculty_promotion_page, promote_students, remove_sem8_students, toggle_no_due_access
 
from django.conf.urls.static import static

urlpatterns = [
    path("", index, name="index"), # LOGIN PAGE
    
     # student
        # Institution dashboards 
    path("college/",college_dashboard, name="college_dashboard"),
    path("department/",department_dashboard, name="department_dashboard"),
    path("hostel/",hostel_dashboard, name="hostel_dashboard"),
    # Actions
    path("approve/",bulk_approve, name="bulk_approve"),
    path("reject/",reject_request, name="reject_request"),

    # Student
    path("student/login/",student_login, name="student_login"),
    path("student/dashboard/",student_dashboard, name="student_dashboard"),
    path("student/hostel/send/",send_hostel_request, name="send_hostel"),
    path("library/", library_dashboard, name="library_dashboard"),
    path("student/send/", send_no_due_request, name="send_no_due"),
    path("student/retry/", retry_request, name="retry_request"),
    path("student/profile/update/", update_student_profile, name="update_student_profile"),
    path("student/no-due/certificate/", no_due_certificate, name="no_due_certificate"),

    path("logout/", logout_view, name="logout"),
    
    path("faculty/", faculty_dashboard, name="faculty_dashboard"),
    path("faculty/add-student/", add_student, name="add_student"),
    path("faculty/delete/", delete_students, name="delete_students"),
    path("faculty/edit/", edit_student, name="edit_student"),
    path("faculty/template/", download_student_template, name="download_student_template"),
    path("faculty/import/", import_students_excel, name="import_students_excel"),
    path("faculty/promotion/", faculty_promotion_page, name="faculty_promotion"),
    path("faculty/promote/", promote_students, name="promote_students"),
    path("faculty/remove-sem8/", remove_sem8_students, name="remove_sem8_students"),
    path("faculty/toggle-no-due-access/", toggle_no_due_access, name="toggle_no_due_access"),
    path("office/student-status/", office_student_status_api, name="student_status_api"),
    path("office/report/preview/", office_report_preview_api, name="report_preview_api"),
    path("office/report/pdf/", office_report_pdf_view, name="report_pdf_view"),
]

 
