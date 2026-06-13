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

import threading
import pytest
from unittest.mock import MagicMock, patch

from nuvolaris.spark_notifier import (
    CouchDBStateStore,
    OWSClient,
    DeliveryError,
    mark_terminal_once,
)


def _make_store(doc):
    store = MagicMock(spec=CouchDBStateStore)
    store.get_doc.return_value = doc
    store.claim_notifying.return_value = True
    return store


def _make_ows():
    ows = MagicMock(spec=OWSClient)
    return ows


def _running_doc(job_id="job-1", rev="1-abc"):
    return {
        "_id": job_id, "_rev": rev,
        "job_id": job_id, "namespace": "nuvolaris",
        "pod_phase": "Succeeded", "status": "running",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "notified_at": None, "delivery_attempts": 0, "last_error": None,
    }


# ---------------------------------------------------------------------------
# Guard: already notified
# ---------------------------------------------------------------------------

class TestIdempotencyGuard:
    def test_skips_post_event_when_already_notified(self):
        """R2.AC4: status=notified → skip, no post_event call."""
        doc = {**_running_doc(), "status": "notified", "notified_at": "2026-01-01T01:00:00Z"}
        store = _make_store(doc)
        ows = _make_ows()

        mark_terminal_once("job-1", "Succeeded", store, ows)

        ows.post_event.assert_not_called()
        store.claim_notifying.assert_not_called()

    def test_logs_idempotency_guard_hit(self, caplog):
        """R2.AC4: guard hit → event_type idempotency_guard_hit in logs."""
        import logging
        doc = {**_running_doc(), "status": "notified"}
        store = _make_store(doc)
        ows = _make_ows()

        with caplog.at_level(logging.INFO, logger="nuvolaris.spark_notifier"):
            mark_terminal_once("job-1", "Succeeded", store, ows)

        assert any("idempotency_guard_hit" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Race condition: claim lost (409)
# ---------------------------------------------------------------------------

class TestClaimRace:
    def test_skips_post_event_when_claim_returns_false(self):
        """R2.AC3: claim_notifying returns False (409) → skip notification."""
        store = _make_store(_running_doc())
        store.claim_notifying.return_value = False
        ows = _make_ows()

        mark_terminal_once("job-1", "Succeeded", store, ows)

        ows.post_event.assert_not_called()
        store.mark_notified.assert_not_called()

    def test_concurrent_calls_fire_exactly_one_notification(self):
        """R2.AC3: two concurrent calls → only the one that wins the claim fires post_event."""
        notification_count = [0]
        lock = threading.Lock()

        def claim_side_effect(job_id, rev):
            with lock:
                if notification_count[0] == 0:
                    notification_count[0] += 1
                    return True
            return False

        doc = _running_doc()
        store = MagicMock(spec=CouchDBStateStore)
        store.get_doc.return_value = doc
        store.claim_notifying.side_effect = claim_side_effect
        ows = _make_ows()

        threads = [
            threading.Thread(target=mark_terminal_once, args=("job-1", "Succeeded", store, ows))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert ows.post_event.call_count == 1


# ---------------------------------------------------------------------------
# Delivery failure
# ---------------------------------------------------------------------------

class TestDeliveryFailure:
    def test_calls_mark_delivery_failed_on_delivery_error(self):
        """R3.AC3: DeliveryError after retries → mark_delivery_failed called."""
        store = _make_store(_running_doc())
        ows = _make_ows()
        ows.post_event.side_effect = DeliveryError("connection refused")

        mark_terminal_once("job-1", "Failed", store, ows)

        store.mark_delivery_failed.assert_called_once()
        store.mark_notified.assert_not_called()

    def test_does_not_mark_notified_on_delivery_failure(self):
        """mark_notified must not be called when delivery fails."""
        store = _make_store(_running_doc())
        ows = _make_ows()
        ows.post_event.side_effect = DeliveryError("timeout")

        mark_terminal_once("job-1", "Succeeded", store, ows)

        store.mark_notified.assert_not_called()


# ---------------------------------------------------------------------------
# Successful delivery
# ---------------------------------------------------------------------------

class TestSuccessfulDelivery:
    def test_calls_mark_notified_after_successful_post(self):
        """R5.AC3: successful POST → mark_notified called."""
        store = _make_store(_running_doc())
        ows = _make_ows()

        mark_terminal_once("job-1", "Succeeded", store, ows)

        store.mark_notified.assert_called_once_with("job-1")
        store.mark_delivery_failed.assert_not_called()

    def test_post_event_receives_correct_arguments(self):
        """R3.AC1: post_event called with job_id, phase, namespace, timestamp."""
        doc = _running_doc(job_id="job-xyz")
        doc["namespace"] = "test-ns"
        store = _make_store(doc)
        ows = _make_ows()

        mark_terminal_once("job-xyz", "Failed", store, ows)

        call_kwargs = ows.post_event.call_args
        args = call_kwargs.args if call_kwargs.args else call_kwargs[0]
        assert args[0] == "job-xyz"
        assert args[1] == "Failed"
        assert args[2] == "test-ns"
        assert args[3].endswith("Z")  # ISO-8601 timestamp


# ---------------------------------------------------------------------------
# Missing document
# ---------------------------------------------------------------------------

class TestMissingDocument:
    def test_does_nothing_when_doc_not_found(self):
        """mark_terminal_once must not crash when CouchDB doc is absent."""
        store = MagicMock(spec=CouchDBStateStore)
        store.get_doc.return_value = None
        ows = _make_ows()

        mark_terminal_once("job-missing", "Succeeded", store, ows)

        ows.post_event.assert_not_called()
