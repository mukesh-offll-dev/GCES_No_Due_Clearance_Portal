"""
Gunicorn production configuration for the GCES No Due portal.

Workload profile: almost every request is I/O-bound — it waits on MongoDB
(pymongo) and occasionally Cloudinary. It is NOT CPU-bound. The right model is
therefore a modest number of processes, each with many threads (gthread), so a
thread parked on a socket read frees the CPU for another request.

Concurrency = workers * threads. Defaults below target ~400 concurrent request
slots on a 12+ core / 32 GB machine, and auto-scale on smaller hardware.

Override any value with an environment variable (WEB_CONCURRENCY, WEB_THREADS).

Run with:  gunicorn -c gunicorn.conf.py nodue_portal.wsgi
"""
import os
import multiprocessing

_cores = multiprocessing.cpu_count()

# ── Socket ────────────────────────────────────────────────────────────
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")

# ── Worker model ──────────────────────────────────────────────────────
# gthread: threaded sync workers. Ideal for blocking I/O clients like pymongo.
worker_class = "gthread"

# Processes: 2-4 worker processes. Prevents SQLite write lock contention and MongoDB connection exhaustion.
workers = int(os.environ.get("WEB_CONCURRENCY", min(max(_cores, 2), 4)))

# Threads per worker: 4 threads per worker process for I/O concurrency without excessive thread overhead.
threads = int(os.environ.get("WEB_THREADS", 4))

# Effective capacity = workers * threads  (e.g. 25 * 16 = 400 slots).
# Backlog of pending connections before the OS refuses new ones.
backlog = int(os.environ.get("GUNICORN_BACKLOG", 2048))

# ── Timeouts & reliability ────────────────────────────────────────────
# A request stuck longer than this has its worker killed & replaced, so one
# hung DB call cannot permanently consume a slot.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 60))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", 30))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", 5))

# Recycle workers periodically to bound memory growth / recover from leaks.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", 2000))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", 200))

# ── App loading ───────────────────────────────────────────────────────
# preload_app=False: each worker forks BEFORE importing the app, so every
# worker gets its own fresh MongoClient (sharing sockets across a fork is
# unsafe). This also lets AppConfig.ready() run per worker.
preload_app = False

# ── Logging ───────────────────────────────────────────────────────────
accesslog = "-"   # stdout
errorlog = "-"    # stderr
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")


def post_fork(server, worker):
    server.log.info("Worker spawned (pid=%s)", worker.pid)


def worker_int(worker):
    worker.log.info("Worker interrupted (pid=%s)", worker.pid)
