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
import sys
import json
import logging
import pytest
from unittest.mock import MagicMock, patch, call

from nuvolaris.spark_notifier import CouchDBStateStore, OWSClient
from nuvolaris.watcher import handle_pod_event, validate_credentials


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pod(job_id=None, phase="Running", namespace="nuvolaris", name="spark-driver-abc"):
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.labels = {
        "nuvolaris.org/component": "spark",
        "nuvolaris.org/spark-role": "driver",
    }
    if job_id is not None:
        pod.metadata.labels["nuvolaris.org/spark-job-id"] = job_id
    pod.status.phase = phase
    return pod


def _make_store():
    return MagicMock(spec=CouchDBStateStore)


def _make_ows():
    return MagicMock(spec=OWSClient)


# ---------------------------------------------------------------------------
# Label validation
# ---------------------------------------------------------------------------

class TestLabelValidation:
    def test_skips_pod_without_spark_job_id(self):
        """R1.AC3: pod without spark-job-id → WARNING log, no upsert_phase."""
        pod = _make_pod(job_id=None)
        store = _make_store()
        ows = _make_ows()

        handle_pod_event(pod, store, ows)

        store.upsert_phase.assert_not_called()

    def test_logs_warning_for_missing_job_id(self, caplog):
        """R1.AC3: skip event must log a WARNING with event_type skip_no_job_id."""
        pod = _make_pod(job_id=None)

        with caplog.at_level(logging.WARNING, logger="nuvolaris.watcher"):
            handle_pod_event(pod, _make_store(), _make_ows())

        assert any("skip_no_job_id" in r.message for r in caplog.records)

    def test_skips_pod_with_empty_job_id(self):
        """Empty string job_id is treated the same as absent."""
        pod = _make_pod(job_id="")
        store = _make_store()

        handle_pod_event(pod, store, _make_ows())

        store.upsert_phase.assert_not_called()

    def test_processes_pod_with_valid_job_id(self):
        """Pod with valid spark-job-id → upsert_phase is called."""
        pod = _make_pod(job_id="job-valid", phase="Running")
        store = _make_store()

        handle_pod_event(pod, store, _make_ows())

        store.upsert_phase.assert_called_once_with("job-valid", "nuvolaris", "Running")


# ---------------------------------------------------------------------------
# Phase dispatch — Running (no notification)
# ---------------------------------------------------------------------------

class TestRunningPhase:
    def test_running_phase_calls_upsert_not_mark_terminal(self):
        """R2.AC5: Running → upsert_phase, no mark_terminal_once."""
        pod = _make_pod(job_id="job-run", phase="Running")
        store = _make_store()
        ows = _make_ows()

        with patch("nuvolaris.watcher.mark_terminal_once") as mock_terminal:
            handle_pod_event(pod, store, ows)
            store.upsert_phase.assert_called_once_with("job-run", "nuvolaris", "Running")
            mock_terminal.assert_not_called()


# ---------------------------------------------------------------------------
# Phase dispatch — terminal states (Succeeded / Failed)
# ---------------------------------------------------------------------------

class TestTerminalPhases:
    @pytest.mark.parametrize("phase", ["Succeeded", "Failed"])
    def test_terminal_phase_calls_upsert_and_mark_terminal(self, phase):
        """R2.AC1, R2.AC2: terminal phase → upsert_phase + mark_terminal_once."""
        pod = _make_pod(job_id="job-term", phase=phase)
        store = _make_store()
        ows = _make_ows()

        with patch("nuvolaris.watcher.mark_terminal_once") as mock_terminal:
            handle_pod_event(pod, store, ows)

            store.upsert_phase.assert_called_once_with("job-term", "nuvolaris", phase)
            mock_terminal.assert_called_once_with("job-term", phase, store, ows)

    def test_upsert_called_before_mark_terminal(self):
        """State must be persisted before notification fires."""
        pod = _make_pod(job_id="job-order", phase="Succeeded")
        store = _make_store()
        ows = _make_ows()
        call_order = []

        store.upsert_phase.side_effect = lambda *a: call_order.append("upsert")

        with patch("nuvolaris.watcher.mark_terminal_once",
                   side_effect=lambda *a: call_order.append("notify")):
            handle_pod_event(pod, store, ows)

        assert call_order == ["upsert", "notify"]


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class TestStructuredLogging:
    def test_log_entry_is_valid_json(self, caplog):
        """NFR3: log entries must be parseable JSON dicts."""
        pod = _make_pod(job_id="job-log", phase="Running")

        with caplog.at_level(logging.INFO, logger="nuvolaris.watcher"):
            handle_pod_event(pod, _make_store(), _make_ows())

        json_entries = []
        for r in caplog.records:
            try:
                parsed = json.loads(r.message)
                if "event_type" in parsed:
                    json_entries.append(parsed)
            except (ValueError, TypeError):
                pass
        assert len(json_entries) > 0

    def test_log_entry_contains_event_type(self, caplog):
        """NFR3: every INFO entry has event_type field."""
        pod = _make_pod(job_id="job-log2", phase="Succeeded")

        with caplog.at_level(logging.INFO, logger="nuvolaris.watcher"):
            with patch("nuvolaris.watcher.mark_terminal_once"):
                handle_pod_event(pod, _make_store(), _make_ows())

        info_entries = [r for r in caplog.records if r.levelno == logging.INFO]
        for entry in info_entries:
            try:
                parsed = json.loads(entry.message)
                assert "event_type" in parsed
            except (ValueError, TypeError):
                pytest.fail(f"non-JSON log entry: {entry.message}")


# ---------------------------------------------------------------------------
# validate_credentials
# ---------------------------------------------------------------------------

class TestValidateCredentials:
    def test_exits_when_credentials_missing(self):
        """R7.AC2: missing env var → sys.exit(1)."""
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("OWS_TRIGGER_URL", "OWS_AUTH_TOKEN", "COUCHDB_URL")}
        with patch.dict(os.environ, clean_env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                validate_credentials()
            assert exc_info.value.code == 1

    def test_does_not_exit_when_all_credentials_present(self):
        """All required vars set → no exception."""
        env = {
            "OWS_TRIGGER_URL": "http://fake/trigger",
            "OWS_AUTH_TOKEN": "tok",
            "COUCHDB_URL": "http://couchdb:5984",
        }
        with patch.dict(os.environ, env):
            validate_credentials()  # must not raise

    def test_logs_error_listing_missing_vars(self, caplog):
        """R7.AC2: ERROR log must name the missing variables."""
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("OWS_TRIGGER_URL", "OWS_AUTH_TOKEN", "COUCHDB_URL")}
        with patch.dict(os.environ, clean_env, clear=True):
            with caplog.at_level(logging.ERROR, logger="nuvolaris.watcher"):
                with pytest.raises(SystemExit):
                    validate_credentials()
        assert any("missing_credentials" in r.message for r in caplog.records)
