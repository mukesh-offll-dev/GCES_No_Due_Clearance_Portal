"""
🎓 Semester promotion and cohort consistency (Faculty Advisor → Student Promotion).

THE RULE: one active semester per Department + Year.
  Year y covers Semesters 2y-1 and 2y. Inside one Department + Year every student
  must be in the SAME semester (e.g. CSE Year 2 all in Semester 3). Odd and even
  semesters never mix inside one year. `check_cohort_semester()` is checked BEFORE
  and `verify_cohort_after_write()` AFTER every roster write (Add Student, Bulk Add,
  Edit, the student's own profile update, Excel import, archive restore), so two
  concurrent writes can never leave a class with two semesters.

PROMOTION MODES
  • Full promotion  — every student in Semesters 1–7 moves up one semester.
  • Semester-wise   — only students in ONE chosen semester, in all departments or
                      in one chosen department.

SERVER-SIDE RULES (the page only displays `promotion_checks()`)
  1. Semester 8 must be empty for a full promotion and for any semester-wise
     promotion that starts a new academic year (2→3, 4→5, 6→7) — for a single
     department, only that department's Semester 8 counts.
  2. Per department the promotion is simulated: a batch may not move into a
     Department + Year that already holds students who are not moving (batch
     merge), and no Department + Year may end up with two semesters (mixing).
  3. The page posts a snapshot of the Department × Semester counts it showed; if
     the roster changed since, nothing happens (stale page / double submit).
  4. One promotion at a time: a lock in portal_settings with a 10-minute expiry
     that the running promotion keeps extending (heartbeat). Roster writes are
     refused while it is held, and promotion waits a short grace period after
     taking it so that any write already in flight finishes first.

ATOMICITY AND RECOVERY
  • A journal is written before anything changes: `promotion_runs` (one doc per
    run) and `promotion_run_items` (one per student: old/new semester and year,
    no-due documents and history row as they were).
  • Replica set (MongoDB Atlas): all writes run in ONE transaction.
  • Standalone MongoDB: writes run in a safe order; if anything raises, the journal
    is replayed backwards at once. If the process is KILLED mid-run, the run stays
    "in_progress"; `recover_interrupted_runs()` (maintenance cycle every minute and
    the start of every promotion) undoes it automatically once its lock expired.
  • After a run, if a class that was consistent now has two semesters (a write
    slipped in), the run is undone automatically and the faculty is told why.
  • `rollback_last_run()` undoes the most recent run — but only while none of its
    students has sent a request for the new semester, so no request is ever lost.
  • `fix_cohort()` repairs a class with mixed semesters (journalled, undoable).
  • Deleted / Semester 8 students are archived and can be restored.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo import InsertOne
from pymongo.errors import DuplicateKeyError, PyMongoError

from .mongo import client, db, no_due_col, portal_settings, promotion_logs, students_col

logger = logging.getLogger("nodue")
audit_logger = logging.getLogger("nodue.audit")

promotion_runs = db["promotion_runs"]
promotion_run_items = db["promotion_run_items"]
archived_students = db["archived_students"]

LOCK_TTL = timedelta(minutes=10)
WRITE_GRACE_SECONDS = 1.5          # let roster writes that passed their lock check finish
PROMOTABLE_SEMESTERS = (1, 2, 3, 4, 5, 6, 7)
NEW_YEAR_FROM = (2, 4, 6)          # promoting from these starts a new academic year
UNDOABLE_STATUSES = ["completed", "interrupted", "in_progress"]


# ───────────────────────── basics ─────────────────────────
def year_for_semester(sem):
    return (int(sem) + 1) // 2


def semesters_of_year(year):
    year = int(year)
    return (2 * year - 1, 2 * year)


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _semester_filter(sem):
    # Match int and numeric-string storage of the same semester.
    return {"$in": [sem, str(sem)]}


def branch_semester_counts():
    """{(branch, semester): count} for every student with a usable semester."""
    out = {}
    for row in students_col.aggregate([
        {"$group": {"_id": {"b": "$branch", "s": "$semester"}, "n": {"$sum": 1}}},
    ]):
        sem = _int_or_none(row["_id"].get("s"))
        branch = row["_id"].get("b") or ""
        if sem is None or not 1 <= sem <= 8:
            continue
        out[(branch, sem)] = out.get((branch, sem), 0) + row["n"]
    return out


def semester_counts(groups=None, branch=None):
    """{1: n, …, 8: n} — students per semester (all departments, or one)."""
    groups = branch_semester_counts() if groups is None else groups
    counts = {s: 0 for s in range(1, 9)}
    for (b, sem), n in groups.items():
        if branch is None or b == branch:
            counts[sem] += n
    return counts


def counts_snapshot(groups=None):
    """Order-stable fingerprint of the Department × Semester counts the page showed."""
    groups = branch_semester_counts() if groups is None else groups
    return ";".join(f"{b}:{s}:{n}" for (b, s), n in sorted(groups.items()) if n)


# ───────────────────────── lock ─────────────────────────
def promotion_running():
    cfg = portal_settings.find_one({"_id": "global_config"}, {"promotion_in_progress": 1, "promotion_lock_until": 1}) or {}
    if not cfg.get("promotion_in_progress"):
        return False
    until = cfg.get("promotion_lock_until")
    if until is None:
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


def acquire_lock():
    now = datetime.now(timezone.utc)
    portal_settings.update_one({"_id": "global_config"},
                               {"$setOnInsert": {"promotion_in_progress": False}}, upsert=True)
    return portal_settings.find_one_and_update(
        {"_id": "global_config", "$or": [
            {"promotion_in_progress": {"$ne": True}},
            {"promotion_lock_until": {"$lte": now}},          # stale lock from a crashed run
        ]},
        {"$set": {"promotion_in_progress": True, "promotion_lock_until": now + LOCK_TTL}},
    ) is not None


def extend_lock():
    """Heartbeat: a long run keeps its lock alive so it is never mistaken for a crashed one."""
    portal_settings.update_one({"_id": "global_config", "promotion_in_progress": True},
                               {"$set": {"promotion_lock_until": datetime.now(timezone.utc) + LOCK_TTL}})


def release_lock():
    portal_settings.update_one({"_id": "global_config"},
                               {"$set": {"promotion_in_progress": False},
                                "$unset": {"promotion_lock_until": ""}})


# ───────────────────────── cohort rule (roster paths) ─────────────────────────
def cohort_semesters(branch, year, exclude_ids=()):
    """{semester: count} of students already in this Department + Year (by semester or stored year)."""
    year = int(year)
    pair = semesters_of_year(year)
    query = {"branch": branch, "$or": [
        {"semester": {"$in": [*pair, *(str(s) for s in pair)]}},
        {"year": {"$in": [year, str(year)]}},
    ]}
    if exclude_ids:
        query["_id"] = {"$nin": list(exclude_ids)}
    out = {}
    for row in students_col.aggregate([{"$match": query}, {"$group": {"_id": "$semester", "n": {"$sum": 1}}}]):
        sem = _int_or_none(row["_id"])
        if sem is not None:
            out[sem] = out.get(sem, 0) + row["n"]
    return out


def check_cohort_semester(branch, year, semester, exclude_ids=()):
    """None if `semester` may be used in Department + Year, otherwise a message to show."""
    if promotion_running():
        return "A semester promotion is running right now. Please try again in a minute."
    year, semester = int(year), int(semester)
    if semester not in semesters_of_year(year):
        valid = " or ".join(str(s) for s in semesters_of_year(year))
        return f"Semester for Year {year} must be {valid}."
    existing = cohort_semesters(branch, year, exclude_ids)
    others = {s: n for s, n in existing.items() if s != semester}
    if not others:
        return None
    if len(existing) > 1:
        detail = " and ".join(f"Semester {s} ({n})" for s, n in sorted(existing.items()))
        return (f"{branch} Year {year} already has students in {detail}. Fix that first on the Promotion "
                f"page so the class has one semester, then try again.")
    (active, n), = others.items()
    return (f"{branch} Year {year} is currently in Semester {active} ({n} student{'s' if n != 1 else ''}). "
            f"Students in {branch} Year {year} must all be in the same semester, so use Semester {active}.")


def verify_cohort_after_write(branch, year):
    """
    Second check AFTER a roster write. Returns a message if the write must be undone:
    the class now has two semesters (a concurrent write slipped in) or a promotion
    started meanwhile. The caller reverses only its own change.
    """
    if promotion_running():
        return "A semester promotion started while saving. Your change was not kept — please try again."
    sems = cohort_semesters(branch, int(year))
    if len(sems) > 1:
        detail = " and ".join(f"Semester {s}" for s in sorted(sems))
        return (f"Someone else changed {branch} Year {year} at the same moment (it would have {detail}). "
                f"Your change was not kept — reload and try again.")
    return None


def cohort_issues(groups=None):
    """Department + Year groups that already hold more than one semester (bad existing data)."""
    groups = branch_semester_counts() if groups is None else groups
    by_cohort = {}
    for (branch, sem), n in groups.items():
        by_cohort.setdefault((branch, year_for_semester(sem)), {})[sem] = n
    issues = []
    for (branch, year), sems in sorted(by_cohort.items()):
        if len(sems) > 1:
            detail = ", ".join(f"Semester {s}: {n}" for s, n in sorted(sems.items()))
            issues.append({"branch": branch, "year": year, "semesters": sorted(sems.items()),
                           "key": f"{branch}|{year}",
                           "message": f"{branch} Year {year} has more than one semester ({detail})."})
    return issues


# ───────────────────────── promotion rules ─────────────────────────
def check_promotion(from_sem, groups=None, branch=None):
    """Return (ok, level, message). level is 'error' or 'warning' when not ok."""
    groups = branch_semester_counts() if groups is None else groups
    if branch is not None and from_sem is None:
        return False, "error", "Choose a semester to promote one department."
    counts = semester_counts(groups, branch=branch)
    where = f" in {branch}" if branch else ""

    if from_sem is None:
        if counts.get(8, 0) > 0:
            return False, "error", (f"Remove the {counts[8]} Semester 8 student(s) before promoting "
                                    "students to the next semester.")
        if sum(counts.get(s, 0) for s in PROMOTABLE_SEMESTERS) == 0:
            return False, "warning", "No students in Semesters 1 to 7 found for promotion."
        movers = set(PROMOTABLE_SEMESTERS)
    else:
        if from_sem not in PROMOTABLE_SEMESTERS:
            return False, "error", "Semester must be between 1 and 7."
        if counts.get(from_sem, 0) == 0:
            return False, "warning", f"No students in Semester {from_sem}{where} found for promotion."
        if from_sem in NEW_YEAR_FROM and counts.get(8, 0) > 0:
            return False, "error", (f"Remove Semester 8 students{where} before promoting students to the next "
                                    "academic year.")
        movers = {from_sem}

    problems = []
    branches = [branch] if branch else sorted({b for (b, _) in groups})
    for b in branches:
        pre = {s: n for (bb, s), n in groups.items() if bb == b and n}
        moving = {s: n for s, n in pre.items() if s in movers}
        if not moving:
            continue
        staying = {s: n for s, n in pre.items() if s not in movers}

        # Batch merge: a moving batch lands in a year that already has students who stay put.
        for s, n in sorted(moving.items()):
            target_year = year_for_semester(s + 1)
            blockers = {t: k for t, k in staying.items() if year_for_semester(t) == target_year}
            if blockers:
                t, k = sorted(blockers.items())[0]
                problems.append(f"{b}: Semester {s} → {s + 1} would join {b} Year {target_year}, "
                                f"which already has {k} student(s) in Semester {t} (two batches would merge).")

        # Mixing: after the move, a Department + Year must hold one semester only.
        post = {}
        for s, n in pre.items():
            new_s = s + 1 if s in movers else s
            y = year_for_semester(new_s)
            post.setdefault(y, {})
            post[y][new_s] = post[y].get(new_s, 0) + n
        touched = {year_for_semester(s) for s in moving} | {year_for_semester(s + 1) for s in moving}
        for y in sorted(touched):
            sems = post.get(y, {})
            if len(sems) > 1:
                detail = " and ".join(f"Semester {s} ({n})" for s, n in sorted(sems.items()))
                problems.append(f"{b} Year {y} would end up with students in {detail}.")

    if problems:
        unique = list(dict.fromkeys(problems))
        more = f" (+{len(unique) - 3} more)" if len(unique) > 3 else ""
        return False, "error", "Promotion blocked. " + " ".join(unique[:3]) + more
    return True, None, ""


def promotion_checks(groups=None):
    """Everything the page previews: 'full', '<sem>' (all departments) and '<sem>|<branch>'."""
    groups = branch_semester_counts() if groups is None else groups
    out = {}
    for key, sem in [("full", None)] + [(str(s), s) for s in PROMOTABLE_SEMESTERS]:
        ok, level, message = check_promotion(sem, groups)
        out[key] = {"ok": ok, "level": level or "", "message": message,
                    "count": sum(n for (b, s), n in groups.items() if sem is None or s == sem)}
    for b in sorted({b for (b, _) in groups if b}):
        for sem in PROMOTABLE_SEMESTERS:
            ok, level, message = check_promotion(sem, groups, branch=b)
            out[f"{sem}|{b}"] = {"ok": ok, "level": level or "", "message": message,
                                 "count": groups.get((b, sem), 0)}
    return out


# ───────────────────────── transactions ─────────────────────────
_TXN_SUPPORTED = None


def transactions_supported():
    """True on a replica set / mongos (MongoDB Atlas); False on a standalone mongod."""
    global _TXN_SUPPORTED
    if _TXN_SUPPORTED is None:
        try:
            hello = client.admin.command("hello")
            _TXN_SUPPORTED = bool(hello.get("setName")) or hello.get("msg") == "isdbgrid"
        except PyMongoError:
            _TXN_SUPPORTED = False
    return _TXN_SUPPORTED


def _in_transaction(fn):
    """Run fn(session) atomically when possible, else fn(None)."""
    if transactions_supported():
        with client.start_session() as session:
            return session.with_transaction(lambda s: fn(s))
    return fn(None)


# ───────────────────────── history rows ─────────────────────────
def _required_offices(student):
    if student.get("student_type") == "Day Scholar":
        return ["LIBRARY", "COLLEGE", "DEPARTMENT"]
    return ["LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"]


def _history_lookup(student, from_sem):
    from_year = student.get("year") or year_for_semester(from_sem)
    return {"student_id": student["_id"], "$or": [
        {"previous_semester": from_sem, "previous_year": from_year},
        {"semester": from_sem, "year": from_year},
    ]}


def _write_history(student, from_sem, to_sem, now, run_id, dues, session=None):
    """One history row per student per completed semester (idempotent)."""
    from_year = student.get("year") or year_for_semester(from_sem)
    offices = _required_offices(student)
    cleared = sum(1 for d in dues if d.get("office") in offices and d.get("status") == "APPROVED") == len(offices)
    existing = promotion_logs.find_one(_history_lookup(student, from_sem), session=session)
    if existing:
        promotion_logs.update_one({"_id": existing["_id"]}, {"$set": {
            "new_semester": to_sem, "new_year": year_for_semester(to_sem),
            "promotion_time": existing.get("promotion_time") or now,
        }}, session=session)
    else:
        promotion_logs.insert_one({
            "student_id": student["_id"],
            "previous_semester": from_sem, "previous_year": from_year,
            "semester": from_sem, "year": from_year,
            "new_semester": to_sem, "new_year": year_for_semester(to_sem),
            "student_type": student.get("student_type", "Hosteller"),
            "completion_time": now if cleared else None,
            "promotion_time": now,
            "no_due_cleared": cleared,
            "status": "Completed" if cleared else "Not Cleared",
            "promotion_run_id": run_id,
        }, session=session)


# ───────────────────────── journal / restore ─────────────────────────
def _journal_students(run_id, students, new_sem_of, with_history=True):
    """Write one journal item per student: everything needed to put them back."""
    items, ops = [], []
    for st in students:
        old_sem = _int_or_none(st.get("semester"))
        new_sem = new_sem_of(st)
        item = {"run_id": run_id, "student_id": st["_id"], "branch": st.get("branch"),
                "old_semester": st.get("semester"), "old_year": st.get("year"),
                "from_sem": old_sem, "new_semester": new_sem, "new_year": year_for_semester(new_sem),
                "student_type": st.get("student_type", "Hosteller"),
                "dues": list(no_due_col.find({"student_id": st["_id"]})),
                "log_before": promotion_logs.find_one(_history_lookup(st, old_sem)) if with_history else None}
        items.append(item)
        ops.append(InsertOne(item))
    if ops:
        promotion_run_items.bulk_write(ops, ordered=True)
    return items


def _restore(items, run_id, session=None, only_if_unchanged=True):
    """Put students, no-dues and history rows back exactly as journalled. Returns (restored, skipped)."""
    restored, skipped = 0, 0
    for it in items:
        sid = it["student_id"]
        flt = {"_id": sid}
        if only_if_unchanged:
            flt["semester"] = {"$in": [it["new_semester"], it["old_semester"]]}
        res = students_col.update_one(flt, {"$set": {"semester": it["old_semester"], "year": it["old_year"]}},
                                      session=session)
        if res.matched_count == 0:
            skipped += 1                     # student deleted or moved again since — leave untouched
            continue
        no_due_col.delete_many({"student_id": sid}, session=session)
        if it.get("dues"):
            no_due_col.insert_many(it["dues"], session=session)
        promotion_logs.delete_many({"student_id": sid, "promotion_run_id": run_id}, session=session)
        if it.get("log_before"):
            promotion_logs.replace_one({"_id": it["log_before"]["_id"]}, it["log_before"], upsert=True,
                                       session=session)
        restored += 1
    return restored, skipped


def _undo_run(run_id, status, actor, only_if_unchanged=True):
    items = list(promotion_run_items.find({"run_id": run_id}))
    restored, skipped = _in_transaction(lambda s: _restore(items, run_id, session=s,
                                                           only_if_unchanged=only_if_unchanged))
    promotion_runs.update_one({"_id": run_id}, {"$set": {
        "status": status, "rolled_back_at": datetime.now(), "rolled_back_by": actor,
        "restored": restored, "skipped": skipped}})
    return items, restored, skipped


def recover_interrupted_runs(lock_held=False):
    """
    Undo runs left 'in_progress'/'interrupted' by a crashed or killed process.
    Safe to call any time: only acts when it can take (or already holds) the lock,
    i.e. when no promotion is actually running. Returns how many runs were undone.
    """
    if not lock_held:
        # Cheap pre-check so the minute-by-minute maintenance call doesn't take the lock for nothing.
        if not promotion_runs.find_one({"status": {"$in": ["in_progress", "interrupted"]}}, {"_id": 1}):
            return 0
        if not acquire_lock():
            return 0          # a promotion is genuinely running; try again next cycle
    try:
        recovered = 0
        for run in promotion_runs.find({"status": {"$in": ["in_progress", "interrupted"]}}).sort("started_at", -1):
            _, restored, skipped = _undo_run(run["_id"], "auto_recovered", "system")
            audit_logger.warning("PROMOTION auto-recovered interrupted run=%s restored=%d skipped=%d",
                                 run["_id"], restored, skipped)
            recovered += 1
        return recovered
    except Exception:
        logger.exception("recover_interrupted_runs failed")
        return 0
    finally:
        if not lock_held:
            release_lock()


# ───────────────────────── promotion ─────────────────────────
def _apply(items, semesters, now, run_id, session=None):
    """The actual writes. Highest semester first so nobody moves twice."""
    by_sem = {}
    for it in items:
        by_sem.setdefault(it["from_sem"], []).append(it)
    moved_total, by_semester = 0, {}
    for sem in semesters:
        batch = by_sem.get(sem, [])
        if not batch:
            continue
        extend_lock()                                       # heartbeat (outside the transaction)
        ids = [it["student_id"] for it in batch]
        for it in batch:                                    # 1. history (from the journalled dues)
            student = {"_id": it["student_id"], "year": it["old_year"], "student_type": it["student_type"]}
            _write_history(student, sem, sem + 1, now, run_id, it["dues"], session=session)
        no_due_col.update_many(                             # 2. fresh no-dues for the new semester
            {"student_id": {"$in": ids}},
            {"$set": {"status": "NOT_SENT", "attempts_used": 0, "attempts_model": 2,
                      "receipt_url": None, "cloudinary_public_id": None, "reject_reason": None,
                      "last_payment_id": None, "updated_at": now},
             "$unset": {"cooldown_expiry": "", "second_rejection_at": ""}},
            session=session)
        moved = students_col.update_many(                   # 3. move (only those still in `sem`)
            {"_id": {"$in": ids}, "semester": _semester_filter(sem)},
            {"$set": {"semester": sem + 1, "year": year_for_semester(sem + 1)}},
            session=session).modified_count
        by_semester[sem] = moved
        moved_total += moved
    return moved_total, by_semester


def run_promotion(from_sem=None, expected_snapshot=None, actor="faculty", branch=None):
    """
    Promote students. from_sem=None → full promotion (Semesters 1–7).
    branch (with from_sem) → only that department's students in from_sem.
    Returns dict(ok, level, message, promoted, by_semester, moves, run_id).
    """
    result = {"ok": False, "level": "error", "message": "", "promoted": 0,
              "by_semester": {}, "promoted_ids": [], "moves": [], "run_id": None}
    branch = branch or None

    if not acquire_lock():
        result["message"] = "A promotion is already in progress. Please wait a moment and reload."
        audit_logger.warning("PROMOTION blocked: another run in progress")
        return result

    run_id = None
    try:
        recover_interrupted_runs(lock_held=True)       # never stack a new run on a crashed one
        time.sleep(WRITE_GRACE_SECONDS)                # roster writes already past their check finish now

        groups = branch_semester_counts()
        if expected_snapshot is not None and expected_snapshot != counts_snapshot(groups):
            result["message"] = ("Student numbers changed since this page was opened (another promotion or "
                                 "roster change). Nothing was promoted — reload the page and check again.")
            audit_logger.warning("PROMOTION blocked: stale page snapshot")
            return result

        ok, level, message = check_promotion(from_sem, groups, branch=branch)
        if not ok:
            result.update(level=level, message=message)
            return result
        issues_before = {i["key"] for i in cohort_issues(groups)}

        semesters = [from_sem] if from_sem is not None else sorted(PROMOTABLE_SEMESTERS, reverse=True)
        now = datetime.now()
        run_id = ObjectId()
        use_txn = transactions_supported()
        promotion_runs.insert_one({
            "_id": run_id, "status": "in_progress", "mode": "semester" if from_sem else "full",
            "from_sem": from_sem, "branch": branch, "actor": actor, "started_at": now, "atomic": use_txn,
            "counts_before": {f"{b}|{s}": n for (b, s), n in groups.items()},
        })
        movers = []
        for sem in semesters:
            q = {"semester": _semester_filter(sem)}
            if branch:
                q["branch"] = branch
            movers.extend(students_col.find(q, {"_id": 1, "student_type": 1, "year": 1, "semester": 1, "branch": 1}))
        items = _journal_students(run_id, movers, lambda st: _int_or_none(st.get("semester")) + 1)

        try:
            moved, by_semester = _in_transaction(lambda s: _apply(items, semesters, now, run_id, session=s))
        except Exception:
            logger.exception("Promotion failed while applying; undoing")
            if not use_txn:
                _restore(items, run_id, only_if_unchanged=False)   # standalone: replay the journal backwards
            promotion_runs.update_one({"_id": run_id}, {"$set": {"status": "failed", "finished_at": datetime.now()}})
            result["message"] = ("Promotion failed and was undone — no student was changed. "
                                 "Please reload the page and try again.")
            return result

        promotion_runs.update_one({"_id": run_id}, {"$set": {
            "status": "completed", "finished_at": datetime.now(), "promoted": moved,
            "by_semester": {str(k): v for k, v in by_semester.items()}}})

        # Integrity check: if a class that was fine now has two semesters, undo this run.
        new_issues = [i for i in cohort_issues() if i["key"] not in issues_before]
        if new_issues:
            _undo_run(run_id, "auto_undone", "system")
            audit_logger.warning("PROMOTION auto-undone run=%s: %s", run_id, [i["message"] for i in new_issues])
            result["message"] = ("The roster changed while promoting (" + new_issues[0]["message"] + "), so the "
                                 "promotion was undone automatically. Reload the page and try again.")
            return result

        detail = ", ".join(f"Sem {s}→{s + 1}: {n}" for s, n in sorted(by_semester.items()) if n)
        scope = (f"Semester {from_sem}" if from_sem is not None else "Semesters 1–7") + (f" ({branch})" if branch else "")
        result.update(ok=True, level="success", promoted=moved, by_semester=by_semester, run_id=run_id,
                      promoted_ids=[it["student_id"] for it in items],
                      moves=[(it["student_id"], it["new_semester"], it["new_year"]) for it in items],
                      message=(f"Promoted {moved} student(s) from {scope}" + (f": {detail}" if detail else "")
                               + ". If this was a mistake, use Undo under “Last promotion” before students "
                                 "start sending requests."))
        audit_logger.info("PROMOTION completed run=%s from_sem=%s branch=%s promoted=%d atomic=%s detail=%s",
                          run_id, from_sem if from_sem is not None else "ALL", branch or "ALL", moved, use_txn, detail)
        return result
    except Exception:
        logger.exception("Promotion failed")
        if run_id is not None:
            promotion_runs.update_one({"_id": run_id, "status": "in_progress"},
                                      {"$set": {"status": "interrupted"}})
            try:
                recover_interrupted_runs(lock_held=True)
                result["message"] = ("Promotion stopped because of an internal error and was undone — no student "
                                     "was changed. Please reload the page and try again.")
                return result
            except Exception:
                pass
        result["message"] = "Promotion stopped because of an internal error. Please reload the page and try again."
        return result
    finally:
        release_lock()


# ───────────────────────── undo ─────────────────────────
def last_run():
    """Most recent run that can still be undone."""
    return promotion_runs.find_one({"status": {"$in": UNDOABLE_STATUSES}}, sort=[("started_at", -1)])


def undo_blockers(run):
    """How many requests students from this run sent after it (undo would discard them)."""
    if not run:
        return 0
    ids = promotion_run_items.distinct("student_id", {"run_id": run["_id"]})
    if not ids:
        return 0
    since = run.get("finished_at") or run.get("started_at")
    return no_due_col.count_documents({"student_id": {"$in": ids}, "status": {"$ne": "NOT_SENT"},
                                       "$or": [{"created_at": {"$gt": since}}, {"updated_at": {"$gt": since}}]})


def rollback_last_run(run_id, actor="faculty"):
    """Undo the most recent run from its journal. Returns dict(ok, level, message, …)."""
    try:
        run_id = ObjectId(str(run_id))
    except Exception:
        return {"ok": False, "level": "error", "message": "Unknown promotion run."}

    if not acquire_lock():
        return {"ok": False, "level": "error", "message": "A promotion is in progress. Please wait and try again."}
    try:
        latest = last_run()
        if not latest or latest["_id"] != run_id:
            return {"ok": False, "level": "error",
                    "message": "Only the most recent promotion can be undone, and this one is not it any more."}
        blockers = undo_blockers(latest)
        if blockers:
            return {"ok": False, "level": "error",
                    "message": (f"Undo is no longer possible: students have already sent {blockers} request(s) "
                                "for the new semester, and undoing would discard them. Fix individual students "
                                "with Edit instead.")}
        items, restored, skipped = _undo_run(run_id, "rolled_back", actor)
        audit_logger.info("PROMOTION rollback run=%s restored=%d skipped=%d", run_id, restored, skipped)
        what = "fix" if latest.get("mode") == "fix" else "promotion"
        msg = f"Undid the {what}: {restored} student(s) are back in their previous semester."
        if skipped:
            msg += f" {skipped} student(s) were skipped because they were deleted or changed afterwards."
        return {"ok": True, "level": "success" if not skipped else "warning", "message": msg,
                "restored": restored, "skipped": skipped,
                "items": [(it["student_id"], it["old_semester"], it["old_year"]) for it in items]}
    except Exception:
        logger.exception("Rollback failed")
        return {"ok": False, "level": "error", "message": "Undo failed because of an internal error. Please try again."}
    finally:
        release_lock()


# ───────────────────────── repair a mixed class ─────────────────────────
def fix_cohort(branch, year, semester, actor="faculty"):
    """Put every student of Department + Year into one semester (journalled → undoable)."""
    year, semester = int(year), int(semester)
    if semester not in semesters_of_year(year):
        return {"ok": False, "level": "error", "message": f"Semester {semester} is not part of Year {year}."}
    if not acquire_lock():
        return {"ok": False, "level": "error", "message": "A promotion is in progress. Please wait and try again."}
    try:
        recover_interrupted_runs(lock_held=True)
        pair = semesters_of_year(year)
        query = {"branch": branch, "semester": {"$in": [s for s in (*pair, *(str(p) for p in pair))
                                                        if _int_or_none(s) != semester]}}
        movers = list(students_col.find(query, {"_id": 1, "semester": 1, "year": 1, "branch": 1, "student_type": 1}))
        if not movers:
            return {"ok": False, "level": "warning", "message": f"{branch} Year {year} already has one semester."}
        run_id = ObjectId()
        now = datetime.now()
        promotion_runs.insert_one({"_id": run_id, "status": "in_progress", "mode": "fix", "branch": branch,
                                   "year": year, "to_sem": semester, "actor": actor, "started_at": now,
                                   "atomic": transactions_supported()})
        items = _journal_students(run_id, movers, lambda st: semester, with_history=False)
        ids = [it["student_id"] for it in items]

        def apply(session):
            return students_col.update_many({"_id": {"$in": ids}},
                                            {"$set": {"semester": semester, "year": year}},
                                            session=session).modified_count
        try:
            moved = _in_transaction(apply)
        except Exception:
            logger.exception("fix_cohort failed; undoing")
            if not transactions_supported():
                _restore(items, run_id, only_if_unchanged=False)
            promotion_runs.update_one({"_id": run_id}, {"$set": {"status": "failed"}})
            return {"ok": False, "level": "error", "message": "The fix failed and was undone. Please try again."}
        promotion_runs.update_one({"_id": run_id}, {"$set": {"status": "completed", "finished_at": datetime.now(),
                                                             "promoted": moved}})
        audit_logger.info("COHORT FIX run=%s %s Year %s -> Semester %s moved=%d", run_id, branch, year, semester, moved)
        return {"ok": True, "level": "success", "run_id": run_id,
                "message": (f"{branch} Year {year}: moved {moved} student(s) to Semester {semester}. "
                            "You can undo this under “Last promotion”.")}
    finally:
        release_lock()


# ───────────────────────── archive ─────────────────────────
def archive_students(student_ids, reason, actor="faculty"):
    """Copy students + their no-dues + history to `archived_students` before they are deleted."""
    if not student_ids:
        return 0
    now = datetime.now()
    ops = []
    for st in students_col.find({"_id": {"$in": list(student_ids)}}):
        ops.append(InsertOne({
            "student": st,
            "no_dues": list(no_due_col.find({"student_id": st["_id"]})),
            "history": list(promotion_logs.find({"student_id": st["_id"]})),
            "reason": reason, "archived_by": actor, "archived_at": now,
        }))
    if ops:
        archived_students.bulk_write(ops, ordered=False)
    return len(ops)


def list_archived(limit=200):
    rows = []
    for a in archived_students.find({}, {"no_dues": 0, "history": 0}).sort("archived_at", -1).limit(limit):
        st = a.get("student") or {}
        rows.append({"id": str(a["_id"]), "reg_no": st.get("reg_no", ""), "name": st.get("name", ""),
                     "branch": st.get("branch", ""), "semester": st.get("semester", ""), "year": st.get("year", ""),
                     "reason": {"semester_8_removal": "Semester 8 removal",
                                "deleted_by_faculty": "Deleted by faculty"}.get(a.get("reason"), a.get("reason", "")),
                     "archived_at": a.get("archived_at")})
    return rows


def restore_archived(archive_ids, actor="faculty"):
    """Put archived students back (same ids, no-dues and history). Skips conflicts and reports them."""
    restored, problems = 0, []
    if promotion_running():
        return {"ok": False, "level": "error", "restored": 0,
                "message": "A semester promotion is running right now. Please try again in a minute."}
    oids = []
    for a in archive_ids:
        try:
            oids.append(ObjectId(str(a)))
        except Exception:
            pass
    for a in archived_students.find({"_id": {"$in": oids}}):
        st = a.get("student") or {}
        reg = st.get("reg_no", "?")
        if students_col.find_one({"$or": [{"_id": st.get("_id")}, {"reg_no": st.get("reg_no")}]}, {"_id": 1}):
            problems.append(f"{reg}: a student with this Register Number already exists")
            continue
        sem, year = _int_or_none(st.get("semester")), _int_or_none(st.get("year"))
        if sem and year and st.get("branch") and sem in semesters_of_year(year):
            err = check_cohort_semester(st["branch"], year, sem)
            if err:
                problems.append(f"{reg}: {err}")
                continue
        try:
            students_col.insert_one(st)
        except DuplicateKeyError:
            problems.append(f"{reg}: Register or Roll Number is already used by another student")
            continue
        if sem and year and st.get("branch"):
            err = verify_cohort_after_write(st["branch"], year)
            if err:
                students_col.delete_one({"_id": st["_id"]})
                problems.append(f"{reg}: {err}")
                continue
        for d in a.get("no_dues") or []:
            try:
                no_due_col.insert_one(d)
            except DuplicateKeyError:
                pass
        for h in a.get("history") or []:
            try:
                promotion_logs.insert_one(h)
            except DuplicateKeyError:
                pass
        archived_students.delete_one({"_id": a["_id"]})
        restored += 1
    audit_logger.info("ARCHIVE restore by=%s restored=%d problems=%d", actor, restored, len(problems))
    msg = f"Restored {restored} student(s)."
    if problems:
        msg += " Not restored: " + "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")
    return {"ok": restored > 0, "level": "success" if not problems else ("warning" if restored else "error"),
            "restored": restored, "message": msg}
