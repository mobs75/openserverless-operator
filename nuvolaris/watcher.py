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

import nuvolaris.config as cfg
from nuvolaris.spark_notifier import (
    CouchDBStateStore,
    OWSClient,
    TERMINAL_PHASES,
    mark_terminal_once,
)

_log = logging.getLogger(__name__)

_DRIVER_LABEL_SELECTOR = (
    "nuvolaris.org/spark-role=driver,nuvolaris.org/component=spark"
)
_REQUIRED_CREDENTIALS = ["OWS_TRIGGER_URL", "OWS_AUTH_TOKEN", "COUCHDB_URL"]


def validate_credentials() -> None:
    """Halt with exit(1) if any required Secret env var is missing (R7.AC2)."""
    missing = [k for k in _REQUIRED_CREDENTIALS if not os.environ.get(k)]
    if missing:
        _log.error(json.dumps({
            "event_type": "missing_credentials",
            "missing": missing,
        }))
        sys.exit(1)


def handle_pod_event(pod, store: CouchDBStateStore, ows_client: OWSClient) -> None:
    """Process one pod Watch event.

    - Skips pods with empty/absent nuvolaris.org/spark-job-id (R1.AC3).
    - Calls upsert_phase for every phase including Running (R5.AC1, R5.AC2, R2.AC5).
    - Calls mark_terminal_once only for Succeeded/Failed (R2.AC1, R2.AC2).
    - All log entries are structured JSON with event_type (NFR3).
    """
    labels = pod.metadata.labels or {}
    job_id = labels.get("nuvolaris.org/spark-job-id", "")
    if not job_id:
        _log.warning(json.dumps({
            "event_type": "skip_no_job_id",
            "pod": pod.metadata.name,
            "namespace": pod.metadata.namespace,
        }))
        return

    namespace = pod.metadata.namespace or cfg.get("spark.namespace", defval="nuvolaris")
    phase = (pod.status and pod.status.phase) or "Unknown"

    _log.info(json.dumps({
        "event_type": "pod_phase_observed",
        "job_id": job_id,
        "namespace": namespace,
        "phase": phase,
    }))

    store.upsert_phase(job_id, namespace, phase)

    if phase in TERMINAL_PHASES:
        mark_terminal_once(job_id, phase, store, ows_client)


def watch_driver_pods(
    namespace: str,
    store: CouchDBStateStore,
    ows_client: OWSClient,
) -> None:
    """Stream pod events for Spark driver pods and dispatch to handle_pod_event.

    Uses the Kubernetes Watch API with the standard driver label selector (R1.AC1).
    Skips DELETED events — the Reconciler handles missed terminal transitions.
    """
    from kubernetes import client as k8s_client, watch
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

    v1 = k8s_client.CoreV1Api()
    w = watch.Watch()

    _log.info(json.dumps({
        "event_type": "watcher_started",
        "namespace": namespace,
        "selector": _DRIVER_LABEL_SELECTOR,
    }))

    for event in w.stream(
        v1.list_namespaced_pod,
        namespace=namespace,
        label_selector=_DRIVER_LABEL_SELECTOR,
    ):
        event_type = event.get("type", "")
        if event_type == "DELETED":
            continue
        pod = event["object"]
        try:
            handle_pod_event(pod, store, ows_client)
        except Exception as exc:
            _log.error(json.dumps({
                "event_type": "handle_pod_error",
                "pod": getattr(pod.metadata, "name", "unknown"),
                "error": str(exc),
            }))


def main() -> None:
    validate_credentials()
    namespace = cfg.get("spark.namespace", defval="nuvolaris")
    store = CouchDBStateStore()
    ows_client = OWSClient()
    watch_driver_pods(namespace, store, ows_client)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
