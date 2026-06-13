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

import sys
import pytest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub out heavy operator framework deps before importing nuvolaris.spark
# ---------------------------------------------------------------------------
for _mod in [
    "kopf",
    "nuvolaris.kube",
    "nuvolaris.kustomize",
    "nuvolaris.util",
    "nuvolaris.operator_util",
    "nuvolaris.template",
    "nuvolaris.config",
    "flatdict",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from nuvolaris.spark import (   # noqa: E402  (import after sys.modules setup)
    _deploy_reconciliation_components,
    _delete_reconciliation_components,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_DATA = {
    "namespace": "nuvolaris",
    "ows_secret_name": "spark-ows-credentials",
    "reconciler_schedule": "*/5 * * * *",
    "watcher_replicas": 1,
    "spark_image": "apache/spark:3.5.0",
    "couchdb_secret_name": "spark-couchdb-credentials",
}

_FAKE_WATCHER_YAML = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: spark-watcher
  namespace: nuvolaris
spec:
  replicas: 1
"""

_FAKE_RECONCILER_YAML = """\
apiVersion: batch/v1
kind: CronJob
metadata:
  name: spark-reconciler
  namespace: nuvolaris
spec:
  schedule: "*/5 * * * *"
"""


def _rendered(template_name, data):
    if "watcher" in template_name:
        return _FAKE_WATCHER_YAML
    return _FAKE_RECONCILER_YAML


# ---------------------------------------------------------------------------
# R6.AC1: _deploy_reconciliation_components applies Watcher + Reconciler
# ---------------------------------------------------------------------------

class TestDeployReconciliationComponents:
    def test_applies_watcher_deployment(self):
        """R6.AC1: kube.apply called for Watcher Deployment."""
        with patch("nuvolaris.spark.ntp.expand_template", side_effect=_rendered), \
             patch("nuvolaris.spark.kube.apply") as mock_apply, \
             patch("nuvolaris.spark.kopf.append_owner_reference"):
            _deploy_reconciliation_components(owner=None, data=_SAMPLE_DATA)

        kinds = [c.args[0]["kind"] for c in mock_apply.call_args_list]
        assert "Deployment" in kinds

    def test_applies_reconciler_cronjob(self):
        """R6.AC1: kube.apply called for Reconciler CronJob."""
        with patch("nuvolaris.spark.ntp.expand_template", side_effect=_rendered), \
             patch("nuvolaris.spark.kube.apply") as mock_apply, \
             patch("nuvolaris.spark.kopf.append_owner_reference"):
            _deploy_reconciliation_components(owner=None, data=_SAMPLE_DATA)

        kinds = [c.args[0]["kind"] for c in mock_apply.call_args_list]
        assert "CronJob" in kinds

    def test_applies_two_resources(self):
        """R6.AC1: exactly two kube.apply calls — one per component."""
        with patch("nuvolaris.spark.ntp.expand_template", side_effect=_rendered), \
             patch("nuvolaris.spark.kube.apply") as mock_apply, \
             patch("nuvolaris.spark.kopf.append_owner_reference"):
            _deploy_reconciliation_components(owner=None, data=_SAMPLE_DATA)

        assert mock_apply.call_count == 2

    # -----------------------------------------------------------------------
    # R6.AC3: ownerReferences appended when owner is provided
    # -----------------------------------------------------------------------

    def test_appends_owner_reference_when_owner_provided(self):
        """R6.AC3: kopf.append_owner_reference called once per resource when owner set."""
        owner = MagicMock()
        with patch("nuvolaris.spark.ntp.expand_template", side_effect=_rendered), \
             patch("nuvolaris.spark.kube.apply"), \
             patch("nuvolaris.spark.kopf.append_owner_reference") as mock_owner:
            _deploy_reconciliation_components(owner=owner, data=_SAMPLE_DATA)

        assert mock_owner.call_count == 2

    def test_skips_owner_reference_when_owner_is_none(self):
        """R6.AC3: kopf.append_owner_reference not called when owner is None."""
        with patch("nuvolaris.spark.ntp.expand_template", side_effect=_rendered), \
             patch("nuvolaris.spark.kube.apply"), \
             patch("nuvolaris.spark.kopf.append_owner_reference") as mock_owner:
            _deploy_reconciliation_components(owner=None, data=_SAMPLE_DATA)

        mock_owner.assert_not_called()

    # -----------------------------------------------------------------------
    # R6.AC4: exception from kube.apply propagates to caller
    # -----------------------------------------------------------------------

    def test_propagates_apply_exception(self):
        """R6.AC4: kube.apply failure must propagate so Kopf can retry."""
        with patch("nuvolaris.spark.ntp.expand_template", side_effect=_rendered), \
             patch("nuvolaris.spark.kube.apply", side_effect=RuntimeError("k8s down")), \
             patch("nuvolaris.spark.kopf.append_owner_reference"):
            with pytest.raises(RuntimeError, match="k8s down"):
                _deploy_reconciliation_components(owner=None, data=_SAMPLE_DATA)


# ---------------------------------------------------------------------------
# R6.AC2: _delete_reconciliation_components removes Watcher + Reconciler
# ---------------------------------------------------------------------------

class TestDeleteReconciliationComponents:
    def test_deletes_watcher_deployment(self):
        """R6.AC2: kube.delete called for Deployment/spark-watcher."""
        with patch("nuvolaris.spark.kube.delete") as mock_delete:
            _delete_reconciliation_components(namespace="nuvolaris")

        names = [c.args[0]["metadata"]["name"] for c in mock_delete.call_args_list]
        assert "spark-watcher" in names

    def test_deletes_reconciler_cronjob(self):
        """R6.AC2: kube.delete called for CronJob/spark-reconciler."""
        with patch("nuvolaris.spark.kube.delete") as mock_delete:
            _delete_reconciliation_components(namespace="nuvolaris")

        names = [c.args[0]["metadata"]["name"] for c in mock_delete.call_args_list]
        assert "spark-reconciler" in names

    def test_deletes_two_resources(self):
        """R6.AC2: exactly two kube.delete calls — one per component."""
        with patch("nuvolaris.spark.kube.delete") as mock_delete:
            _delete_reconciliation_components(namespace="nuvolaris")

        assert mock_delete.call_count == 2

    def test_uses_correct_namespace(self):
        """R6.AC2: deleted resources carry the supplied namespace."""
        with patch("nuvolaris.spark.kube.delete") as mock_delete:
            _delete_reconciliation_components(namespace="custom-ns")

        namespaces = {c.args[0]["metadata"]["namespace"] for c in mock_delete.call_args_list}
        assert namespaces == {"custom-ns"}

    def test_continues_on_delete_error(self):
        """R6.AC2: a delete failure for one component must not prevent deleting the other."""
        call_count = 0

        def fail_first(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("not found")

        with patch("nuvolaris.spark.kube.delete", side_effect=fail_first):
            _delete_reconciliation_components(namespace="nuvolaris")  # must not raise

        assert call_count == 2
