import unittest
from unittest.mock import patch, MagicMock
# No SQL database: SimpleTestCase (MongoDB is used directly).
from django.test import SimpleTestCase as TestCase, Client
from django.urls import reverse
from django.conf import settings
from bson import ObjectId
from django.contrib.auth.hashers import make_password, check_password


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
            "password_hash": make_password("Gces#Pass2026"),
            "profile_completed": True,
        }

        with patch("authentication.views.students_col.find_one", return_value=fake_student):
            response = self.client.post(reverse("student_login"), {
                "reg_no": "830123104001",
                "password": "Gces#Pass2026",
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

        with patch("authentication.decorators.students_col.find_one", return_value={"_id": fake_id, "profile_completed": True}):
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
        fake_student = {"_id": fake_id, "student_type": "Hosteller",
                        "password_hash": make_password("Gces#Pass2026"), "profile_completed": True}
        with patch("authentication.views.students_col.find_one", return_value=fake_student):
            client_student.post(reverse("student_login"), {
                "reg_no": "830123104009",
                "password": "Gces#Pass2026",
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

        fake_student = {"_id": fake_id, "reg_no": "830123104001", "name": "TEST", "branch": "CSE", "year": 3, "semester": 5, "profile_completed": True}

        with patch("authentication.decorators.students_col.find_one", return_value={"_id": fake_id, "profile_completed": True}), \
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

        with patch("authentication.decorators.students_col.find_one", return_value={"_id": fake_id, "profile_completed": True}), \
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
            from authentication.promotion import promotion_runs, promotion_run_items
            run_ids = promotion_run_items.distinct("run_id", {"student_id": test_id})
            promotion_run_items.delete_many({"run_id": {"$in": run_ids}})
            promotion_runs.delete_many({"_id": {"$in": run_ids}})
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

            # Missing params -> back to the directory with a message (no bare text page)
            bad_res = self.client.get(reverse("faculty_report_pdf"), {"branch": "CSE"})
            self.assertEqual(bad_res.status_code, 302)
            self.assertTrue(bad_res.url.startswith(reverse("faculty_dashboard")))

            # Empty class -> back to that class's directory with a clear warning
            empty = self.client.get(reverse("faculty_report_pdf"), {"branch": "CSE", "year": "1"}, follow=True)
            self.assertEqual(empty.redirect_chain[0][1], 302)
            self.assertIn("branch=CSE", empty.redirect_chain[0][0])
            self.assertContains(empty, "no students in CSE")

            # Valid branch + year -> 200 PDF
            res = self.client.get(reverse("faculty_report_pdf"), {"branch": "CSE", "year": "3"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res["Content-Type"], "application/pdf")
            self.assertTrue(res.content.startswith(b"%PDF"))
        finally:
            students_col.delete_one({"_id": test_id})
            no_due_col.delete_many({"student_id": test_id})


class StudentRegistrationAndLoginTests(TestCase):
    """
    Faculty Add / Bulk Add, first-time login (Register No as temporary
    password), mandatory profile setup, and password-based login.
    Runs against the real MongoDB (like the department tests). All records use
    the reserved Register Number prefix 99998800 and are cleaned up.
    """

    PREFIX = "99998800"
    NEW_PASSWORD = "Gces#Pass2026"

    def setUp(self):
        from authentication.departments import ensure_default_departments
        ensure_default_departments()
        self._cleanup()
        self.client = Client()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        from authentication.mongo import students_col
        students_col.delete_many({"reg_no": {"$regex": f"^{self.PREFIX}"}})
        students_col.delete_many({"roll_no": {"$in": ["99TS001", "99TS002"]}})

    def _reg(self, n):
        return f"{self.PREFIX}{n:04d}"

    def _login_faculty(self):
        session = self.client.session
        session["role"] = "FACULTY"
        session.save()

    def _students(self, query=None):
        from authentication.mongo import students_col
        q = {"reg_no": {"$regex": f"^{self.PREFIX}"}}
        q.update(query or {})
        return list(students_col.find(q))

    def _add_student(self, reg_no, branch="CSE", year="3", semester="5"):
        return self.client.post(reverse("add_student"), {
            "reg_no": reg_no, "branch": branch, "year": year, "semester": semester,
        })

    def _valid_profile(self, **overrides):
        data = {
            "name": "Test   Student",
            "roll_no": "99ts001",
            "dob": "2004-06-15",
            "phone": "9876543210",
            "password": self.NEW_PASSWORD,
            "confirm_password": self.NEW_PASSWORD,
        }
        data.update(overrides)
        return data

    def _first_time_login(self, reg_no):
        return self.client.post(reverse("student_login"), {"reg_no": reg_no, "password": reg_no})

    # ── Individual registration ──────────────────────────────────
    def test_add_student_creates_pending_student(self):
        self._login_faculty()
        reg = self._reg(1)
        res = self._add_student(reg)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/faculty/?branch=CSE&year=3", res.url)

        docs = self._students()
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertEqual((doc["branch"], doc["year"], doc["semester"]), ("CSE", 3, 5))
        self.assertFalse(doc["profile_completed"])
        self.assertNotIn("password_hash", doc)
        self.assertNotIn("roll_no", doc)  # must stay absent for the partial unique index

        dash = self.client.get(reverse("faculty_dashboard") + "?branch=CSE&year=3")
        self.assertContains(dash, reg)
        self.assertContains(dash, "Pending")

    def test_add_student_rejects_duplicates_and_invalid_input(self):
        self._login_faculty()
        reg = self._reg(2)
        self._add_student(reg)
        self._add_student(reg)  # duplicate
        self.assertEqual(len(self._students()), 1)
        self.assertIn("already exists", self.client.session.get("add_error", ""))

        self._add_student(self._reg(3), semester="2")      # sem 2 is not a Year-3 semester
        self._add_student("12345", semester="5")            # not 12 digits
        self._add_student(self._reg(4), branch="NOPE")      # unknown department
        self.assertEqual(len(self._students()), 1)

    def test_add_student_requires_faculty(self):
        res = self._add_student(self._reg(5))
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, reverse("index"))
        self.assertEqual(len(self._students()), 0)

    # ── Bulk registration ────────────────────────────────────────
    def test_bulk_add_creates_range_and_skips_existing(self):
        self._login_faculty()
        self._add_student(self._reg(12))  # already exists inside the range

        res = self.client.post(reverse("bulk_add_students"), {
            "start_reg_no": self._reg(10), "end_reg_no": self._reg(14),
            "branch": "CSE", "year": "3", "semester": "5",
        }, follow=True)
        self.assertEqual(res.status_code, 200)

        regs = sorted(d["reg_no"] for d in self._students())
        self.assertEqual(regs, [self._reg(n) for n in range(10, 15)])  # no duplicates
        msgs = [str(m) for m in res.context["messages"]]
        self.assertTrue(any("Added: 4" in m and "Skipped (already exist): 1" in m for m in msgs), msgs)
        self.assertTrue(any(self._reg(12) in m for m in msgs), msgs)

        # Running the same range again adds nothing
        res = self.client.post(reverse("bulk_add_students"), {
            "start_reg_no": self._reg(10), "end_reg_no": self._reg(14),
            "branch": "CSE", "year": "3", "semester": "5",
        }, follow=True)
        msgs = [str(m) for m in res.context["messages"]]
        self.assertTrue(any("Added: 0" in m and "Skipped (already exist): 5" in m for m in msgs), msgs)
        self.assertEqual(len(self._students()), 5)

    def test_bulk_add_rejects_invalid_ranges(self):
        self._login_faculty()
        base = {"branch": "CSE", "year": "3", "semester": "5"}
        bad_ranges = [
            (self._reg(20), self._reg(10)),          # start > end
            (self._reg(0), self._reg(600)),          # over BULK_ADD_LIMIT
            ("123", self._reg(5)),                   # not 12 digits
        ]
        for start, end in bad_ranges:
            self.client.post(reverse("bulk_add_students"), dict(base, start_reg_no=start, end_reg_no=end))
        self.client.post(reverse("bulk_add_students"),
                         dict(base, start_reg_no=self._reg(1), end_reg_no=self._reg(2), semester="8"))
        self.assertEqual(len(self._students()), 0)

    # ── First-time login + profile completion ────────────────────
    def test_first_time_login_goes_to_profile_setup_only(self):
        self._login_faculty()
        reg = self._reg(30)
        self._add_student(reg)
        self.client = Client()

        # Wrong temporary password
        res = self.client.post(reverse("student_login"), {"reg_no": reg, "password": "wrong"})
        self.assertEqual(res.url, reverse("index"))
        self.assertIsNone(self.client.session.get("pending_student_id"))

        res = self._first_time_login(reg)
        self.assertEqual(res.url, reverse("student_profile_setup"))
        self.assertIsNone(self.client.session.get("role"))  # NOT logged in yet

        # Pending student cannot reach student pages
        res = self.client.get(reverse("student_dashboard"))
        self.assertEqual(res.url, reverse("index"))
        # Login page bounces back to setup while it is unfinished
        res = self.client.get(reverse("index"))
        self.assertEqual(res.url, reverse("student_profile_setup"))

    def test_profile_setup_validates_then_completes_and_logs_in(self):
        self._login_faculty()
        reg = self._reg(31)
        self._add_student(reg)
        self.client = Client()
        self._first_time_login(reg)

        res = self.client.get(reverse("student_profile_setup"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, reg)

        invalid_cases = [
            self._valid_profile(confirm_password="Different#1"),
            self._valid_profile(password=reg, confirm_password=reg),
            self._valid_profile(password="12345678", confirm_password="12345678"),
            self._valid_profile(phone="12345"),
            self._valid_profile(dob="2999-01-01"),
            self._valid_profile(name=""),
        ]
        for data in invalid_cases:
            res = self.client.post(reverse("student_profile_setup"), data)
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.context["errors"])
        self.assertFalse(self._students()[0]["profile_completed"])

        res = self.client.post(reverse("student_profile_setup"), self._valid_profile())
        self.assertEqual(res.url, reverse("student_dashboard"))
        self.assertEqual(self.client.session.get("role"), "STUDENT")
        self.assertIsNone(self.client.session.get("pending_student_id"))

        doc = self._students()[0]
        self.assertTrue(doc["profile_completed"])
        self.assertEqual(doc["name"], "TEST STUDENT")
        self.assertEqual(doc["roll_no"], "99TS001")
        self.assertEqual((doc["dob"], doc["phone"]), ("2004-06-15", "9876543210"))
        self.assertNotEqual(doc["password_hash"], self.NEW_PASSWORD)
        self.assertTrue(check_password(self.NEW_PASSWORD, doc["password_hash"]))

        # Faculty now sees Completed
        faculty = Client()
        s = faculty.session
        s["role"] = "FACULTY"
        s.save()
        dash = faculty.get(reverse("faculty_dashboard") + "?branch=CSE&year=3")
        self.assertContains(dash, "Completed")

    def test_subsequent_login_requires_new_password(self):
        self._login_faculty()
        reg = self._reg(32)
        self._add_student(reg)
        self.client = Client()
        self._first_time_login(reg)
        self.client.post(reverse("student_profile_setup"), self._valid_profile())
        self.client.get(reverse("logout"))

        # Register Number no longer works as a password
        res = self._first_time_login(reg)
        self.assertEqual(res.url, reverse("index"))
        self.assertIsNone(self.client.session.get("role"))
        self.assertIsNone(self.client.session.get("pending_student_id"))

        res = self.client.post(reverse("student_login"), {"reg_no": reg, "password": self.NEW_PASSWORD})
        self.assertEqual(res.url, reverse("student_dashboard"))
        self.assertEqual(self.client.session.get("role"), "STUDENT")

        # Setup page cannot be reused to overwrite the password
        res = self.client.get(reverse("student_profile_setup"))
        self.assertEqual(res.url, reverse("student_dashboard"))

    def test_profile_setup_rejects_duplicate_roll_no(self):
        from authentication.mongo import students_col
        students_col.insert_one({"reg_no": self._reg(40), "roll_no": "99TS002", "name": "OTHER",
                                 "branch": "CSE", "year": 3, "semester": 5, "profile_completed": True})
        self._login_faculty()
        reg = self._reg(41)
        self._add_student(reg)
        self.client = Client()
        self._first_time_login(reg)
        res = self.client.post(reverse("student_profile_setup"), self._valid_profile(roll_no="99TS002"))
        self.assertEqual(res.status_code, 200)
        self.assertIn("This Roll Number is already registered to another student.", res.context["errors"])
        self.assertFalse(students_col.find_one({"reg_no": reg})["profile_completed"])

    # ── Existing (pre-password) student records ──────────────────
    def test_existing_student_record_is_prefilled_and_can_complete(self):
        from authentication.mongo import students_col
        reg = self._reg(50)
        students_col.insert_one({
            "reg_no": reg, "roll_no": "99TS001", "name": "LEGACY STUDENT", "dob": "2003-02-01",
            "phone": "9123456789", "branch": "CSE", "year": 3, "semester": 5,
            "student_type": "Day Scholar",
        })

        res = self._first_time_login(reg)
        self.assertEqual(res.url, reverse("student_profile_setup"))
        res = self.client.get(reverse("student_profile_setup"))
        self.assertContains(res, "LEGACY STUDENT")
        self.assertContains(res, "99TS001")
        self.assertContains(res, "2003-02-01")
        self.assertContains(res, "Day Scholar")

        # Keeping their own roll no must not count as a duplicate
        res = self.client.post(reverse("student_profile_setup"), self._valid_profile(
            name="LEGACY STUDENT", roll_no="99TS001", dob="2003-02-01", phone="9123456789"))
        self.assertEqual(res.url, reverse("student_dashboard"))
        doc = students_col.find_one({"reg_no": reg})
        self.assertTrue(doc["profile_completed"])
        self.assertEqual(doc["student_type"], "Day Scholar")  # untouched

    def test_old_student_session_without_profile_is_logged_out(self):
        from authentication.mongo import students_col
        reg = self._reg(60)
        sid = students_col.insert_one({"reg_no": reg, "name": "OLD SESSION", "branch": "CSE",
                                       "year": 3, "semester": 5}).inserted_id
        session = self.client.session
        session["role"] = "STUDENT"
        session["student_id"] = str(sid)
        session.save()

        res = self.client.get(reverse("student_dashboard"))
        self.assertEqual(res.url, reverse("index"))
        self.assertIsNone(self.client.session.get("role"))

    def test_profile_setup_requires_first_time_login(self):
        res = self.client.get(reverse("student_profile_setup"))
        self.assertEqual(res.url, reverse("index"))

    def test_login_page_has_password_and_forgot_password(self):
        res = self.client.get(reverse("index"))
        self.assertContains(res, 'name="password"', count=2)  # institution + student
        self.assertNotContains(res, 'name="dob"')
        self.assertContains(res, "Forgot Password?")


class MongoSessionStoreTests(TestCase):
    """Login sessions are stored in MongoDB (authentication/mongo_sessions.py)."""

    def setUp(self):
        from authentication.mongo_sessions import SessionStore, sessions_col
        self.SessionStore = SessionStore
        self.col = sessions_col
        self.keys = []

    def tearDown(self):
        self.col.delete_many({"_id": {"$in": self.keys}})

    def _new(self, **data):
        store = self.SessionStore()
        for k, v in data.items():
            store[k] = v
        store.save()
        self.keys.append(store.session_key)
        return store

    def test_save_and_load_roundtrip(self):
        store = self._new(role="LIBRARY", department="CSE")
        doc = self.col.find_one({"_id": store.session_key})
        self.assertIsNotNone(doc)
        self.assertIn("expire_date", doc)
        loaded = self.SessionStore(session_key=store.session_key)
        self.assertEqual(loaded.get("role"), "LIBRARY")
        self.assertEqual(loaded.get("department"), "CSE")

    def test_expired_session_is_empty(self):
        from datetime import datetime, timedelta, timezone
        store = self._new(role="FACULTY")
        self.col.update_one({"_id": store.session_key},
                            {"$set": {"expire_date": datetime.now(timezone.utc) - timedelta(seconds=1)}})
        loaded = self.SessionStore(session_key=store.session_key)
        self.assertIsNone(loaded.get("role"))

    def test_flush_deletes_document(self):
        store = self._new(role="HOSTEL")
        key = store.session_key
        store.flush()
        self.assertIsNone(self.col.find_one({"_id": key}))

    def test_cycle_key_moves_data(self):
        store = self._new(role="COLLEGE")
        old_key = store.session_key
        store.cycle_key()
        self.keys.append(store.session_key)
        self.assertNotEqual(old_key, store.session_key)
        self.assertIsNone(self.col.find_one({"_id": old_key}))
        self.assertEqual(self.SessionStore(session_key=store.session_key).get("role"), "COLLEGE")

    def test_unknown_key_gives_empty_session(self):
        loaded = self.SessionStore(session_key="doesnotexist0000000000000000000000")
        self.assertEqual(dict(loaded.items()), {})

    def test_login_creates_mongo_session(self):
        from django.conf import settings
        c = Client()
        c.post("/", {"office": "library", "username": "library_admin", "password": "lib@123"})
        key = c.cookies[settings.SESSION_COOKIE_NAME].value
        self.keys.append(key)
        doc = self.col.find_one({"_id": key})
        self.assertIsNotNone(doc)
        self.assertEqual(self.SessionStore(session_key=key).get("role"), "LIBRARY")


TEST_SSO_SECRET = "test-hostel-sso-secret-0123456789abcdef"


class HostelSSOTests(TestCase):
    """Hostel Admin App single sign-on endpoint (authentication/sso.py)."""

    URL = "/sso/hostel/"

    def setUp(self):
        from authentication.sso import sso_nonces
        self.nonces = sso_nonces
        self.used = []
        self._override = self.settings(HOSTEL_SSO_SECRET=TEST_SSO_SECRET, HOSTEL_SSO_MAX_AGE=60)
        self._override.enable()

    def tearDown(self):
        self._override.disable()
        self.nonces.delete_many({"_id": {"$in": self.used}})

    def _params(self, ts=None, nonce=None, user="warden01", secret=TEST_SSO_SECRET):
        import secrets as _s
        import time as _t
        from authentication.sso import sign
        ts = str(int(_t.time()) if ts is None else ts)
        nonce = nonce or _s.token_urlsafe(24)
        self.used.append(nonce)
        return {"ts": ts, "nonce": nonce, "user": user, "sig": sign(secret, ts, nonce, user)}

    def _role(self, client):
        return client.session.get("role")

    def test_valid_get_logs_in_as_hostel(self):
        c = Client()
        r = c.get(self.URL, self._params())
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/hostel/")
        self.assertEqual(self._role(c), "HOSTEL")
        self.assertEqual(c.session.get("login_method"), "SSO")

    def test_valid_post_logs_in_without_csrf_token(self):
        c = Client(enforce_csrf_checks=True)
        r = c.post(self.URL, self._params())
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._role(c), "HOSTEL")

    def test_user_is_optional(self):
        c = Client()
        r = c.get(self.URL, self._params(user=""))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._role(c), "HOSTEL")

    def test_bad_signature_rejected(self):
        c = Client()
        p = self._params()
        p["sig"] = "0" * 64
        r = c.get(self.URL, p)
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self._role(c))

    def test_wrong_secret_rejected(self):
        c = Client()
        r = c.get(self.URL, self._params(secret="some-other-secret-that-is-long-enough-xx"))
        self.assertEqual(r.status_code, 403)

    def test_tampered_user_rejected(self):
        c = Client()
        p = self._params(user="warden01")
        p["user"] = "admin"
        self.assertEqual(c.get(self.URL, p).status_code, 403)

    def test_expired_token_rejected(self):
        import time as _t
        c = Client()
        r = c.get(self.URL, self._params(ts=int(_t.time()) - 120))
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self._role(c))

    def test_far_future_token_rejected(self):
        import time as _t
        r = Client().get(self.URL, self._params(ts=int(_t.time()) + 600))
        self.assertEqual(r.status_code, 403)

    def test_replay_rejected(self):
        p = self._params()
        self.assertEqual(Client().get(self.URL, p).status_code, 302)
        c2 = Client()
        self.assertEqual(c2.get(self.URL, p).status_code, 403)
        self.assertIsNone(self._role(c2))

    def test_missing_or_malformed_params_rejected(self):
        c = Client()
        self.assertEqual(c.get(self.URL).status_code, 403)
        p = self._params()
        p["nonce"] = "short"
        self.assertEqual(c.get(self.URL, p).status_code, 403)

    def test_disabled_when_secret_not_configured(self):
        with self.settings(HOSTEL_SSO_SECRET=""):
            self.assertEqual(Client().get(self.URL, self._params()).status_code, 404)
        with self.settings(HOSTEL_SSO_SECRET="too-short"):
            self.assertEqual(Client().get(self.URL, self._params(secret="too-short")).status_code, 404)

    def test_replaces_existing_session(self):
        c = Client()
        s = c.session
        s["role"] = "STUDENT"
        s["student_id"] = "507f1f77bcf86cd799439011"
        s.save()
        old_key = s.session_key
        c.cookies["sessionid"] = old_key
        r = c.get(self.URL, self._params())
        self.assertEqual(r.status_code, 302)
        self.assertNotEqual(c.cookies["sessionid"].value, old_key)
        self.assertEqual(self._role(c), "HOSTEL")
        self.assertIsNone(c.session.get("student_id"))

    def test_sso_session_can_open_hostel_dashboard_only(self):
        c = Client()
        c.get(self.URL, self._params())
        self.assertEqual(c.get("/hostel/").status_code, 200)
        # still just the Hostel role: other dashboards bounce back
        self.assertEqual(c.get("/library/").status_code, 302)
        self.assertEqual(c.get("/faculty/").status_code, 302)


class ClearanceRequestRulesTests(TestCase):
    """Regression tests for bugs found by the concurrent end-to-end run."""

    PREFIX = "99998810"

    def setUp(self):
        from datetime import datetime, timedelta, timezone
        from authentication.mongo import db
        self.db = db
        self._cleanup()
        cfg = db.portal_settings.find_one({"_id": "global_config"}) or {}
        self._saved_cfg = {k: cfg.get(k) for k in ("no_due_access_enabled", "auto_disable_at")}
        db.portal_settings.update_one({"_id": "global_config"}, {"$set": {
            "no_due_access_enabled": True,
            "auto_disable_at": datetime.now(timezone.utc) + timedelta(hours=1)}}, upsert=True)
        self.cse = self._student("0001", "CSE", "Hosteller")
        self.day = self._student("0002", "CSE", "Day Scholar")
        self.ece = self._student("0003", "ECE", "Day Scholar")

    def tearDown(self):
        self._cleanup()
        self.db.portal_settings.update_one({"_id": "global_config"}, {"$set": self._saved_cfg})

    def _cleanup(self):
        ids = [s["_id"] for s in self.db.students.find({"reg_no": {"$regex": f"^{self.PREFIX}"}}, {"_id": 1})]
        self.db.no_due_requests.delete_many({"student_id": {"$in": ids}})
        self.db.students.delete_many({"_id": {"$in": ids}})

    def _student(self, suffix, branch, stype):
        return self.db.students.insert_one({
            "reg_no": self.PREFIX + suffix, "name": "RULE TEST", "branch": branch, "year": 4, "semester": 7,
            "student_type": stype, "is_75_scheme": False, "profile_completed": True,
            "password_hash": make_password("x")}).inserted_id

    def _client(self, **sess):
        c = Client()
        s = c.session
        for k, v in sess.items():
            s[k] = v
        s.save()
        from django.conf import settings
        c.cookies[settings.SESSION_COOKIE_NAME] = s.session_key
        return c

    def _as_student(self, sid):
        return self._client(role="STUDENT", student_id=str(sid))

    def _send(self, c, office):
        return c.post("/student/send/", {"office": office, "format": "json"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def _doc(self, sid, office):
        return self.db.no_due_requests.find_one({"student_id": sid, "office": office})

    def test_unknown_office_rejected(self):
        r = self._send(self._as_student(self.cse), "PRINCIPAL")
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(self._doc(self.cse, "PRINCIPAL"))

    def test_hostel_not_accepted_on_generic_endpoint(self):
        r = self._send(self._as_student(self.cse), "HOSTEL")
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(self._doc(self.cse, "HOSTEL"))

    def test_day_scholar_cannot_send_hostel(self):
        c = self._as_student(self.day)
        r = c.post("/student/hostel/send/", {"format": "json"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(self._doc(self.day, "HOSTEL"))

    def test_duplicate_send_does_not_burn_an_attempt(self):
        c = self._as_student(self.cse)
        self.assertEqual(self._send(c, "LIBRARY").status_code, 200)
        r = self._send(c, "LIBRARY")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("already_pending"))
        self.assertEqual(self._doc(self.cse, "LIBRARY")["attempts_used"], 0)   # sending never uses an attempt

    def test_approved_request_cannot_be_resent(self):
        c = self._as_student(self.cse)
        self._send(c, "LIBRARY")
        self.db.no_due_requests.update_one({"student_id": self.cse, "office": "LIBRARY"}, {"$set": {"status": "APPROVED"}})
        r = self._send(c, "LIBRARY")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._doc(self.cse, "LIBRARY")["status"], "APPROVED")

    def test_department_cannot_act_on_other_departments_students(self):
        self._send(self._as_student(self.ece), "DEPARTMENT")
        req_id = str(self._doc(self.ece, "DEPARTMENT")["_id"])
        cse_dept = self._client(role="DEPARTMENT", department="CSE")
        cse_dept.post("/approve/", {"request_ids": [req_id]})
        self.assertEqual(self._doc(self.ece, "DEPARTMENT")["status"], "PENDING")
        cse_dept.post("/reject/", {"req_id": req_id, "reason": "x"})
        self.assertEqual(self._doc(self.ece, "DEPARTMENT")["status"], "PENDING")
        ece_dept = self._client(role="DEPARTMENT", department="ECE")
        ece_dept.post("/approve/", {"request_ids": [req_id]})
        self.assertEqual(self._doc(self.ece, "DEPARTMENT")["status"], "APPROVED")

    def test_send_allowed_right_after_cooldown_ends(self):
        from datetime import datetime, timedelta, timezone
        self.db.no_due_requests.insert_one({
            "student_id": self.cse, "office": "COLLEGE", "status": "REJECTED", "attempts_used": 2,
            "cooldown_expiry": datetime.now(timezone.utc) - timedelta(seconds=5), "created_at": datetime.now()})
        r = self._send(self._as_student(self.cse), "COLLEGE")
        self.assertEqual(r.status_code, 200, r.content[:200])
        d = self._doc(self.cse, "COLLEGE")
        self.assertEqual((d["status"], d["attempts_used"]), ("PENDING", 0))
        self.assertNotIn("cooldown_expiry", d)

    def test_send_blocked_during_active_cooldown(self):
        from datetime import datetime, timedelta, timezone
        self.db.no_due_requests.insert_one({
            "student_id": self.cse, "office": "COLLEGE", "status": "REJECTED", "attempts_used": 2,
            "cooldown_expiry": datetime.now(timezone.utc) + timedelta(hours=5), "created_at": datetime.now()})
        r = self._send(self._as_student(self.cse), "COLLEGE")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._doc(self.cse, "COLLEGE")["status"], "REJECTED")

    # ── attempts count office rejections only ──
    def _reject(self, office_role, sid, office):
        req_id = str(self._doc(sid, office)["_id"])
        self._client(role=office_role).post("/reject/", {"req_id": req_id, "reason": "test"})
        return self._doc(sid, office)

    def test_rejections_use_attempts_and_second_starts_cooldown(self):
        c = self._as_student(self.cse)
        self._send(c, "LIBRARY")
        self.assertEqual(self._doc(self.cse, "LIBRARY")["attempts_used"], 0)
        d = self._reject("LIBRARY", self.cse, "LIBRARY")
        self.assertEqual((d["status"], d["attempts_used"]), ("REJECTED", 1))
        self.assertNotIn("cooldown_expiry", d)
        c.post("/student/retry/", {"office": "LIBRARY", "format": "json"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self._send(c, "LIBRARY")
        self.assertEqual(self._doc(self.cse, "LIBRARY")["attempts_used"], 1)   # resend: still 1
        d = self._reject("LIBRARY", self.cse, "LIBRARY")
        self.assertEqual((d["status"], d["attempts_used"]), ("REJECTED", 2))
        self.assertIsNotNone(d.get("cooldown_expiry"))

    def test_expiry_after_two_days_does_not_use_an_attempt(self):
        from datetime import datetime, timedelta
        from authentication.maintenance import PENDING_TTL, _expire_stale_pending
        self.assertEqual(PENDING_TTL, timedelta(days=2))
        c = self._as_student(self.cse)
        self._send(c, "LIBRARY")
        self._reject("LIBRARY", self.cse, "LIBRARY")                        # 1 rejection
        c.post("/student/retry/", {"office": "LIBRARY", "format": "json"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self._send(c, "LIBRARY")
        self.db.no_due_requests.update_one({"student_id": self.cse, "office": "LIBRARY"},
                                           {"$set": {"created_at": datetime.now() - timedelta(days=1, hours=23)}})
        _expire_stale_pending()
        self.assertEqual(self._doc(self.cse, "LIBRARY")["status"], "PENDING")   # < 2 days: untouched
        self.db.no_due_requests.update_one({"student_id": self.cse, "office": "LIBRARY"},
                                           {"$set": {"created_at": datetime.now() - timedelta(days=2, minutes=1)}})
        _expire_stale_pending()
        d = self._doc(self.cse, "LIBRARY")
        self.assertEqual((d["status"], d["attempts_used"]), ("NOT_SENT", 1))   # attempt count unchanged
        self.assertEqual(self._send(c, "LIBRARY").status_code, 200)             # can send again

    def test_old_model_pending_doc_is_not_double_counted(self):
        from datetime import datetime
        # Saved before this change: the send itself was counted (attempts_used=1, no attempts_model).
        self.db.no_due_requests.insert_one({"student_id": self.cse, "office": "COLLEGE", "status": "PENDING",
                                            "attempts_used": 1, "created_at": datetime.now()})
        d = self._reject("COLLEGE", self.cse, "COLLEGE")
        self.assertEqual(d["attempts_used"], 1)          # first rejection, not 2
        self.assertNotIn("cooldown_expiry", d)

    def test_cooldown_sweep_uses_utc_not_local_time(self):
        from datetime import datetime, timedelta, timezone
        from authentication.maintenance import fast_cooldown_reset
        self.db.no_due_requests.insert_one({
            "student_id": self.cse, "office": "LIBRARY", "status": "REJECTED", "attempts_used": 2,
            "cooldown_expiry": datetime.now(timezone.utc) + timedelta(hours=3), "created_at": datetime.now()})
        fast_cooldown_reset(force=True)
        d = self._doc(self.cse, "LIBRARY")
        self.assertEqual((d["status"], d["attempts_used"]), ("REJECTED", 2))   # 3 h left: still locked
        self.assertIn("cooldown_expiry", d)
        self.db.no_due_requests.update_one({"_id": d["_id"]}, {"$set": {
            "cooldown_expiry": datetime.now(timezone.utc) - timedelta(seconds=1)}})
        fast_cooldown_reset(force=True)
        d = self._doc(self.cse, "LIBRARY")
        self.assertEqual((d["status"], d["attempts_used"]), ("NOT_SENT", 0))


class PromotionTests(TestCase):
    """Semester promotion engine (authentication/promotion.py) and the promote view."""

    PREFIX = "99998820"

    def setUp(self):
        from datetime import datetime
        from authentication.mongo import db
        self.db = db
        self._t0 = datetime.now()
        self._cleanup()
        # Promotion acts on EVERY student, so only run against a database holding test data alone.
        if self.db.students.count_documents({}) > 0:
            self.skipTest("local MongoDB has non-test students; promotion tests need an empty students collection")
        self.db.portal_settings.update_one({"_id": "global_config"}, {"$set": {"promotion_in_progress": False},
                                           "$unset": {"promotion_lock_until": ""}}, upsert=True)
        self.n = 0
        from authentication import promotion
        self._grace = promotion.WRITE_GRACE_SECONDS
        promotion.WRITE_GRACE_SECONDS = 0

    def tearDown(self):
        from authentication import promotion
        promotion.WRITE_GRACE_SECONDS = getattr(self, "_grace", promotion.WRITE_GRACE_SECONDS)
        self._cleanup()
        self.db.portal_settings.update_one({"_id": "global_config"}, {"$set": {"promotion_in_progress": False},
                                           "$unset": {"promotion_lock_until": ""}})

    def _cleanup(self):
        ids = [s["_id"] for s in self.db.students.find({"reg_no": {"$regex": f"^{self.PREFIX}"}}, {"_id": 1})]
        self.db.no_due_requests.delete_many({"student_id": {"$in": ids}})
        self.db.promotion_logs.delete_many({"student_id": {"$in": ids}})
        self.db.students.delete_many({"_id": {"$in": ids}})
        run_ids = self.db.promotion_run_items.distinct("run_id", {"student_id": {"$in": ids}}) if ids else []
        self.db.promotion_run_items.delete_many({"student_id": {"$in": ids}})
        self.db.promotion_runs.delete_many({"_id": {"$in": run_ids}})
        # runs created by this class's tests (students may already be gone)
        t0 = getattr(self, "_t0", None)
        if t0 is not None:
            stale = self.db.promotion_runs.distinct("_id", {"started_at": {"$gte": t0}})
            self.db.promotion_run_items.delete_many({"run_id": {"$in": stale}})
            self.db.promotion_runs.delete_many({"_id": {"$in": stale}})
        self.db.archived_students.delete_many({"student.reg_no": {"$regex": f"^{self.PREFIX}"}})

    def _add(self, sem, year=None, stype="Day Scholar", approved=(), branch="CSE"):
        self.n += 1
        sid = self.db.students.insert_one({
            "reg_no": f"{self.PREFIX}{self.n:04d}", "name": "PROMO TEST", "branch": branch,
            "semester": sem, "year": year if year is not None else (sem + 1) // 2,
            "student_type": stype, "is_75_scheme": False, "profile_completed": True}).inserted_id
        for office in approved:
            self.db.no_due_requests.insert_one({"student_id": sid, "office": office, "status": "APPROVED",
                                                "attempts_used": 0, "attempts_model": 2})
        return sid

    def _sem(self, sid):
        d = self.db.students.find_one({"_id": sid})
        return d["semester"], d["year"]

    def _faculty(self):
        c = Client()
        s = c.session
        s["role"] = "FACULTY"
        s["promotion_unlocked"] = True
        s.save()
        from django.conf import settings
        c.cookies[settings.SESSION_COOKIE_NAME] = s.session_key
        return c

    # ── the reported bug ──
    def test_semester_wise_form_promotes_only_that_semester(self):
        a, b, c3 = self._add(1), self._add(3), self._add(5)
        r = self._faculty().post("/faculty/promote/", {"promotion_mode": "semester", "from_sem": "3"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._sem(a), (1, 1))
        self.assertEqual(self._sem(b), (4, 2))
        self.assertEqual(self._sem(c3), (5, 3))

    def test_semester_mode_without_semester_does_not_fall_back_to_full(self):
        a = self._add(1)
        self._faculty().post("/faculty/promote/", {"promotion_mode": "semester", "from_sem": ""})
        self.assertEqual(self._sem(a), (1, 1))

    # ── rules ──
    def test_semester_wise_blocked_when_target_has_students(self):
        from authentication.promotion import run_promotion
        a, b = self._add(3), self._add(4)
        r = run_promotion(from_sem=3)
        self.assertFalse(r["ok"])
        self.assertIn("merge", r["message"])
        self.assertEqual((self._sem(a), self._sem(b)), ((3, 2), (4, 2)))

    def test_new_academic_year_needs_semester_8_removed(self):
        from authentication.promotion import run_promotion
        s2, s8 = self._add(2), self._add(8)
        self.assertFalse(run_promotion(from_sem=2)["ok"])        # 2→3 starts a new year
        self.assertEqual(self._sem(s2), (2, 1))
        s5 = self._add(5)
        self.assertTrue(run_promotion(from_sem=5)["ok"])         # 5→6 same year: allowed
        self.assertEqual(self._sem(s5), (6, 3))
        self.assertFalse(run_promotion()["ok"])                  # full promotion blocked by Sem 8
        self.assertEqual(self._sem(s8), (8, 4))

    def test_full_promotion_moves_every_semester_exactly_once(self):
        from authentication.promotion import run_promotion
        # Valid data: one semester per Department + Year (CSE odd semesters, ECE even ones).
        ids = {sem: self._add(sem, branch="CSE" if sem % 2 else "ECE") for sem in range(1, 8)}
        r = run_promotion()
        self.assertTrue(r["ok"], r["message"])
        self.assertEqual(r["promoted"], 7)
        for sem, sid in ids.items():
            self.assertEqual(self._sem(sid), (sem + 1, (sem + 2) // 2))

    def test_year_is_derived_from_semester(self):
        from authentication.promotion import run_promotion
        sid = self._add(4, year=1)          # wrong stored year
        run_promotion(from_sem=4)
        self.assertEqual(self._sem(sid), (5, 3))

    def test_no_dues_reset_only_for_promoted_and_history_logged(self):
        from authentication.promotion import run_promotion
        moved = self._add(3, approved=("LIBRARY", "COLLEGE", "DEPARTMENT"))
        stay = self._add(1, approved=("LIBRARY",))
        run_promotion(from_sem=3)
        self.assertEqual(self.db.no_due_requests.count_documents({"student_id": moved, "status": "NOT_SENT"}), 3)
        self.assertEqual(self.db.no_due_requests.count_documents({"student_id": stay, "status": "APPROVED"}), 1)
        log = self.db.promotion_logs.find_one({"student_id": moved})
        self.assertTrue(log and log["no_due_cleared"])
        self.assertEqual((log["previous_semester"], log["new_semester"]), (3, 4))

    def test_running_twice_does_not_double_promote(self):
        from authentication.promotion import run_promotion
        sid = self._add(3)
        self.assertTrue(run_promotion(from_sem=3)["ok"])
        self.assertFalse(run_promotion(from_sem=3)["ok"])
        self.assertEqual(self._sem(sid), (4, 2))

    def test_stale_page_snapshot_blocks_promotion(self):
        from authentication.promotion import counts_snapshot, run_promotion
        a = self._add(1)
        snap = counts_snapshot()
        self._add(1)                         # roster changed after the page loaded
        r = run_promotion(expected_snapshot=snap)
        self.assertFalse(r["ok"])
        self.assertEqual(self._sem(a), (1, 1))

    def test_view_rejects_stale_snapshot(self):
        a = self._add(1)
        self._faculty().post("/faculty/promote/", {"promotion_mode": "full", "counts_snapshot": "CSE:1:9"})
        self.assertEqual(self._sem(a), (1, 1))

    def test_active_lock_blocks_but_stale_lock_is_recovered(self):
        from datetime import datetime, timedelta, timezone
        from authentication.promotion import run_promotion
        sid = self._add(1)
        self.db.portal_settings.update_one({"_id": "global_config"}, {"$set": {
            "promotion_in_progress": True, "promotion_lock_until": datetime.now(timezone.utc) + timedelta(minutes=5)}})
        self.assertFalse(run_promotion()["ok"])
        self.assertEqual(self._sem(sid), (1, 1))
        self.db.portal_settings.update_one({"_id": "global_config"}, {"$set": {
            "promotion_lock_until": datetime.now(timezone.utc) - timedelta(minutes=1)}})
        self.assertTrue(run_promotion()["ok"])
        self.assertEqual(self._sem(sid), (2, 1))
        cfg = self.db.portal_settings.find_one({"_id": "global_config"})
        self.assertFalse(cfg.get("promotion_in_progress"))

    def test_promotion_requires_unlocked_faculty(self):
        a = self._add(1)
        c = Client()
        s = c.session
        s["role"] = "FACULTY"
        s.save()
        from django.conf import settings
        c.cookies[settings.SESSION_COOKIE_NAME] = s.session_key
        c.post("/faculty/promote/", {"promotion_mode": "full"})
        self.assertEqual(self._sem(a), (1, 1))

    # ── one semester per Department + Year: promotion ──
    def test_other_departments_do_not_block_semester_wise(self):
        from authentication.promotion import run_promotion
        cse3, ece4 = self._add(3, branch="CSE"), self._add(4, branch="ECE")
        r = run_promotion(from_sem=3)
        self.assertTrue(r["ok"], r["message"])
        self.assertEqual(self._sem(cse3), (4, 2))
        self.assertEqual(self._sem(ece4), (4, 2))

    def test_year_boundary_merge_blocked(self):
        from authentication.promotion import run_promotion
        y1, y2 = self._add(2), self._add(4)          # CSE Year 1 (Sem 2) and CSE Year 2 (Sem 4)
        r = run_promotion(from_sem=2)                # Sem 2 → 3 would join Year 2, which has Sem 4
        self.assertFalse(r["ok"])
        self.assertIn("merge", r["message"])
        self.assertEqual((self._sem(y1), self._sem(y2)), ((2, 1), (4, 2)))

    def test_full_promotion_blocked_when_it_would_mix_semesters(self):
        from authentication.promotion import run_promotion
        a, b = self._add(2), self._add(3)            # CSE Year 1 Sem 2, CSE Year 2 Sem 3
        r = run_promotion()                          # → CSE Year 2 would hold Sem 3 and Sem 4
        self.assertFalse(r["ok"])
        self.assertIn("would end up with students in", r["message"])
        self.assertEqual((self._sem(a), self._sem(b)), ((2, 1), (3, 2)))

    def test_promotion_checks_match_engine(self):
        from authentication.promotion import promotion_checks
        self._add(3, branch="CSE")
        self._add(4, branch="CSE")
        checks = promotion_checks()
        self.assertFalse(checks["3"]["ok"])
        self.assertTrue(checks["full"]["ok"])

    # ── undo / atomicity ──
    def test_undo_restores_semester_year_dues_and_history(self):
        from authentication.promotion import run_promotion, rollback_last_run
        sid = self._add(3, approved=("LIBRARY", "COLLEGE", "DEPARTMENT"))
        r = run_promotion(from_sem=3)
        self.assertTrue(r["ok"])
        self.assertEqual(self._sem(sid), (4, 2))
        self.assertEqual(self.db.no_due_requests.count_documents({"student_id": sid, "status": "APPROVED"}), 0)
        u = rollback_last_run(r["run_id"])
        self.assertTrue(u["ok"], u["message"])
        self.assertEqual(self._sem(sid), (3, 2))
        self.assertEqual(self.db.no_due_requests.count_documents({"student_id": sid, "status": "APPROVED"}), 3)
        self.assertEqual(self.db.promotion_logs.count_documents({"student_id": sid}), 0)
        self.assertEqual(self.db.promotion_runs.find_one({"_id": r["run_id"]})["status"], "rolled_back")

    def test_only_latest_promotion_can_be_undone(self):
        from authentication.promotion import run_promotion, rollback_last_run
        sid = self._add(1)
        first = run_promotion(from_sem=1)
        second = run_promotion(from_sem=2)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertFalse(rollback_last_run(first["run_id"])["ok"])
        self.assertTrue(rollback_last_run(second["run_id"])["ok"])
        self.assertEqual(self._sem(sid), (2, 1))
        self.assertTrue(rollback_last_run(first["run_id"])["ok"])   # now it is the latest
        self.assertEqual(self._sem(sid), (1, 1))

    def test_undo_skips_students_changed_after_promotion(self):
        from authentication.promotion import run_promotion, rollback_last_run
        a, b = self._add(3), self._add(3)
        r = run_promotion(from_sem=3)
        self.db.students.update_one({"_id": b}, {"$set": {"semester": 6, "year": 3}})   # edited afterwards
        u = rollback_last_run(r["run_id"])
        self.assertEqual((u["restored"], u["skipped"]), (1, 1))
        self.assertEqual(self._sem(a), (3, 2))
        self.assertEqual(self._sem(b), (6, 3))

    def test_failure_mid_promotion_is_undone_automatically(self):
        from unittest import mock
        from authentication import promotion
        ids = [self._add(sem, branch="CSE" if sem % 2 else "ECE") for sem in (1, 2, 3, 4, 5)]
        before = [self._sem(i) for i in ids]
        real_apply = promotion._apply

        def crash(items, semesters, now, run_id, session=None):
            real_apply(items, semesters[:2], now, run_id, session=session)   # half the work…
            raise RuntimeError("simulated crash")

        with mock.patch.object(promotion, "_apply", side_effect=crash), \
             mock.patch.object(promotion, "transactions_supported", return_value=False):
            r = promotion.run_promotion()
        self.assertFalse(r["ok"])
        self.assertEqual([self._sem(i) for i in ids], before)          # …all undone
        self.assertFalse(self.db.portal_settings.find_one({"_id": "global_config"}).get("promotion_in_progress"))

    def test_concurrent_promotions_only_one_runs(self):
        import threading
        from authentication.promotion import run_promotion
        sid = self._add(1)
        results = []
        barrier = threading.Barrier(4)

        def go():
            barrier.wait()
            results.append(run_promotion(from_sem=1))
        threads = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sum(1 for r in results if r["ok"]), 1)
        self.assertEqual(self._sem(sid), (2, 1))

    def test_undo_view_requires_confirmation(self):
        from authentication.promotion import run_promotion
        sid = self._add(3)
        r = run_promotion(from_sem=3)
        c = self._faculty()
        c.post("/faculty/promotion/undo/", {"run_id": str(r["run_id"]), "confirm_text": "no"})
        self.assertEqual(self._sem(sid), (4, 2))
        c.post("/faculty/promotion/undo/", {"run_id": str(r["run_id"]), "confirm_text": "UNDO"})
        self.assertEqual(self._sem(sid), (3, 2))

    # ── one semester per Department + Year: roster paths ──
    def test_add_and_bulk_add_must_match_active_semester(self):
        self._add(3)                                  # CSE Year 2 is in Semester 3
        c = self._faculty()
        c.post("/faculty/add-student/", {"reg_no": f"{self.PREFIX}9001", "branch": "CSE", "year": "2", "semester": "4"})
        self.assertIsNone(self.db.students.find_one({"reg_no": f"{self.PREFIX}9001"}))
        c.post("/faculty/add-student/", {"reg_no": f"{self.PREFIX}9002", "branch": "CSE", "year": "2", "semester": "3"})
        self.assertIsNotNone(self.db.students.find_one({"reg_no": f"{self.PREFIX}9002"}))
        c.post("/faculty/bulk-add/", {"start_reg_no": f"{self.PREFIX}9010", "end_reg_no": f"{self.PREFIX}9012",
                                      "branch": "CSE", "year": "2", "semester": "4"})
        self.assertEqual(self.db.students.count_documents({"reg_no": {"$regex": f"^{self.PREFIX}901"}}), 0)
        c.post("/faculty/add-student/", {"reg_no": f"{self.PREFIX}9003", "branch": "ECE", "year": "2", "semester": "4"})
        self.assertIsNotNone(self.db.students.find_one({"reg_no": f"{self.PREFIX}9003"}))   # other department: fine

    def test_edit_cannot_mix_semesters_and_validates_pair(self):
        a, b = self._add(3), self._add(3)
        c = self._faculty()
        base = {"student_id": str(b), "reg_no": f"{self.PREFIX}0002", "name": "X", "branch": "CSE", "student_type": "Day Scholar"}
        c.post("/faculty/edit/", {**base, "year": "2", "semester": "4"})       # would mix CSE Year 2
        self.assertEqual(self._sem(b), (3, 2))
        c.post("/faculty/edit/", {**base, "year": "2", "semester": "7"})       # invalid pair
        self.assertEqual(self._sem(b), (3, 2))
        c.post("/faculty/edit/", {**base, "branch": "ECE", "year": "2", "semester": "4"})   # move to ECE: ok
        d = self.db.students.find_one({"_id": b})
        self.assertEqual((d["branch"], d["semester"], d["year"]), ("ECE", 4, 2))

    def test_roster_changes_refused_while_promotion_runs(self):
        from datetime import datetime, timedelta, timezone
        self.db.portal_settings.update_one({"_id": "global_config"}, {"$set": {
            "promotion_in_progress": True, "promotion_lock_until": datetime.now(timezone.utc) + timedelta(minutes=5)}})
        self._faculty().post("/faculty/add-student/", {"reg_no": f"{self.PREFIX}9100", "branch": "CSE", "year": "1", "semester": "1"})
        self.assertIsNone(self.db.students.find_one({"reg_no": f"{self.PREFIX}9100"}))

    def test_semester8_removal_and_delete_archive_students(self):
        s8 = self._add(8)
        other = self._add(1)
        c = self._faculty()
        c.post("/faculty/remove-sem8/")
        self.assertIsNone(self.db.students.find_one({"_id": s8}))
        self.assertIsNotNone(self.db.archived_students.find_one({"student._id": s8, "reason": "semester_8_removal"}))
        c.post("/faculty/delete/", {"student_ids": [str(other)], "branch": "CSE", "year": "1"})
        self.assertIsNone(self.db.students.find_one({"_id": other}))
        self.assertIsNotNone(self.db.archived_students.find_one({"student._id": other, "reason": "deleted_by_faculty"}))
        self.db.archived_students.delete_many({"student._id": {"$in": [s8, other]}})

    # ── risk 1: process killed mid-promotion → automatic recovery ──
    def test_killed_mid_promotion_is_recovered_automatically(self):
        from unittest import mock
        from authentication import promotion
        from authentication.maintenance import run_maintenance_cycle
        ids = [self._add(sem, branch="CSE" if sem % 2 else "ECE") for sem in (1, 2, 3, 4, 5)]
        before = [self._sem(i) for i in ids]
        real_apply = promotion._apply

        class Killed(BaseException):
            """Like SIGKILL: bypasses every `except Exception`."""

        def killed(items, semesters, now, run_id, session=None):
            real_apply(items, [x for x in semesters if x in (5, 4)], now, run_id, session=session)
            raise Killed()

        with mock.patch.object(promotion, "_apply", side_effect=killed), \
             mock.patch.object(promotion, "transactions_supported", return_value=False):
            with self.assertRaises(Killed):
                promotion.run_promotion()
        self.assertNotEqual([self._sem(i) for i in ids], before)                 # half-promoted…
        self.assertEqual(self.db.promotion_runs.count_documents({"status": "in_progress"}), 1)
        self.db.portal_settings.delete_one({"_id": "maintenance_lock"})
        run_maintenance_cycle()                                                   # …next maintenance tick
        self.assertEqual([self._sem(i) for i in ids], before)                    # …fully undone
        self.assertEqual(self.db.promotion_runs.count_documents({"status": "auto_recovered"}), 1)

    def test_live_promotion_is_not_touched_by_recovery(self):
        from authentication.promotion import acquire_lock, release_lock, recover_interrupted_runs
        from bson import ObjectId
        from datetime import datetime
        self.db.promotion_runs.insert_one({"_id": ObjectId(), "status": "in_progress", "started_at": datetime.now()})
        self.assertTrue(acquire_lock())                       # a promotion is genuinely running
        try:
            self.assertEqual(recover_interrupted_runs(), 0)
            self.assertEqual(self.db.promotion_runs.count_documents({"status": "in_progress"}), 1)
        finally:
            release_lock()

    # ── risk 2: roster write slips into a promotion → promotion undoes itself ──
    def test_concurrent_write_during_promotion_undoes_it(self):
        from unittest import mock
        from authentication import promotion
        a = self._add(3)
        real_apply = promotion._apply

        def apply_then_sneak_in(*args, **kwargs):
            out = real_apply(*args, **kwargs)
            self._add(3)                                      # someone adds a Semester 3 student meanwhile
            return out

        with mock.patch.object(promotion, "_apply", side_effect=apply_then_sneak_in):
            r = promotion.run_promotion(from_sem=3)
        self.assertFalse(r["ok"])
        self.assertIn("undone automatically", r["message"])
        self.assertEqual(self._sem(a), (3, 2))
        self.assertEqual(self.db.promotion_runs.count_documents({"status": "auto_undone"}), 1)

    def test_concurrent_adds_with_different_semesters_never_mix_a_class(self):
        import threading
        c1, c2 = self._faculty(), self._faculty()
        barrier = threading.Barrier(2)

        def add(client, suffix, sem):
            barrier.wait()
            client.post("/faculty/add-student/", {"reg_no": f"{self.PREFIX}95{suffix}", "branch": "CSE",
                                                  "year": "2", "semester": sem})
        for round_no in range(5):
            self._cleanup()
            t1 = threading.Thread(target=add, args=(c1, f"{round_no}1", "3"))
            t2 = threading.Thread(target=add, args=(c2, f"{round_no}2", "4"))
            t1.start(); t2.start(); t1.join(); t2.join()
            sems = set(self.db.students.distinct("semester", {"branch": "CSE", "reg_no": {"$regex": f"^{self.PREFIX}"}}))
            self.assertLessEqual(len(sems), 1, f"round {round_no}: CSE Year 2 has {sems}")

    # ── risk 4: undo never discards new-semester requests ──
    def test_undo_refused_after_students_send_requests(self):
        from datetime import datetime, timedelta
        from authentication.promotion import rollback_last_run, run_promotion
        sid = self._add(3)
        r = run_promotion(from_sem=3)
        self.db.no_due_requests.update_one({"student_id": sid, "office": "LIBRARY"}, {"$set": {
            "status": "PENDING", "created_at": datetime.now() + timedelta(seconds=1)}}, upsert=True)
        u = rollback_last_run(r["run_id"])
        self.assertFalse(u["ok"])
        self.assertIn("no longer possible", u["message"])
        self.assertEqual(self._sem(sid), (4, 2))

    # ── risk 3: repair a mixed class (undoable) ──
    def test_fix_mixed_class_and_undo(self):
        from authentication.promotion import cohort_issues, fix_cohort, rollback_last_run
        a, b, c = self._add(3), self._add(3), self._add(4)
        self.assertEqual(len(cohort_issues()), 1)
        r = fix_cohort("CSE", 2, 3)
        self.assertTrue(r["ok"], r["message"])
        self.assertEqual([self._sem(x) for x in (a, b, c)], [(3, 2)] * 3)
        self.assertEqual(cohort_issues(), [])
        self.assertTrue(rollback_last_run(r["run_id"])["ok"])
        self.assertEqual(self._sem(c), (4, 2))

    def test_fix_view(self):
        a, b = self._add(3), self._add(4)
        self._faculty().post("/faculty/promotion/fix-class/", {"branch": "CSE", "year": "2", "semester": "4"})
        self.assertEqual([self._sem(a), self._sem(b)], [(4, 2), (4, 2)])

    # ── risk 5: archive restore ──
    def test_restore_archived_student(self):
        sid = self._add(1, approved=("LIBRARY",))
        c = self._faculty()
        c.post("/faculty/delete/", {"student_ids": [str(sid)], "branch": "CSE", "year": "1"})
        self.assertIsNone(self.db.students.find_one({"_id": sid}))
        arch = self.db.archived_students.find_one({"student._id": sid})
        c.post("/faculty/promotion/restore/", {"archive_ids": [str(arch["_id"])]})
        self.assertEqual(self._sem(sid), (1, 1))
        self.assertEqual(self.db.no_due_requests.count_documents({"student_id": sid, "status": "APPROVED"}), 1)
        self.assertIsNone(self.db.archived_students.find_one({"_id": arch["_id"]}))

    def test_restore_skips_conflicts(self):
        from authentication.promotion import archive_students, restore_archived
        sid = self._add(1)
        reg = self.db.students.find_one({"_id": sid})["reg_no"]
        archive_students([sid], reason="deleted_by_faculty")
        self.db.students.delete_one({"_id": sid})
        self.db.students.insert_one({"reg_no": reg, "branch": "CSE", "semester": 1, "year": 1})   # reg reused
        arch = self.db.archived_students.find_one({"student._id": sid})
        r = restore_archived([str(arch["_id"])])
        self.assertEqual(r["restored"], 0)
        self.assertIn("already exists", r["message"])
        self.assertIsNotNone(self.db.archived_students.find_one({"_id": arch["_id"]}))   # kept for later

    # ── risk 6: per-department semester-wise promotion ──
    def test_semester_wise_for_one_department(self):
        from authentication.promotion import run_promotion
        cse, ece = self._add(3, branch="CSE"), self._add(3, branch="ECE")
        r = run_promotion(from_sem=3, branch="CSE")
        self.assertTrue(r["ok"], r["message"])
        self.assertEqual(self._sem(cse), (4, 2))
        self.assertEqual(self._sem(ece), (3, 2))

    def test_department_sem8_rule_is_per_department(self):
        from authentication.promotion import run_promotion
        cse2, ece8 = self._add(2, branch="CSE"), self._add(8, branch="ECE")
        self.assertFalse(run_promotion(from_sem=2)["ok"])                  # all departments: Sem 8 blocks
        self.assertEqual(self._sem(cse2), (2, 1))
        self.assertTrue(run_promotion(from_sem=2, branch="CSE")["ok"])   # CSE only: ECE's Sem 8 doesn't matter
        self.assertEqual(self._sem(cse2), (3, 2))

    def test_department_promotion_via_view_and_requires_semester(self):
        cse, ece = self._add(5, branch="CSE"), self._add(5, branch="ECE")
        c = self._faculty()
        c.post("/faculty/promote/", {"promotion_mode": "semester", "from_sem": "", "promote_branch": "CSE"})
        self.assertEqual(self._sem(cse), (5, 3))
        c.post("/faculty/promote/", {"promotion_mode": "semester", "from_sem": "5", "promote_branch": "CSE"})
        self.assertEqual((self._sem(cse), self._sem(ece)), ((6, 3), (5, 3)))
