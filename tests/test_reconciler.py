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

import json
import logging
import pytest
from unittest.mock import MagicMock, patch, call

from nuvolaris.spark_notifier import (
    CouchDBStateStore,
    CouchDBConnectionError,
    OWSClient,
)
from nuvolaris.reconciler import reconcile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(stale_records=None):
    store = MagicMock(spec=CouchDBStateStore)
    store.query_stale.return_value = stale_records or []
    return store


def _make_ows():
    return MagicMock(spec=OWSClient)


def _stale_record(job_id="job-1", namespace="nuvolaris", pod_phase="Succeeded", status="running"):
    return {
        "_id": job_id, "_rev": "3-abc",
        "job_id": job_id, "namespace": namespace,
        "pod_phase": pod_phase, "status": status,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "notified_at": None, "delivery_attempts": 0, "last_error": None,
    }


def _mock_pod(phase="Running"):
    pod = MagicMock()
    pod.status.phase = phase
    return pod


# ---------------------------------------------------------------------------
# R4.AC1: query_stale called with threshold
# ---------------------------------------------------------------------------

class TestQueryStale:
    def test_calls_query_stale_with_threshold(self):
        """R4.AC1: Reconciler queries CouchDB for stale non-notified records."""
        store = _make_store(stale_records=[])
        ows = _make_ows()

        with patch("nuvolaris.reconciler._get_pod"), \
             patch("nuvolaris.reconciler._load_k8s"):
            reconcile(store, ows)

        store.query_stale.assert_called_once()

    def test_default_threshold_is_5_minutes(self):
        """NFR2: default staleness threshold is 5 minutes."""
        store = _make_store(stale_records=[])
        ows = _make_ows()

        with patch("nuvolaris.reconciler._load_k8s"):
            reconcile(store, ows)

        args = store.query_stale.call_args
        threshold = args.kwargs.get("threshold_minutes") or args.args[0]
        assert threshold == 5


# ---------------------------------------------------------------------------
# R4.AC2: stale record + pod absent → mark_terminal_once
# ---------------------------------------------------------------------------

class TestStalePodGone:
    def test_fires_notification_when_pod_absent(self):
        """R4.AC2: pod not found → mark_terminal_once with last known phase."""
        record = _stale_record(job_id="job-gone", pod_phase="Succeeded")
        store = _make_store(stale_records=[record])
        ows = _make_ows()

        with patch("nuvolaris.reconciler._get_pod", return_value=None), \
             patch("nuvolaris.reconciler._load_k8s"), \
             patch("nuvolaris.reconciler.mark_terminal_once") as mock_notify:
            reconcile(store, ows)

        mock_notify.assert_called_once_with("job-gone", "Succeeded", store, ows)

    def test_infers_failed_when_pod_phase_unknown(self):
        """R4.AC2: unknown pod_phase → infer Failed as safe default."""
        record = _stale_record(job_id="job-unknown", pod_phase=None)
        store = _make_store(stale_records=[record])
        ows = _make_ows()

        with patch("nuvolaris.reconciler._get_pod", return_value=None), \
             patch("nuvolaris.reconciler._load_k8s"), \
             patch("nuvolaris.reconciler.mark_terminal_once") as mock_notify:
            reconcile(store, ows)

        _, phase, _, _ = mock_notify.call_args.args
        assert phase == "Failed"

    def test_logs_reconciled_after_notification(self, caplog):
        """R4.AC4: structured INFO log with job_id and inferred_status after correction."""
        record = _stale_record(job_id="job-log", pod_phase="Failed")
        store = _make_store(stale_records=[record])
        ows = _make_ows()

        with caplog.at_level(logging.INFO, logger="nuvolaris.reconciler"), \
             patch("nuvolaris.reconciler._get_pod", return_value=None), \
             patch("nuvolaris.reconciler._load_k8s"), \
             patch("nuvolaris.reconciler.mark_terminal_once"):
            reconcile(store, ows)

        reconciled = [
            json.loads(r.message) for r in caplog.records
            if r.levelno == logging.INFO and "reconciled" in r.message
        ]
        assert len(reconciled) == 1
        assert reconciled[0]["job_id"] == "job-log"
        assert "inferred_status" in reconciled[0]
        assert "reconciled_at" in reconciled[0]


# ---------------------------------------------------------------------------
# R4.AC3: stale record + pod present → upsert_phase, no notification
# ---------------------------------------------------------------------------

class TestStalePodPresent:
    def test_refreshes_couchdb_when_pod_still_present(self):
        """R4.AC3: pod found → upsert_phase with current phase, no notification."""
        record = _stale_record(job_id="job-live", pod_phase="Running")
        store = _make_store(stale_records=[record])
        ows = _make_ows()
        pod = _mock_pod(phase="Running")

        with patch("nuvolaris.reconciler._get_pod", return_value=pod), \
             patch("nuvolaris.reconciler._load_k8s"), \
             patch("nuvolaris.reconciler.mark_terminal_once") as mock_notify:
            reconcile(store, ows)

        store.upsert_phase.assert_called_once_with("job-live", "nuvolaris", "Running")
        mock_notify.assert_not_called()

    def test_does_not_fire_notification_when_pod_present(self):
        """R4.AC3: pod present → no OpenServerless trigger fired."""
        record = _stale_record(job_id="job-running", pod_phase="Running")
        store = _make_store(stale_records=[record])
        pod = _mock_pod(phase="Running")

        with patch("nuvolaris.reconciler._get_pod", return_value=pod), \
             patch("nuvolaris.reconciler._load_k8s"), \
             patch("nuvolaris.reconciler.mark_terminal_once") as mock_notify:
            reconcile(store, _make_ows())

        mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# R4.AC5: CouchDB unreachable → abort cycle
# ---------------------------------------------------------------------------

class TestCouchDBUnreachable:
    def test_aborts_cycle_when_couchdb_unreachable(self):
        """R4.AC5: CouchDBConnectionError on query → abort, no pod lookups."""
        store = _make_store()
        store.query_stale.side_effect = CouchDBConnectionError("connection refused")
        ows = _make_ows()

        with patch("nuvolaris.reconciler._get_pod") as mock_get_pod, \
             patch("nuvolaris.reconciler._load_k8s"):
            reconcile(store, ows)

        mock_get_pod.assert_not_called()

    def test_logs_error_when_couchdb_unreachable(self, caplog):
        """R4.AC5: ERROR log emitted when CouchDB is unreachable."""
        store = _make_store()
        store.query_stale.side_effect = CouchDBConnectionError("timeout")
        ows = _make_ows()

        with caplog.at_level(logging.ERROR, logger="nuvolaris.reconciler"), \
             patch("nuvolaris.reconciler._load_k8s"):
            reconcile(store, ows)

        assert any("couchdb_error" in r.message for r in caplog.records)

    def test_does_not_raise_when_couchdb_unreachable(self):
        """R4.AC5: CouchDB failure must not crash the Reconciler process."""
        store = _make_store()
        store.query_stale.side_effect = CouchDBConnectionError("down")

        with patch("nuvolaris.reconciler._load_k8s"):
            reconcile(store, _make_ows())  # must not raise
