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

import pytest
import json
from unittest.mock import MagicMock, patch, call
import requests

from nuvolaris.spark_notifier import CouchDBStateStore, CouchDBConnectionError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store():
    """Build a CouchDBStateStore with mocked HTTP session (no real CouchDB)."""
    with patch.object(CouchDBStateStore, "_ensure_db", return_value=None):
        store = CouchDBStateStore.__new__(CouchDBStateStore)
        store._db_url = "http://fake-couchdb:5984/nuvolaris_spark_jobs"
        store._session = MagicMock()
    return store


def _resp(status_code: int, body: dict):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body
    return r


# ---------------------------------------------------------------------------
# upsert_phase — document creation
# ---------------------------------------------------------------------------

class TestUpsertPhaseCreate:
    def test_creates_document_on_first_observation(self):
        """R5.AC1: WHEN first observed, the system SHALL create a CouchDB document."""
        store = _make_store()
        # GET returns 404 (no existing doc)
        store._session.get.return_value = _resp(404, {"error": "not_found"})
        store._session.put.return_value = _resp(201, {"ok": True, "rev": "1-abc"})

        store.upsert_phase("job-1", "nuvolaris", "Pending")

        put_call = store._session.put.call_args
        body = json.loads(put_call.kwargs["data"])
        assert body["job_id"] == "job-1"
        assert body["namespace"] == "nuvolaris"
        assert body["pod_phase"] == "Pending"
        assert body["status"] == "pending"
        assert "created_at" in body
        assert "updated_at" in body
        assert body["notified_at"] is None

    def test_sets_status_running_when_phase_is_running(self):
        """R2.AC5: Running phase → status=running, no notification."""
        store = _make_store()
        existing = {
            "_id": "job-2", "_rev": "1-abc",
            "job_id": "job-2", "namespace": "nuvolaris",
            "pod_phase": "Pending", "status": "pending",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "notified_at": None, "delivery_attempts": 0, "last_error": None,
        }
        store._session.get.return_value = _resp(200, existing)
        store._session.put.return_value = _resp(200, {"ok": True, "rev": "2-def"})

        store.upsert_phase("job-2", "nuvolaris", "Running")

        put_call = store._session.put.call_args
        body = json.loads(put_call.kwargs["data"])
        assert body["status"] == "running"
        assert body["pod_phase"] == "Running"


# ---------------------------------------------------------------------------
# upsert_phase — _rev update
# ---------------------------------------------------------------------------

class TestUpsertPhaseUpdate:
    def test_updates_existing_document_with_rev(self):
        """R5.AC2: existing doc → update status and updated_at with _rev."""
        store = _make_store()
        existing = {
            "_id": "job-3", "_rev": "2-xyz",
            "job_id": "job-3", "namespace": "nuvolaris",
            "pod_phase": "Running", "status": "running",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "notified_at": None, "delivery_attempts": 0, "last_error": None,
        }
        store._session.get.return_value = _resp(200, existing)
        store._session.put.return_value = _resp(200, {"ok": True, "rev": "3-new"})

        store.upsert_phase("job-3", "nuvolaris", "Succeeded")

        put_call = store._session.put.call_args
        body = json.loads(put_call.kwargs["data"])
        assert body["_rev"] == "2-xyz"  # original _rev preserved in PUT
        assert body["pod_phase"] == "Succeeded"

    def test_does_not_overwrite_notified_status(self):
        """Once notified, upsert_phase must not downgrade status."""
        store = _make_store()
        existing = {
            "_id": "job-4", "_rev": "5-done",
            "job_id": "job-4", "namespace": "nuvolaris",
            "pod_phase": "Succeeded", "status": "notified",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "notified_at": "2026-01-01T01:00:00Z", "delivery_attempts": 1, "last_error": None,
        }
        store._session.get.return_value = _resp(200, existing)
        store._session.put.return_value = _resp(200, {"ok": True, "rev": "6-x"})

        store.upsert_phase("job-4", "nuvolaris", "Succeeded")

        put_call = store._session.put.call_args
        body = json.loads(put_call.kwargs["data"])
        assert body["status"] == "notified"  # not downgraded


# ---------------------------------------------------------------------------
# claim_notifying — 409 conflict
# ---------------------------------------------------------------------------

class TestClaimNotifying:
    def test_returns_false_on_409_conflict(self):
        """R2.AC3: 409 → another instance claimed, return False."""
        store = _make_store()
        doc = {
            "_id": "job-5", "_rev": "1-aaa",
            "job_id": "job-5", "namespace": "nuvolaris",
            "pod_phase": "Succeeded", "status": "pending",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "notified_at": None, "delivery_attempts": 0, "last_error": None,
        }
        store._session.get.return_value = _resp(200, doc)
        store._session.put.return_value = _resp(409, {"error": "conflict"})

        result = store.claim_notifying("job-5", "1-aaa")

        assert result is False

    def test_returns_true_on_successful_claim(self):
        """Successful PUT → claim acquired."""
        store = _make_store()
        doc = {
            "_id": "job-6", "_rev": "1-bbb",
            "job_id": "job-6", "namespace": "nuvolaris",
            "pod_phase": "Failed", "status": "pending",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "notified_at": None, "delivery_attempts": 0, "last_error": None,
        }
        store._session.get.return_value = _resp(200, doc)
        store._session.put.return_value = _resp(200, {"ok": True, "rev": "2-ccc"})

        result = store.claim_notifying("job-6", "1-bbb")

        assert result is True

    def test_returns_false_if_already_notified(self):
        """R2.AC4: status=notified → no re-notification."""
        store = _make_store()
        doc = {
            "_id": "job-7", "_rev": "9-zzz",
            "job_id": "job-7", "namespace": "nuvolaris",
            "pod_phase": "Succeeded", "status": "notified",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "notified_at": "2026-01-01T01:00:00Z", "delivery_attempts": 1, "last_error": None,
        }
        store._session.get.return_value = _resp(200, doc)

        result = store.claim_notifying("job-7", "9-zzz")

        assert result is False
        store._session.put.assert_not_called()


# ---------------------------------------------------------------------------
# upsert_phase — ConnectionError retry
# ---------------------------------------------------------------------------

class TestUpsertPhaseConnectionError:
    def test_retries_3x_on_connection_error_then_returns(self):
        """R5.AC5: ConnectionError → retry 3x, log ERROR, continue monitoring (no raise)."""
        store = _make_store()
        store._session.get.side_effect = requests.ConnectionError("unreachable")

        # Must not raise
        store.upsert_phase("job-8", "nuvolaris", "Running")

        assert store._session.get.call_count == 3

    def test_succeeds_after_transient_connection_error(self):
        """First two attempts fail, third succeeds."""
        store = _make_store()
        existing_doc = {
            "_id": "job-9", "_rev": "1-aaa",
            "job_id": "job-9", "namespace": "nuvolaris",
            "pod_phase": "Pending", "status": "pending",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "notified_at": None, "delivery_attempts": 0, "last_error": None,
        }
        store._session.get.side_effect = [
            requests.ConnectionError("fail1"),
            requests.ConnectionError("fail2"),
            _resp(200, existing_doc),
        ]
        store._session.put.return_value = _resp(200, {"ok": True, "rev": "2-bbb"})

        store.upsert_phase("job-9", "nuvolaris", "Running")

        assert store._session.get.call_count == 3
        store._session.put.assert_called_once()
