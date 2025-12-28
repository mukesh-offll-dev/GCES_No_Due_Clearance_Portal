from django.urls import path
from .views import index, library_dashboard, logout_view, faculty_dashboard, add_student , delete_students , edit_student, student_login, student_dashboard, send_hostel_request, college_dashboard, department_dashboard, hostel_dashboard, bulk_approve, reject_request, send_no_due_request, retry_request, update_student_profile , no_due_certificate
from django.conf import settings
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
    
    
]


if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)