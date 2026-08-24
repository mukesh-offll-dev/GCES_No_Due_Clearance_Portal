import unittest
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from django.conf import settings
from bson import ObjectId


class AuthenticationAndSecurityTests(TestCase):
    """
    Tests covering:
    - Production 500 error prevention (defensive field access, null checks)
    - Logout invalidation and anti-caching headers
    - Browser back-button protection and session expiry
    - Role-based server-side authorization
    """

    def setUp(self):
        self.client = Client()

    # ─────────────────────────────────────────────────────────────
    #  1. Unauthenticated & Protected Routes
    # ─────────────────────────────────────────────────────────────
    def test_unauthenticated_access_redirects_to_login(self):
        """Unauthenticated requests to protected endpoints must redirect to login."""
        protected_urls = [
            reverse("faculty_dashboard"),
            reverse("student_dashboard"),
            reverse("library_dashboard"),
            reverse("hostel_dashboard"),
            reverse("college_dashboard"),
            reverse("department_dashboard"),
            reverse("no_due_certificate"),
            reverse("faculty_promotion"),
        ]
        for url in protected_urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, f"Expected 302 for {url}")
            self.assertEqual(response.url, reverse("index"))
            # Anti-cache headers must be present even on redirect
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_anti_cache_headers_on_all_responses(self):
        """Protected pages and logout must return strict anti-cache headers."""
        response = self.client.get(reverse("logout"))
        self.assertEqual(response.status_code, 302)
        cache_control = response.headers.get("Cache-Control", "")
        self.assertIn("no-store", cache_control)
        self.assertIn("no-cache", cache_control)
        self.assertIn("must-revalidate", cache_control)
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        self.assertEqual(response.headers.get("Expires"), "0")

    # ─────────────────────────────────────────────────────────────
    #  2. Login & Logout Lifecycle
    # ─────────────────────────────────────────────────────────────
    def test_institution_faculty_login_and_logout(self):
        """Valid faculty login creates session, logout completely invalidates it."""
        with patch("authentication.views.institution_logs.insert_one"):
            response = self.client.post(reverse("index"), {
                "office": "faculty",
                "username": "faculty_admin",
                "password": "fac@123",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("faculty_dashboard"))

        # Session should contain role FACULTY
        session = self.client.session
        self.assertEqual(session.get("role"), "FACULTY")

        # Visiting faculty dashboard should now succeed
        with patch("authentication.views.students_col") as mock_students, \
             patch("authentication.views.no_due_col") as mock_no_due:
            mock_students.find.return_value = []
            mock_no_due.find.return_value = []
            dash_response = self.client.get(reverse("faculty_dashboard"))
            self.assertEqual(dash_response.status_code, 200)
            self.assertIn("no-store", dash_response.headers.get("Cache-Control", ""))

        # Logout
        logout_response = self.client.get(reverse("logout"))
        self.assertEqual(logout_response.status_code, 302)
        self.assertEqual(logout_response.url, reverse("index"))

        # Session must be completely flushed
        self.assertIsNone(self.client.session.get("role"))

        # Simulating Browser Back Button: request faculty dashboard again
        back_response = self.client.get(reverse("faculty_dashboard"))
        self.assertEqual(back_response.status_code, 302)
        self.assertEqual(back_response.url, reverse("index"))

    def test_student_login_and_logout(self):
        """Valid student login establishes session and logout flushes it."""
        fake_id = ObjectId()
        fake_student = {
            "_id": fake_id,
            "reg_no": "830123104001",
            "dob": "2003-01-01",
            "name": "TEST STUDENT",
            "student_type": "Hosteller",
            "branch": "CSE",
            "year": 3,
            "semester": 5,
        }

        with patch("authentication.views.students_col.find_one", return_value=fake_student):
            response = self.client.post(reverse("student_login"), {
                "reg_no": "830123104001",
                "dob": "2003-01-01",
            })
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, reverse("student_dashboard"))

        # Session should contain role STUDENT and student_id
        session = self.client.session
        self.assertEqual(session.get("role"), "STUDENT")
        self.assertEqual(session.get("student_id"), str(fake_id))

        # Logout
        self.client.get(reverse("logout"))

        # Session must be flushed
        self.assertIsNone(self.client.session.get("role"))
        self.assertIsNone(self.client.session.get("student_id"))

        # Accessing student dashboard after logout redirects to login
        back_response = self.client.get(reverse("student_dashboard"))
        self.assertEqual(back_response.status_code, 302)
        self.assertEqual(back_response.url, reverse("index"))

    # ─────────────────────────────────────────────────────────────
    #  3. Role-Based Authorization
    # ─────────────────────────────────────────────────────────────
    def test_cross_role_access_prevented(self):
        """A user with role STUDENT cannot access FACULTY or OFFICE dashboards."""
        fake_id = ObjectId()
        session = self.client.session
        session["role"] = "STUDENT"
        session["student_id"] = str(fake_id)
        session.save()

        with patch("authentication.decorators.students_col.find_one", return_value={"_id": fake_id}):
            # Student accessing Faculty dashboard -> redirected
            response = self.client.get(reverse("faculty_dashboard"))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, reverse("index"))

            # Student accessing Hostel dashboard -> redirected
            response = self.client.get(reverse("hostel_dashboard"))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, reverse("index"))

    # ─────────────────────────────────────────────────────────────
    #  4. Defensive Field Access (500 Error Prevention)
    # ─────────────────────────────────────────────────────────────
    def test_faculty_dashboard_missing_fields_no_500(self):
        """Student documents with null/missing fields must render cleanly without KeyError or 500."""
        session = self.client.session
        session["role"] = "FACULTY"
        session.save()

        fake_id = ObjectId()
        # Incomplete student document missing phone, dob, semester, etc.
        incomplete_students = [
            {
                "_id": fake_id,
                "reg_no": "830123104002",
                "roll_no": "23CS002",
                "name": "PARTIAL DATA STUDENT",
                "branch": "CSE",
                "year": 3,
                # phone, dob, semester, student_type are missing
            }
        ]

        with patch("authentication.views.students_col.find", return_value=incomplete_students), \
             patch("authentication.views.no_due_col.find", return_value=[]):
            response = self.client.get(reverse("faculty_dashboard") + "?branch=CSE&year=3")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "PARTIAL DATA STUDENT")
            self.assertContains(response, "23CS002")

    def test_add_student_missing_post_fields_no_500(self):
        """Submitting malformed or empty POST to add_student must not throw KeyError / 500."""
        session = self.client.session
        session["role"] = "FACULTY"
        session.save()

        response = self.client.post(reverse("add_student"), {
            "branch": "CSE",
            "year": "3",
            # missing roll_no, reg_no, name, etc.
        })
        self.assertEqual(response.status_code, 302)
        # Should redirect back to faculty view with error in session
        self.assertIn("/faculty/?branch=CSE&year=3", response.url)

    def test_edit_student_invalid_values_no_500(self):
        """Submitting invalid integer or ObjectId to edit_student must not throw 500."""
        session = self.client.session
        session["role"] = "FACULTY"
        session.save()

        response = self.client.post(reverse("edit_student"), {
            "student_id": "invalid-object-id-string",
            "branch": "CSE",
            "year": "invalid-year",
            "semester": "invalid-sem",
            "roll_no": "23CS003",
            "reg_no": "830123104003",
            "name": "TEST",
        })
        self.assertEqual(response.status_code, 302)

    # ─────────────────────────────────────────────────────────────
    #  5. Protected APIs Authorization & Anti-Caching
    # ─────────────────────────────────────────────────────────────
    def test_office_student_status_api_unauthorized(self):
        """Protected API rejects unauthenticated requests with 302 to index."""
        response = self.client.get(reverse("student_status_api") + "?year=3")
        self.assertEqual(response.status_code, 302)

    def test_office_student_status_api_authorized(self):
        """Protected API allows authorized office roles and returns anti-cache headers."""
        session = self.client.session
        session["role"] = "LIBRARY"
        session.save()

        with patch("authentication.views.students_col.aggregate", return_value=[{"metadata": [{"total": 0}], "data": []}]):
            response = self.client.get(reverse("student_status_api") + "?year=3")
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    # ─────────────────────────────────────────────────────────────
    #  6. Office Dashboards & Department Specific Tests
    # ─────────────────────────────────────────────────────────────
    def test_department_login_and_dashboard(self):
        """Department login sets role and department, dashboard renders correctly."""
        with patch("authentication.views.institution_logs.insert_one"):
            response = self.client.post(reverse("index"), {
                "office": "department",
                "department": "CSE",
                "username": "cse_admin",
                "password": "cse@123",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("department_dashboard"))
        self.assertEqual(self.client.session.get("role"), "DEPARTMENT")
        self.assertEqual(self.client.session.get("department"), "CSE")

        with patch("authentication.views.no_due_col.aggregate", return_value=[]):
            dash_response = self.client.get(reverse("department_dashboard"))
            self.assertEqual(dash_response.status_code, 200)
            self.assertIn("no-store", dash_response.headers.get("Cache-Control", ""))

    def test_hostel_and_college_login(self):
        """Hostel and College logins establish correct roles and redirect to dashboards."""
        client_h = Client()
        client_c = Client()
        with patch("authentication.views.institution_logs.insert_one"):
            h_resp = client_h.post(reverse("index"), {
                "office": "hostel",
                "username": "hostel_admin",
                "password": "host@123",
            })
            self.assertEqual(h_resp.status_code, 302)
            self.assertEqual(h_resp.url, reverse("hostel_dashboard"))
            self.assertEqual(client_h.session.get("role"), "HOSTEL")

            c_resp = client_c.post(reverse("index"), {
                "office": "college",
                "username": "college_admin",
                "password": "col@123",
            })
            self.assertEqual(c_resp.status_code, 302)
            self.assertEqual(c_resp.url, reverse("college_dashboard"))
            self.assertEqual(client_c.session.get("role"), "COLLEGE")

    # ─────────────────────────────────────────────────────────────
    #  7. Faculty Promotion Password Gate
    # ─────────────────────────────────────────────────────────────
    def test_faculty_promotion_unlock_flow(self):
        """Promotion page requires password verification before displaying actions."""
        session = self.client.session
        session["role"] = "FACULTY"
        session.save()

        # Initial access shows promotion password form
        response = self.client.get(reverse("faculty_promotion"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "promotion_password")

        # Wrong password shows error
        wrong_resp = self.client.post(reverse("faculty_promotion"), {
            "promotion_password": "wrong_password",
        })
        self.assertEqual(wrong_resp.status_code, 200)
        self.assertFalse(self.client.session.get("promotion_unlocked", False))

        # Correct password unlocks
        with patch("authentication.views.portal_settings.find_one", return_value=None), \
             patch("authentication.views.students_col.count_documents", return_value=0):
            correct_resp = self.client.post(reverse("faculty_promotion"), {
                "promotion_password": "gces8301",
            })
            self.assertEqual(correct_resp.status_code, 302)
            self.assertEqual(correct_resp.url, reverse("faculty_promotion"))
            self.assertTrue(self.client.session.get("promotion_unlocked"))

    # ─────────────────────────────────────────────────────────────
    #  8. Expired / Stale Student ID Session Handling
    # ─────────────────────────────────────────────────────────────
    def test_deleted_student_session_auto_flushes(self):
        """If a student was deleted from DB, any subsequent request flushes session and redirects."""
        fake_id = ObjectId()
        session = self.client.session
        session["role"] = "STUDENT"
        session["student_id"] = str(fake_id)
        session.save()

        # Database returns None (student was deleted)
        with patch("authentication.decorators.students_col.find_one", return_value=None):
            response = self.client.get(reverse("student_dashboard"))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, reverse("index"))
            # Session must now be flushed
            self.assertIsNone(self.client.session.get("role"))
            self.assertIsNone(self.client.session.get("student_id"))

    # ─────────────────────────────────────────────────────────────
    #  9. Concurrent / Multiple User Sessions
    # ─────────────────────────────────────────────────────────────
    def test_multiple_simultaneous_users_isolated(self):
        """Multiple users logged in concurrently maintain isolated sessions."""
        client_faculty = Client()
        client_student = Client()

        # Login faculty
        with patch("authentication.views.institution_logs.insert_one"):
            client_faculty.post(reverse("index"), {
                "office": "faculty",
                "username": "faculty_admin",
                "password": "fac@123",
            })

        # Login student
        fake_id = ObjectId()
        with patch("authentication.views.students_col.find_one", return_value={"_id": fake_id, "student_type": "Hosteller"}):
            client_student.post(reverse("student_login"), {
                "reg_no": "830123104009",
                "dob": "2003-05-15",
            })

        # Check sessions are isolated
        self.assertEqual(client_faculty.session.get("role"), "FACULTY")
        self.assertEqual(client_student.session.get("role"), "STUDENT")

        # Faculty logs out
        client_faculty.get(reverse("logout"))
        self.assertIsNone(client_faculty.session.get("role"))

        # Student session remains active
        self.assertEqual(client_student.session.get("role"), "STUDENT")


class WebSocketAndRealTimeTests(TestCase):
    """
    Tests covering:
    - WebSocket consumer connection, authentication, and group routing
    - Real-time event broadcasting helpers (notify_student, notify_office, etc.)
    - Background maintenance auto-disable, timeout reset, and cooldown reset broadcasts
    - AJAX endpoint JSON responses for zero-reload DOM updates
    - Office Student Status API payload with student_id
    """

    def setUp(self):
        self.client = Client()

    # ─────────────────────────────────────────────────────────────
    #  1. Event Broadcaster Helper Tests
    # ─────────────────────────────────────────────────────────────
    @patch("authentication.events.get_channel_layer")
    @patch("authentication.events.async_to_sync")
    def test_notify_student_broadcast(self, mock_async_to_sync, mock_get_layer):
        from authentication.events import notify_student
        mock_layer = MagicMock()
        mock_get_layer.return_value = mock_layer

        student_id = ObjectId()
        notify_student(student_id, "request_status_updated", {
            "office": "LIBRARY",
            "status": "APPROVED"
        })

        mock_get_layer.assert_called_once()
        mock_async_to_sync.assert_called_once()

    @patch("authentication.events.get_channel_layer")
    @patch("authentication.events.async_to_sync")
    def test_notify_office_broadcast(self, mock_async_to_sync, mock_get_layer):
        from authentication.events import notify_office
        mock_layer = MagicMock()
        mock_get_layer.return_value = mock_layer

        notify_office("LIBRARY", "new_request_submitted", {
            "reg_no": "830123104001",
            "name": "TEST STUDENT"
        })

        mock_get_layer.assert_called_once()
        mock_async_to_sync.assert_called_once()

    @patch("authentication.events.get_channel_layer")
    @patch("authentication.events.async_to_sync")
    def test_notify_all_students_broadcast(self, mock_async_to_sync, mock_get_layer):
        from authentication.events import notify_all_students
        mock_layer = MagicMock()
        mock_get_layer.return_value = mock_layer

        notify_all_students("no_due_access_toggled", {
            "enabled": True
        })

        mock_get_layer.assert_called_once()
        mock_async_to_sync.assert_called_once()

    # ─────────────────────────────────────────────────────────────
    #  2. Maintenance Tasks Broadcast Tests
    # ─────────────────────────────────────────────────────────────
    @patch("authentication.maintenance.portal_settings")
    @patch("authentication.events.notify_all_students")
    @patch("authentication.events.notify_faculty")
    def test_maintenance_auto_disable_access_broadcast(self, mock_notify_fac, mock_notify_stu, mock_settings):
        from authentication.maintenance import _check_auto_disable_access
        from datetime import datetime, timedelta, timezone

        # Access was enabled, but auto_disable_at has passed
        past_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        mock_settings.find_one.return_value = {
            "_id": "global_config",
            "no_due_access_enabled": True,
            "auto_disable_at": past_time
        }

        _check_auto_disable_access()

        mock_settings.update_one.assert_called_once()
        mock_notify_stu.assert_called_once_with("no_due_access_toggled", {"enabled": False, "auto_disable_str": ""})
        mock_notify_fac.assert_called_once_with("no_due_access_toggled", {"enabled": False, "auto_disable_str": ""})

    @patch("authentication.maintenance.no_due_col")
    @patch("authentication.events.notify_student")
    def test_fast_cooldown_reset_broadcast(self, mock_notify_stu, mock_no_due):
        from authentication.maintenance import fast_cooldown_reset

        fake_id = ObjectId()
        mock_no_due.find.return_value = [
            {"_id": ObjectId(), "student_id": fake_id, "office": "LIBRARY", "attempts_used": 2}
        ]

        fast_cooldown_reset()

        mock_no_due.update_many.assert_called_once()
        # Verify both cooldown_expired and request_status_updated with attempts_used: 0 are broadcast
        calls = [call[0] for call in mock_notify_stu.call_args_list]
        event_names = [c[1] for c in calls]
        self.assertIn("cooldown_expired", event_names)
        self.assertIn("request_status_updated", event_names)

    @patch("authentication.maintenance.no_due_col")
    @patch("authentication.events.notify_student")
    @patch("authentication.events.notify_office")
    def test_reset_pending_timeout_no_dues_broadcast(self, mock_notify_office, mock_notify_student, mock_no_due):
        from authentication.maintenance import _expire_stale_pending

        doc_id = ObjectId()
        student_id = ObjectId()

        mock_no_due.find.return_value.limit.return_value = [
            {"_id": doc_id, "student_id": student_id, "office": "LIBRARY", "status": "PENDING", "attempts_used": 1}
        ]

        _expire_stale_pending()

        mock_no_due.update_one.assert_called_once()
        mock_notify_student.assert_called_once_with(student_id, "request_status_updated", {
            "office": "LIBRARY",
            "status": "NOT_SENT",
            "attempts_used": 0,
            "attempts_remaining": 2,
        })
        mock_notify_office.assert_called_once_with("LIBRARY", "request_expired", {
            "request_id": str(doc_id),
        })

    # ─────────────────────────────────────────────────────────────
    #  3. AJAX Views JSON Response Tests
    # ─────────────────────────────────────────────────────────────
    def test_send_no_due_request_ajax_success(self):
        """AJAX request to send_no_due returns JSON response without redirect."""
        fake_id = ObjectId()
        session = self.client.session
        session["role"] = "STUDENT"
        session["student_id"] = str(fake_id)
        session.save()

        fake_student = {"_id": fake_id, "reg_no": "830123104001", "name": "TEST", "branch": "CSE", "year": 3, "semester": 5}

        with patch("authentication.decorators.students_col.find_one", return_value={"_id": fake_id}), \
             patch("authentication.views.check_no_due_access_status", return_value=True), \
             patch("authentication.views.students_col.find_one", return_value=fake_student), \
             patch("authentication.views.no_due_col") as mock_no_due, \
             patch("authentication.views.notify_student"), \
             patch("authentication.views.notify_office"):
            mock_no_due.find_one.return_value = None

            response = self.client.post(
                reverse("send_no_due"),
                {"office": "LIBRARY", "format": "json"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("status"), "PENDING")

    def test_retry_request_ajax_success(self):
        """AJAX request to retry_request returns JSON response with NOT_SENT status."""
        fake_id = ObjectId()
        session = self.client.session
        session["role"] = "STUDENT"
        session["student_id"] = str(fake_id)
        session.save()

        existing_req = {"_id": ObjectId(), "student_id": fake_id, "office": "LIBRARY", "status": "REJECTED", "attempts_used": 1}

        with patch("authentication.decorators.students_col.find_one", return_value={"_id": fake_id}), \
             patch("authentication.views.check_no_due_access_status", return_value=True), \
             patch("authentication.views.no_due_col") as mock_no_due, \
             patch("authentication.views.notify_student"):
            mock_no_due.find_one.return_value = existing_req

            response = self.client.post(
                reverse("retry_request"),
                {"office": "LIBRARY", "format": "json"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("status"), "NOT_SENT")

    def test_office_student_status_api_contains_student_id(self):
        """Office Student Status API response includes student_id for each student."""
        session = self.client.session
        session["role"] = "LIBRARY"
        session.save()

        fake_id = ObjectId()
        mock_results = [{
            "metadata": [{"total": 1}],
            "data": [{
                "_id": fake_id,
                "reg_no": "830123104001",
                "name": "TEST",
                "branch": "CSE",
                "semester": 5,
                "is_75_scheme": False,
                "office_status": "Completed",
                "completed_time": None
            }]
        }]

        with patch("authentication.views.students_col.aggregate", return_value=mock_results):
            response = self.client.get(reverse("student_status_api") + "?year=3")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("students", data)
            self.assertEqual(len(data["students"]), 1)
            self.assertEqual(data["students"][0]["student_id"], str(fake_id))
            self.assertEqual(data["students"][0]["reg_no"], "830123104001")

    def test_excel_dob_parser(self):
        """Test _parse_excel_dob parses multiple date formats into YYYY-MM-DD."""
        from authentication.views import _parse_excel_dob
        from datetime import date, datetime

        # Date / datetime objects
        self.assertEqual(_parse_excel_dob(date(2003, 5, 15)), "2003-05-15")
        self.assertEqual(_parse_excel_dob(datetime(2003, 5, 15, 10, 30)), "2003-05-15")

        # Standard strings
        self.assertEqual(_parse_excel_dob("2003-05-15"), "2003-05-15")
        self.assertEqual(_parse_excel_dob("15-05-2003"), "2003-05-15")
        self.assertEqual(_parse_excel_dob("15/05/2003"), "2003-05-15")
        self.assertEqual(_parse_excel_dob("2003/05/15"), "2003-05-15")

        # Empty / None
        self.assertEqual(_parse_excel_dob(None), "")
        self.assertEqual(_parse_excel_dob(""), "")

    def test_import_students_excel_mongo_error_handling(self):
        """Test import_students_excel gracefully handles database connection exceptions."""
        from pymongo.errors import AutoReconnect
        import io
        from openpyxl import Workbook

        # Create minimal in-memory Excel workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Roll No", "Register No", "Name", "DOB", "Phone", "Semester"])
        ws.append(["23CS01", "830123104001", "JOHN DOE", "2003-05-15", "9876543210", 3])
        excel_file = io.BytesIO()
        wb.save(excel_file)
        excel_file.seek(0)
        excel_file.name = "test_students.xlsx"

        session = self.client.session
        session["role"] = "FACULTY"
        session.save()

        with patch("authentication.views.students_col.find", side_effect=AutoReconnect("SSL handshake failed")):
            response = self.client.post(
                reverse("import_students_excel"),
                {
                    "excel": excel_file,
                    "branch": "CSE",
                    "year": "2",
                }
            )
            # Should redirect back to faculty dashboard
            self.assertEqual(response.status_code, 302)
            self.assertIn("/faculty/?branch=CSE&year=2", response.url)

    # ─────────────────────────────────────────────────────────────
    #  8. Dynamic Department Management Tests
    # ─────────────────────────────────────────────────────────────
    def test_default_departments_seeding(self):
        """Default departments (CSE, ECE, EEE, MECH, CIVIL, MCT) must be seeded if empty."""
        from authentication.departments import get_all_departments, get_active_department_codes
        departments = get_all_departments()
        self.assertGreaterEqual(len(departments), 6)
        codes = get_active_department_codes()
        self.assertIn("CSE", codes)
        self.assertIn("ECE", codes)
        self.assertIn("EEE", codes)
        self.assertIn("MECH", codes)
        self.assertIn("CIVIL", codes)
        self.assertIn("MCT", codes)

    def test_add_department_success(self):
        """Faculty can add a new dynamic department (e.g. AI-DS)."""
        from authentication.departments import add_department, get_department_by_code
        from authentication.mongo import departments_col
        departments_col.delete_many({"code": "AI-DS"})

        success, result = add_department("Artificial Intelligence and Data Science", "AI-DS")
        self.assertTrue(success)
        self.assertEqual(result["code"], "AI-DS")
        self.assertEqual(result["name"], "Artificial Intelligence and Data Science")
        self.assertTrue(result["is_active"])

        dept = get_department_by_code("AI-DS")
        self.assertIsNotNone(dept)
        self.assertEqual(dept["name"], "Artificial Intelligence and Data Science")
        departments_col.delete_many({"code": "AI-DS"})

    def test_add_duplicate_department_code_rejected(self):
        """Duplicate department code (case-insensitive) must be rejected."""
        from authentication.departments import add_department
        success, error = add_department("Computer Science Duplicate", "CSE")
        self.assertFalse(success)
        self.assertIn("already exists", str(error).lower())

        success_lower, error_lower = add_department("Computer Science Duplicate 2", "cse")
        self.assertFalse(success_lower)
        self.assertIn("already exists", str(error_lower).lower())

    def test_add_duplicate_department_name_rejected(self):
        """Duplicate department name (case-insensitive) must be rejected."""
        from authentication.departments import add_department
        success, error = add_department("computer science and engineering", "CSE-DUP")
        self.assertFalse(success)
        self.assertIn("already exists", str(error).lower())

    def test_edit_department(self):
        """Faculty can edit department name and code."""
        from authentication.departments import add_department, update_department, get_department_by_id
        from authentication.mongo import departments_col
        departments_col.delete_many({"code": {"$in": ["BME", "BMET"]}})

        success, new_dept = add_department("Bio Medical Engineering", "BME")
        self.assertTrue(success)
        dept_id = new_dept["id"]

        # Update name and code
        success, updated = update_department(dept_id, "Biomedical Engineering & Tech", "BMET")
        self.assertTrue(success)
        self.assertEqual(updated["name"], "Biomedical Engineering & Tech")
        self.assertEqual(updated["code"], "BMET")

        fetched = get_department_by_id(dept_id)
        self.assertEqual(fetched["code"], "BMET")
        departments_col.delete_many({"code": {"$in": ["BME", "BMET"]}})

    def test_toggle_department_status(self):
        """Faculty can toggle active/inactive status of a department."""
        from authentication.departments import add_department, toggle_department_status, get_active_department_codes
        from authentication.mongo import departments_col
        departments_col.delete_many({"code": "ROBO"})

        success, dept = add_department("Robotics and Automation", "ROBO")
        self.assertTrue(success)
        dept_id = dept["id"]

        # Deactivate
        success, updated = toggle_department_status(dept_id, is_active=False)
        self.assertTrue(success)
        self.assertFalse(updated["is_active"])
        self.assertNotIn("ROBO", get_active_department_codes())

        # Reactivate
        success, updated = toggle_department_status(dept_id, is_active=True)
        self.assertTrue(success)
        self.assertTrue(updated["is_active"])
        self.assertIn("ROBO", get_active_department_codes())
        departments_col.delete_many({"code": "ROBO"})

    def test_delete_department_blocked_when_students_exist(self):
        """Deleting a department associated with students must be blocked."""
        from authentication.departments import add_department, delete_department
        from authentication.mongo import students_col, departments_col
        departments_col.delete_many({"code": "CHEM"})

        success, dept = add_department("Chemical Engineering", "CHEM")
        self.assertTrue(success)
        dept_id = dept["id"]

        # Insert a student in CHEM
        fake_id = ObjectId()
        students_col.insert_one({
            "_id": fake_id,
            "roll_no": "23CH01",
            "reg_no": "830123108001",
            "name": "TEST CHEM STUDENT",
            "branch": "CHEM",
            "year": 1,
            "semester": 1,
        })

        try:
            success, msg = delete_department(dept_id)
            self.assertFalse(success)
            self.assertIn("cannot be permanently deleted", msg)
            self.assertIn("deactivate it instead", msg)
        finally:
            students_col.delete_one({"_id": fake_id})
            departments_col.delete_many({"code": "CHEM"})

    def test_delete_department_allowed_when_no_students_exist(self):
        """Deleting a department with 0 associated students must succeed."""
        from authentication.departments import add_department, delete_department, get_department_by_id
        from authentication.mongo import departments_col
        departments_col.delete_many({"code": "MARINE"})

        success, dept = add_department("Marine Engineering", "MARINE")
        self.assertTrue(success)
        dept_id = dept["id"]

        success, msg = delete_department(dept_id)
        self.assertTrue(success)
        self.assertIn("deleted successfully", msg)
        self.assertIsNone(get_department_by_id(dept_id))

    def test_department_management_endpoints_authorization(self):
        """Unauthenticated or locked promotion requests to department endpoints must be blocked."""
        # Unauthenticated
        res = self.client.post(reverse("add_department"), {"name": "Test", "code": "TST"})
        self.assertEqual(res.status_code, 302)

        # Logged in as Faculty without promotion unlocked
        session = self.client.session
        session["role"] = "FACULTY"
        session["promotion_unlocked"] = False
        session.save()

        res = self.client.post(
            reverse("add_department"),
            {"name": "Test", "code": "TST"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(res.status_code, 403)

    def test_immediate_no_due_log_creation_for_day_scholar(self):
        """As soon as all 3 required offices for a Day Scholar are approved, completion log is created immediately with current year/sem."""
        from authentication.utils import record_student_no_due_completion
        from authentication.mongo import students_col, no_due_col, promotion_logs

        test_id = ObjectId()
        students_col.insert_one({
            "_id": test_id,
            "roll_no": "21CS099",
            "reg_no": "830121104099",
            "name": "TEST COMPLETION STUDENT",
            "branch": "CSE",
            "year": 3,
            "semester": 6,
            "student_type": "Day Scholar",
        })

        try:
            # 2 out of 3 approved -> should not record yet
            no_due_col.insert_many([
                {"student_id": test_id, "office": "LIBRARY", "status": "APPROVED"},
                {"student_id": test_id, "office": "COLLEGE", "status": "APPROVED"},
                {"student_id": test_id, "office": "DEPARTMENT", "status": "PENDING"},
            ])
            self.assertFalse(record_student_no_due_completion(test_id))
            self.assertEqual(promotion_logs.count_documents({"student_id": test_id}), 0)

            # Approve 3rd office (DEPARTMENT)
            no_due_col.update_one({"student_id": test_id, "office": "DEPARTMENT"}, {"$set": {"status": "APPROVED"}})
            self.assertTrue(record_student_no_due_completion(test_id))

            # Verify log was created immediately with current Year 3, Semester 6
            log = promotion_logs.find_one({"student_id": test_id})
            self.assertIsNotNone(log)
            self.assertEqual(log["previous_year"], 3)
            self.assertEqual(log["previous_semester"], 6)
            self.assertTrue(log["no_due_cleared"])
            self.assertEqual(log["status"], "Completed")

            # Duplicate call should NOT insert another log
            self.assertTrue(record_student_no_due_completion(test_id))
            self.assertEqual(promotion_logs.count_documents({"student_id": test_id}), 1)
        finally:
            students_col.delete_one({"_id": test_id})
            no_due_col.delete_many({"student_id": test_id})
            promotion_logs.delete_many({"student_id": test_id})

    def test_promotion_preserves_existing_completion_log(self):
        """When promoted to Semester 7, the previous Semester 6 completion log must be preserved without duplicate."""
        from authentication.utils import record_student_no_due_completion
        from authentication.mongo import students_col, no_due_col, promotion_logs

        test_id = ObjectId()
        students_col.insert_one({
            "_id": test_id,
            "roll_no": "21CS098",
            "reg_no": "830121104098",
            "name": "TEST PROMOTION PRESERVE",
            "branch": "CSE",
            "year": 3,
            "semester": 6,
            "student_type": "Hosteller",
        })

        try:
            # 4 approved offices for hosteller
            no_due_col.insert_many([
                {"student_id": test_id, "office": "LIBRARY", "status": "APPROVED"},
                {"student_id": test_id, "office": "HOSTEL", "status": "APPROVED"},
                {"student_id": test_id, "office": "COLLEGE", "status": "APPROVED"},
                {"student_id": test_id, "office": "DEPARTMENT", "status": "APPROVED"},
            ])
            # Step 1: All required offices completed -> log created for Year 3, Semester 6
            self.assertTrue(record_student_no_due_completion(test_id))
            initial_log = promotion_logs.find_one({"student_id": test_id})
            self.assertIsNotNone(initial_log)
            self.assertEqual(initial_log["previous_semester"], 6)
            self.assertEqual(initial_log["previous_year"], 3)
            initial_completion_time = initial_log["completion_time"]

            # Step 2: Later promotion happens via faculty promotion action
            session = self.client.session
            session["role"] = "FACULTY"
            session["promotion_unlocked"] = True
            session.save()

            res = self.client.post(
                reverse("promote_students"),
                {"from_semester": "6"},
                HTTP_REFERER="/faculty/promotion/"
            )
            self.assertEqual(res.status_code, 302)

            # Verify student is now Year 4, Semester 7
            updated_student = students_col.find_one({"_id": test_id})
            self.assertEqual(updated_student["semester"], 7)
            self.assertEqual(updated_student["year"], 4)

            # Verify logs count is still 1 (no duplicate) and previous Semester 6 is preserved
            logs = list(promotion_logs.find({"student_id": test_id}))
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0]["previous_semester"], 6)
            self.assertEqual(logs[0]["previous_year"], 3)
            self.assertEqual(logs[0]["completion_time"], initial_completion_time)
            self.assertTrue(logs[0]["no_due_cleared"])
        finally:
            students_col.delete_one({"_id": test_id})
            no_due_col.delete_many({"student_id": test_id})
            promotion_logs.delete_many({"student_id": test_id})

    def test_faculty_report_pdf_view_authorization(self):
        """Non-faculty users cannot generate faculty PDF reports."""
        res = self.client.get(reverse("faculty_report_pdf"), {"branch": "CSE", "year": "3"})
        self.assertEqual(res.status_code, 302)

    def test_faculty_report_pdf_view_success(self):
        """Faculty can generate and download clearance PDF report for selected Branch and Year."""
        from authentication.mongo import students_col, no_due_col
        test_id = ObjectId()
        students_col.insert_one({
            "_id": test_id,
            "roll_no": "22CS050",
            "reg_no": "830122104050",
            "name": "REPORT TEST STUDENT",
            "branch": "CSE",
            "year": 3,
            "semester": 5,
            "student_type": "Day Scholar",
        })

        try:
            session = self.client.session
            session["role"] = "FACULTY"
            session.save()

            # Missing params -> 400
            bad_res = self.client.get(reverse("faculty_report_pdf"), {"branch": "CSE"})
            self.assertEqual(bad_res.status_code, 400)

            # Valid branch + year -> 200 PDF
            res = self.client.get(reverse("faculty_report_pdf"), {"branch": "CSE", "year": "3"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res["Content-Type"], "application/pdf")
            self.assertTrue(res.content.startswith(b"%PDF"))
        finally:
            students_col.delete_one({"_id": test_id})
            no_due_col.delete_many({"student_id": test_id})





