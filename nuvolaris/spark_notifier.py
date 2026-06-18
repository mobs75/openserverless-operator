# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import os
import json
import time
import logging
import datetime
import base64
import requests
import requests.auth

_log = logging.getLogger(__name__)

TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})
_SPARK_JOBS_DB = "nuvolaris_spark_jobs"


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class CouchDBConnectionError(Exception):
    pass


class DeliveryError(Exception):
    pass


# ---------------------------------------------------------------------------
# CouchDB state store
# ---------------------------------------------------------------------------

class CouchDBStateStore:
    """
    Manages Spark job lifecycle documents in CouchDB.

    Document schema:
        _id / job_id    : Spark job identifier (nuvolaris.org/spark-job-id label)
        namespace       : Kubernetes namespace
        pod_phase       : Last observed Kubernetes pod phase
        status          : pending | running | notifying | notified | delivery_failed
        created_at      : ISO-8601 UTC
        updated_at      : ISO-8601 UTC
        notified_at     : ISO-8601 UTC or null
        delivery_attempts: int
        last_error      : str or null
    """

    def __init__(self):
        raw_url = os.environ.get("COUCHDB_URL", "http://couchdb:5984").rstrip("/")
        user = os.environ.get("COUCHDB_USER", "whisk_admin")
        password = os.environ.get("COUCHDB_PASSWORD", "")
        self._db_url = f"{raw_url}/{_SPARK_JOBS_DB}"
        self._session = requests.Session()
        self._session.auth = requests.auth.HTTPBasicAuth(user, password)
        self._session.headers.update({"Content-Type": "application/json"})
        self._ensure_db(raw_url, user, password)

    def _ensure_db(self, base_url: str, user: str, password: str) -> None:
        s = requests.Session()
        s.auth = requests.auth.HTTPBasicAuth(user, password)
        try:
            r = s.head(f"{base_url}/{_SPARK_JOBS_DB}", timeout=5)
            if r.status_code == 404:
                s.put(f"{base_url}/{_SPARK_JOBS_DB}", timeout=5)
        except requests.ConnectionError:
            pass

    # -- low-level helpers --------------------------------------------------

    def get_doc(self, job_id: str) -> dict | None:
        """Return the full CouchDB document (including _rev), or None if absent."""
        try:
            r = self._session.get(f"{self._db_url}/{job_id}", timeout=5)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.ConnectionError as exc:
            raise CouchDBConnectionError(str(exc)) from exc

    def _put_doc(self, doc: dict) -> tuple[int, dict]:
        """PUT doc to CouchDB. Returns (status_code, response_body)."""
        doc_id = doc.get("_id") or doc.get("job_id")
        try:
            r = self._session.put(
                f"{self._db_url}/{doc_id}",
                data=json.dumps(doc),
                timeout=5,
            )
            return r.status_code, r.json()
        except requests.ConnectionError as exc:
            raise CouchDBConnectionError(str(exc)) from exc

    # -- public API ---------------------------------------------------------

    def upsert_phase(self, job_id: str, namespace: str, phase: str) -> None:
        """Create or update the job document with the current pod phase.

        Retries up to 3 times on CouchDB connection errors (R5.AC5).
        Uses optimistic concurrency (_rev) to handle concurrent writers (R5.AC2).
        """
        now = _now()
        for attempt in range(3):
            try:
                existing = self.get_doc(job_id)
                if existing is None:
                    doc: dict = {
                        "_id": job_id,
                        "job_id": job_id,
                        "namespace": namespace,
                        "pod_phase": phase,
                        "status": "pending",
                        "created_at": now,
                        "updated_at": now,
                        "notified_at": None,
                        "delivery_attempts": 0,
                        "last_error": None,
                    }
                else:
                    doc = dict(existing)
                    doc["pod_phase"] = phase
                    doc["updated_at"] = now
                    current = existing.get("status", "pending")
                    if current not in ("notifying", "notified", "delivery_failed"):
                        if phase == "Running":
                            doc["status"] = "running"

                status_code, _ = self._put_doc(doc)
                if status_code in (200, 201):
                    _log.info(json.dumps({
                        "event_type": "phase_updated",
                        "job_id": job_id,
                        "phase": phase,
                        "status": doc["status"],
                    }))
                    return
                if status_code == 409:
                    _log.warning(json.dumps({
                        "event_type": "upsert_conflict",
                        "job_id": job_id,
                        "attempt": attempt + 1,
                    }))
                    continue
            except CouchDBConnectionError as exc:
                _log.error(json.dumps({
                    "event_type": "couchdb_error",
                    "job_id": job_id,
                    "attempt": attempt + 1,
                    "error": str(exc),
                }))
                if attempt == 2:
                    return  # R5.AC5: log and continue pod monitoring
                time.sleep(1)

    def claim_notifying(self, job_id: str, rev: str) -> bool:
        """Atomically claim the notifying state using CouchDB _rev conflict detection.

        GETs the current document and PUTs with status=notifying. If the document
        changed since the caller's read (detected via _rev mismatch or 409), returns
        False so the caller can yield to the winning instance (R2.AC3).
        """
        now = _now()
        try:
            doc = self.get_doc(job_id)
            if doc is None:
                return False
            if doc.get("status") in ("notifying", "notified"):
                return False
            if doc.get("_rev") != rev:
                return False  # doc changed since caller's read
            doc["status"] = "notifying"
            doc["updated_at"] = now
            status_code, _ = self._put_doc(doc)
            return status_code in (200, 201)
        except CouchDBConnectionError:
            return False

    def mark_notified(self, job_id: str) -> None:
        """Set status=notified and populate notified_at (R5.AC3)."""
        now = _now()
        try:
            for _ in range(2):  # one retry on 409 (R5.AC4)
                doc = self.get_doc(job_id)
                if doc is None:
                    return
                doc["status"] = "notified"
                doc["notified_at"] = now
                doc["updated_at"] = now
                status_code, _ = self._put_doc(doc)
                if status_code in (200, 201):
                    _log.info(json.dumps({
                        "event_type": "job_notified",
                        "job_id": job_id,
                        "notified_at": now,
                    }))
                    return
                if status_code != 409:
                    _log.error(json.dumps({
                        "event_type": "couchdb_error",
                        "job_id": job_id,
                        "error": f"unexpected status {status_code} on mark_notified",
                    }))
                    return
            _log.error(json.dumps({
                "event_type": "couchdb_error",
                "job_id": job_id,
                "error": "409 on mark_notified after retry",
            }))
        except CouchDBConnectionError as exc:
            _log.error(json.dumps({"event_type": "couchdb_error", "job_id": job_id, "error": str(exc)}))

    def mark_delivery_failed(self, job_id: str, error: Exception | None = None) -> None:
        """Set status=delivery_failed after exhausted OWS retry attempts (R3.AC3)."""
        now = _now()
        try:
            for _ in range(2):  # one retry on 409 (R5.AC4)
                doc = self.get_doc(job_id)
                if doc is None:
                    return
                doc["status"] = "delivery_failed"
                doc["updated_at"] = now
                doc["delivery_attempts"] = doc.get("delivery_attempts", 0) + 1
                doc["last_error"] = str(error) if error else None
                status_code, _ = self._put_doc(doc)
                if status_code in (200, 201):
                    return
                if status_code != 409:
                    return
            _log.error(json.dumps({
                "event_type": "couchdb_error",
                "job_id": job_id,
                "error": "409 on mark_delivery_failed after retry",
            }))
        except CouchDBConnectionError as exc:
            _log.error(json.dumps({"event_type": "couchdb_error", "job_id": job_id, "error": str(exc)}))

    def query_stale(self, threshold_minutes: int = 5) -> list[dict]:
        """Return job docs that are non-notified and not updated within threshold_minutes (R4.AC1)."""
        threshold = (
            datetime.datetime.utcnow() - datetime.timedelta(minutes=threshold_minutes)
        ).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        query = {
            "selector": {
                "status": {"$nin": ["notified"]},
                "updated_at": {"$lt": threshold},
            },
            "limit": 100,
        }
        try:
            r = self._session.post(f"{self._db_url}/_find", json=query, timeout=10)
            if r.status_code == 404:
                return []
            r.raise_for_status()
            return r.json().get("docs", [])
        except requests.ConnectionError as exc:
            raise CouchDBConnectionError(str(exc)) from exc


# ---------------------------------------------------------------------------
# OpenServerless trigger client
# ---------------------------------------------------------------------------

class OWSClient:
    """HTTP client for OpenServerless trigger notifications."""

    def __init__(self):
        self._trigger_url = os.environ.get("OWS_TRIGGER_URL", "")
        self._auth_token = os.environ.get("OWS_AUTH_TOKEN", "")

    def post_event(
        self,
        job_id: str,
        status: str,
        namespace: str,
        timestamp: str,
    ) -> None:
        """POST notification to OpenServerless trigger.

        Retries up to 3 times on 5xx or ConnectionError with exponential back-off
        1s / 2s / 4s (R3.AC2). Raises DeliveryError after all retries fail (R3.AC3).
        """
        payload = {
            "job_id": job_id,
            "status": status,
            "namespace": namespace,
            "timestamp": timestamp,
        }
        headers = {"Authorization": "Basic " + base64.b64encode(self._auth_token.encode()).decode()}
        delays = [1, 2, 4]
        last_exc: Exception | None = None

        for attempt, delay in enumerate(delays, start=1):
            try:
                r = requests.post(
                    self._trigger_url,
                    json=payload,
                    headers=headers,
                    timeout=10,
                )
                if r.status_code < 500:
                    _log.info(json.dumps({
                        "event_type": "ows_delivered",
                        "job_id": job_id,
                        "status": status,
                        "http_status": r.status_code,
                    }))
                    return
                last_exc = Exception(f"HTTP {r.status_code}")
            except requests.ConnectionError as exc:
                last_exc = exc

            _log.warning(json.dumps({
                "event_type": "ows_retry",
                "job_id": job_id,
                "attempt": attempt,
                "error": str(last_exc),
            }))
            if attempt < len(delays):
                time.sleep(delay)

        _log.error(json.dumps({
            "event_type": "ows_delivery_failed",
            "job_id": job_id,
            "error": str(last_exc),
        }))
        raise DeliveryError(str(last_exc)) from last_exc


# ---------------------------------------------------------------------------
# exactly-once notification
# ---------------------------------------------------------------------------

def mark_terminal_once(
    job_id: str,
    phase: str,
    store: CouchDBStateStore,
    ows_client: OWSClient,
) -> None:
    """Fire exactly one OpenServerless notification for a terminal Spark job.

    Algorithm (R2.AC3, R2.AC4):
    1. GET CouchDB doc.
    2. If status == 'notified' → return (idempotency guard).
    3. claim_notifying() with _rev → if False → return (lost the race).
    4. POST to OpenServerless trigger.
    5. On success → mark_notified().
       On DeliveryError → mark_delivery_failed().
    """
    doc = store.get_doc(job_id)
    if doc is None:
        _log.warning(json.dumps({"event_type": "mark_terminal_no_doc", "job_id": job_id}))
        return

    if doc.get("status") == "notified":
        _log.info(json.dumps({"event_type": "idempotency_guard_hit", "job_id": job_id}))
        return

    rev = doc.get("_rev", "")
    if not store.claim_notifying(job_id, rev):
        _log.info(json.dumps({"event_type": "claim_lost", "job_id": job_id}))
        return

    timestamp = _now()
    namespace = doc.get("namespace", "")
    try:
        ows_client.post_event(job_id, phase, namespace, timestamp)
    except DeliveryError as exc:
        store.mark_delivery_failed(job_id, error=exc)
        return

    store.mark_notified(job_id)
