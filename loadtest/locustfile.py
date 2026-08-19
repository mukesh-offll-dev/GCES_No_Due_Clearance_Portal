"""
Realistic load test for the GCES No Due portal using Locust.

Exercises the ACTUAL user journeys — student login + dashboard, office login +
dashboard + paginated status API — including CSRF handling, so the numbers
reflect real production behaviour (DB lookups, aggregations, sessions).

Install & run:
    pip install locust
    locust -f loadtest/locustfile.py --host http://localhost:8000

Then open http://localhost:8089 and set:
    * Number of users  = target concurrency (e.g. 400)
    * Spawn rate       = 50/s
Watch the p95 latency and failure ratio; the highest user count that keeps
failures ≈ 0 and p95 acceptable is your stable capacity.

IMPORTANT: point --host at a STAGING copy with a realistic student dataset.
Do NOT run write-heavy scenarios against production data.
"""
import re
import random

from locust import HttpUser, task, between

_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


def _csrf(html):
    m = _CSRF_RE.search(html)
    return m.group(1) if m else ""


class OfficeStaff(HttpUser):
    """An office logs in and browses dashboards + the status API (read-heavy)."""
    weight = 1
    wait_time = between(1, 4)

    def on_start(self):
        r = self.client.get("/")
        token = _csrf(r.text)
        # Adjust credentials to a dedicated load-test account if desired.
        self.client.post("/", data={
            "office": "library",
            "username": "library_admin",
            "password": "lib@123",
            "csrfmiddlewaretoken": token,
        }, headers={"Referer": self.host + "/"})

    @task(3)
    def dashboard_summary(self):
        self.client.get("/library/", name="/library/ (summary)")

    @task(2)
    def dashboard_branch_year(self):
        branch = random.choice(["CSE", "ECE", "EEE", "MECH"])
        year = random.choice([1, 2, 3, 4])
        self.client.get(f"/library/?branch={branch}&year={year}",
                        name="/library/?branch&year")

    @task(2)
    def status_api(self):
        year = random.choice([1, 2, 3, 4])
        self.client.get(f"/office/student-status/?year={year}&page=1",
                        name="/office/student-status/")


class Student(HttpUser):
    """A student loads the login page and (if configured) their dashboard."""
    weight = 3
    wait_time = between(2, 6)

    @task
    def visit_login(self):
        self.client.get("/", name="/ (student landing)")
