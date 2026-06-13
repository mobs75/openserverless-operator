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

import nuvolaris.config as cfg
from nuvolaris.spark_notifier import (
    CouchDBStateStore,
    CouchDBConnectionError,
    OWSClient,
    mark_terminal_once,
)
from nuvolaris.watcher import validate_credentials

_log = logging.getLogger(__name__)

_FALLBACK_TERMINAL_PHASE = "Failed"


def _get_pod(v1, job_id: str, namespace: str):
    """Return pod object or None if not found (ApiException 404)."""
    from kubernetes.client.exceptions import ApiException
    try:
        return v1.read_namespaced_pod(name=job_id, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def _load_k8s():
    """Load Kubernetes config and return CoreV1Api client."""
    from kubernetes import client as k8s_client, config as k8s_config
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
    return k8s_client.CoreV1Api()


def reconcile(store: CouchDBStateStore, ows_client: OWSClient) -> None:
    """One reconciliation cycle.

    Queries CouchDB for stale non-notified job records, then for each:
    - If the driver pod is gone → mark_terminal_once with last known phase (R4.AC2).
    - If the driver pod still exists → refresh CouchDB with current phase (R4.AC3).
    Aborts the whole cycle if CouchDB is unreachable (R4.AC5).
    """
    v1 = _load_k8s()

    threshold = cfg.get("spark.reconciler.staleness_minutes", defval=5)
    try:
        stale_records = store.query_stale(threshold_minutes=int(threshold))
    except CouchDBConnectionError as exc:
        _log.error(json.dumps({
            "event_type": "couchdb_error",
            "phase": "reconcile_query",
            "error": str(exc),
        }))
        return  # R4.AC5: abort cycle, next CronJob run will retry

    for record in stale_records:
        job_id = record.get("job_id", "")
        namespace = record.get("namespace", cfg.get("spark.namespace", defval="nuvolaris"))
        last_phase = record.get("pod_phase") or _FALLBACK_TERMINAL_PHASE

        try:
            pod = _get_pod(v1, job_id, namespace)
        except Exception as exc:
            _log.error(json.dumps({
                "event_type": "k8s_lookup_error",
                "job_id": job_id,
                "error": str(exc),
            }))
            continue

        if pod is None:
            # Pod is gone → infer terminal state and notify (R4.AC2)
            inferred_phase = last_phase if last_phase in ("Succeeded", "Failed") \
                else _FALLBACK_TERMINAL_PHASE
            mark_terminal_once(job_id, inferred_phase, store, ows_client)
            _log.info(json.dumps({
                "event_type": "reconciled",
                "job_id": job_id,
                "inferred_status": inferred_phase,
                "reconciled_at": _now(),
            }))
        else:
            # Pod still exists → refresh CouchDB, no notification (R4.AC3)
            current_phase = (pod.status and pod.status.phase) or "Unknown"
            store.upsert_phase(job_id, namespace, current_phase)
            _log.info(json.dumps({
                "event_type": "reconciled",
                "job_id": job_id,
                "inferred_status": current_phase,
                "reconciled_at": _now(),
                "pod_still_present": True,
            }))


def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def main() -> None:
    validate_credentials()
    store = CouchDBStateStore()
    ows_client = OWSClient()
    reconcile(store, ows_client)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
