"""One independent local digest job; catalog commits remain on collector thread."""
from __future__ import annotations
import threading
import time


class Verifier:
    def __init__(self, digest):
        self.digest = digest
        self.thread = None
        self.result = None
        self.job_id = None

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def submit(self, job_id, path):
        if self.thread is not None:
            raise RuntimeError("Verification slot has not been drained")
        self.job_id = job_id
        def run():
            start = time.monotonic()
            try:
                self.result = (job_id, self.digest(path), None, time.monotonic() - start)
            except Exception as exc:
                self.result = (job_id, None, exc, time.monotonic() - start)
        self.thread = threading.Thread(target=run, name="hub-verification", daemon=True)
        self.thread.start()

    def poll(self):
        if self.thread is None or self.thread.is_alive():
            return None
        self.thread.join()
        result = self.result
        self.thread = self.result = self.job_id = None
        return result
